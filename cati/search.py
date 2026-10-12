"""Búsqueda instantánea: nombre, palabras y significado fusionados en una sola lista.

Los vectores viven en memoria (float16) y se actualizan por incrementos cuando el
indexador publica cambios, así que una consulta solo cuesta un producto matricial.
"""
import fcntl
import math
from difflib import SequenceMatcher
import re
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .catalog import ARCHIVES, EXTRA_CODE, EXTRA_IMAGES, OFFICE, SHEETS, SLIDES, category
from .engine import Engine
from .query import Intent, normalize, parse, stem
from .readers import AUDIO, CODE, IMAGES, TEXT, VIDEO, image
from .config import DATA
from .store import connect

CANDIDATES, SAMPLE = 10000, 4000
MODALITY = {'audio': 2, 'image': 1, 'pdf-image': 1, 'video-frame': 1, 'doc-image': 1}
CATEGORY_EXTS = {'document': TEXT | OFFICE, 'pdf': {'.pdf'}, 'image': IMAGES | EXTRA_IMAGES, 'video': VIDEO,
                 'audio': AUDIO, 'sheet': SHEETS, 'slides': SLIDES, 'code': CODE | EXTRA_CODE, 'archive': ARCHIVES}
LEGACY_KINDS = {'documents': {'document', 'pdf'}}


@dataclass
class Results:
    hits: list
    intent: Intent
    semantic: bool = False
    elapsed: float = 0.0
    counts: dict = field(default_factory=dict)


def kind_of(path, indexed=None, is_dir=False):
    """Categoría estable para la interfaz; respeta el tipo con que se indexó."""
    if indexed == 'code': return 'code'
    return category(Path(path).suffix.lower(), is_dir)


class Searcher:
    def __init__(self, db_path=None, engine=None):
        self.db_path = db_path
        self.engine = engine
        self.db = connect(db_path, threads=True)
        self.lock = threading.RLock()
        self.version = None
        self.last_id = 0
        self.size = 0
        self.columns = {}
        self.allocate(0)
        self.by_path = {}
        self.catalog_rows = 0
        self.files = {}
        self.file_code = np.zeros(1, bool)
        self.vectors = OrderedDict()

    # ------------------------------------------------------------------ caché
    # Vectors: a 1-bit sign code per dimension (96 bytes) stays in memory and ranks the whole
    # index by Hamming distance; full 768-dimension float16 vectors live in a file on disk and
    # are read only for the best candidates (the kernel caches what is used).
    LAYOUT = {'ids': (np.int64, ()), 'seg_file': (np.int64, ()), 'seg_mod': (np.int8, ()), 'seg_scale': (bool, ()),
              'alive': (bool, ()), 'bits': (np.uint8, (96,))}

    def allocate(self, capacity):
        grown = {}
        for name, (dtype, shape) in self.LAYOUT.items():
            array = np.zeros((capacity, *shape), dtype)
            if name in self.columns: array[:self.size] = self.columns[name][:self.size]
            grown[name] = array
        self.columns = grown
        self.views()

    def views(self):
        for name in self.LAYOUT: setattr(self, name, self.columns[name][:self.size])
        self.full = None  # reopened lazily at the right length

    def owner(self):
        """Un solo proceso mantiene la caché compartida; los demás (p. ej. «cati search») usan una privada."""
        if not hasattr(self, '_owner'):
            base = Path(self.db_path).with_suffix('.vectors.lock') if self.db_path else DATA / 'vectors.lock'
            self._lock = open(base, 'a')
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._owner = True
            except BlockingIOError:
                self._owner = False
                self._private = tempfile.NamedTemporaryFile(prefix='cati-vectors-', dir=base.parent, delete=True)
        return self._owner

    def vectors_path(self):
        if not self.owner(): return Path(self._private.name)
        return Path(self.db_path).with_suffix('.vectors.f16') if self.db_path else DATA / 'vectors.f16'

    def snapshot_path(self):
        return Path(self.db_path).with_suffix('.vectors.npz') if self.db_path else DATA / 'vectors.npz'

    def matrix(self):
        if self.full is None and self.size:
            self.full = np.memmap(self.vectors_path(), dtype=np.float16, mode='r', shape=(self.size, 768))
        return self.full

    def append(self, rows):
        count = len(rows)
        if self.size + count > len(self.columns['ids']):
            self.allocate(max(4096, 2 * len(self.columns['ids']), self.size + count))  # amortised: no copy per refresh
        block = np.frombuffer(b''.join(r[4] for r in rows), dtype='<f4').reshape(count, 768)
        path = self.vectors_path()
        if self.size == 0: open(path, 'wb').close()  # a fresh cache starts empty
        with open(path, 'ab') as out:
            out.write(block.astype(np.float16).tobytes())
        path.chmod(0o600)
        end = self.size + count
        c = self.columns
        c['ids'][self.size:end] = [r[0] for r in rows]
        c['seg_file'][self.size:end] = [r[1] for r in rows]
        c['seg_mod'][self.size:end] = [MODALITY.get(r[2], 0) for r in rows]
        c['seg_scale'][self.size:end] = [bool(r[3]) for r in rows]
        c['alive'][self.size:end] = True
        c['bits'][self.size:end] = np.packbits(block > 0, axis=1)
        self.size = end
        self.views()

    def exact(self, rows, vector):
        """Coseno completo (768 dimensiones) leyendo del disco solo las filas pedidas, en orden."""
        return self.matrix()[rows].astype(np.float32) @ vector

    def load_snapshot(self):
        """Arranque sin releer SQLite: metadatos y huellas desde la copia, vectores desde su archivo."""
        if not self.owner(): return
        try:
            with np.load(self.snapshot_path()) as data:
                size = len(data['ids'])
                if int(data['dims']) != 768 or self.vectors_path().stat().st_size != size * 768 * 2: raise ValueError
                self.allocate(max(4096, size))
                for name in self.LAYOUT: self.columns[name][:size] = data[name]
                self.size = size
                self.last_id = int(data['ids'][-1]) if size else 0
                self.views()
            newest = self.db.execute('SELECT max(id) FROM segments').fetchone()[0] or 0
            if self.last_id > newest: raise ValueError  # index rebuilt since the copy was saved
        except (OSError, KeyError, ValueError):
            self.size, self.last_id = 0, 0
            self.allocate(0)

    def save_snapshot(self):
        if not self.owner(): return
        path = self.snapshot_path()
        partial = path.with_name(path.name + '.part.npz')
        try:
            np.savez(partial, dims=768, **{name: getattr(self, name) for name in self.LAYOUT})
            partial.chmod(0o600)
            partial.replace(path)
            self.saved = self.size
        except OSError:
            partial.unlink(missing_ok=True)

    def compact(self):
        """Reescribe sin los fragmentos borrados cuando ya son más de una cuarta parte."""
        keep = np.nonzero(self.alive)[0]
        vectors = np.array(self.matrix()[keep])
        for name in self.LAYOUT: self.columns[name][:len(keep)] = self.columns[name][keep]
        self.size = len(keep)
        temporary = self.vectors_path().with_suffix('.tmp')
        vectors.tofile(temporary)
        temporary.chmod(0o600)
        temporary.replace(self.vectors_path())
        self.views()
        self.save_snapshot()

    def refresh(self):
        version = self.db.execute('PRAGMA data_version').fetchone()[0]
        if version == self.version: return
        if self.version is None: self.load_snapshot()
        self.version = version
        rows = self.db.execute('SELECT id,file,kind,sets_scale,vec FROM segments WHERE id>? ORDER BY id', (self.last_id,)).fetchall()
        if rows:
            self.append(rows)
            self.last_id = int(self.ids[-1])
        count = self.db.execute('SELECT count(*) FROM segments').fetchone()[0]
        if count != int(self.alive.sum()):
            live = np.fromiter((r[0] for r in self.db.execute('SELECT id FROM segments')), np.int64)
            self.columns['alive'][:self.size] = np.isin(self.ids, live)
            self.views()
            if self.alive.sum() < .75 * self.size: self.compact()
        # Keep the copy on disk close to the index: rewrite after every few thousand new fragments.
        if self.size - getattr(self, 'saved', 0) >= 4000 or (rows and getattr(self, 'saved', None) is None):
            self.save_snapshot()
        self.files = {r[0]: (r[1], r[2], r[3], r[4], kind_of(r[1], r[2])) for r in
                      self.db.execute('SELECT id,path,kind,mtime,size FROM files WHERE error IS NULL')}
        self.by_path = {info[0]: fid for fid, info in self.files.items()}
        self.catalog_rows = self.db.execute('SELECT count(*) FROM (SELECT 1 FROM catalog LIMIT 1)').fetchone()[0]
        size = max(self.files, default=0) + 1
        self.file_code = np.zeros(size, bool)
        for fid, (_, kind, _, _, _) in self.files.items():
            if kind == 'code': self.file_code[fid] = True

    def query_vector(self, text, code, image_path):
        key = ('image', str(image_path)) if image_path else (text, code)
        if key in self.vectors:
            self.vectors.move_to_end(key)
            return self.vectors[key]
        model = self.engine or Engine()
        self.engine = model
        if image_path:
            with tempfile.TemporaryDirectory(prefix='cati-query-') as work:
                vector = model.media(image(Path(image_path), Path(work) / 'query.png', 140), 'image')
        else:
            vector = model.text([text], query=True, code=code)[0]
        self.vectors[key] = vector
        while len(self.vectors) > 256: self.vectors.popitem(last=False)
        return vector

    def warm(self):
        """Carga índice y modelo antes de que llegue la primera consulta."""
        with self.lock: self.refresh()
        try: self.query_vector('documento', False, None)
        except Exception: pass

    # --------------------------------------------------------------- filtros
    def accepts(self, intent, path, kind, mtime, size, is_dir=False):
        if intent.categories and kind not in intent.categories: return False
        if intent.exts and (is_dir or Path(path).suffix.lower() not in intent.exts): return False
        if intent.after and mtime < intent.after: return False
        if intent.before and mtime >= intent.before: return False
        if intent.min_size and (is_dir or size < intent.min_size): return False
        if intent.max_size and (is_dir or size > intent.max_size): return False
        if intent.location and not str(path).startswith(str(intent.location) + '/'): return False
        if intent.location_word and intent.location_word not in normalize(str(Path(path).parent)): return False
        return True

    def catalog_sql(self, intent):
        clauses, values = [], []
        if intent.categories:
            parts = []
            if 'folder' in intent.categories: parts.append('c.is_dir=1')
            exts = set().union(*(CATEGORY_EXTS.get(c, set()) for c in intent.categories))
            if exts: parts.append(f"(c.is_dir=0 AND c.ext IN ({','.join('?' * len(exts))}))"); values += sorted(exts)
            if 'other' in intent.categories: parts.append('c.is_dir=0')
            clauses.append('(' + ' OR '.join(parts or ['0']) + ')')
        if intent.exts:
            clauses.append(f"c.is_dir=0 AND c.ext IN ({','.join('?' * len(intent.exts))})"); values += sorted(intent.exts)
        if intent.after: clauses.append('c.mtime>=?'); values.append(intent.after)
        if intent.before: clauses.append('c.mtime<?'); values.append(intent.before)
        if intent.min_size: clauses.append('c.size>=?'); values.append(intent.min_size)
        if intent.max_size: clauses.append('c.size<=?'); values.append(intent.max_size)
        if intent.location:
            root = str(intent.location)
            clauses.append('c.path>=? AND c.path<?'); values += [root + '/', root + '0']
        return clauses, values

    # ---------------------------------------------------------------- nombre
    def by_name(self, intent, terms, limit=3000):
        clauses, values = self.catalog_sql(intent)
        stems = [stem(t) for t in terms]
        long = [s for s in stems if len(s) >= 3]
        short = [s for s in stems if len(s) < 3]
        where = ' AND '.join(clauses)
        if long:
            match = ' OR '.join('"' + s.replace('"', '') + '"' for s in long)
            sql = ('SELECT c.* FROM catalog_names JOIN catalog c ON c.id=catalog_names.rowid WHERE catalog_names MATCH ?'
                   + (' AND ' + where if where else '') + ' ORDER BY rank LIMIT ?')
            rows = self.db.execute(sql, [match, *values, limit]).fetchall()
        elif short:
            like = ' AND '.join('c.name LIKE ?' for _ in short)
            sql = 'SELECT c.* FROM catalog c WHERE ' + like + (' AND ' + where if where else '') + ' LIMIT ?'
            rows = self.db.execute(sql, [*('%' + s + '%' for s in short), *values, limit]).fetchall()
        else:
            if not intent.filtered and not intent.recent: return {}
            sql = 'SELECT c.* FROM catalog c' + (' WHERE ' + where if where else '') + ' ORDER BY c.mtime DESC LIMIT ?'
            rows = self.db.execute(sql, [*values, 400]).fetchall()
        found = {}
        for row in rows:
            share, exact = self.name_share(terms, stems, row['name'], row['folders'])
            if terms and share <= 0: continue
            found[row['path']] = {'share': share, 'exact': exact, 'is_dir': bool(row['is_dir']),
                                  'mtime': row['mtime'], 'size': row['size'], 'fuzzy': False}
        # Typos: a long word with no exact hit looks for names sharing one of its 4-letter pieces
        # (selective in the trigram index, so it stays fast), then keeps only close spellings.
        missing = [s for s in stems if len(s) >= 5 and not any(s in normalize(r['name']) for r in rows[:300])]
        if missing:
            for path, entry in found.items():
                row_name = next((r['name'] for r in rows if r['path'] == path), None)
                if row_name is None: continue
                share, _ = self.name_share(terms, stems, row_name, '', fuzzy=True)
                if share > entry['share']: entry.update(share=share, fuzzy=True)
            size = lambda w: 3 if len(w) <= 6 else 4  # one typo still leaves short words a 3-letter piece intact
            per_word = {w: {w[i:i + size(w)] for i in range(len(w) - size(w) + 1)} for w in missing}
            pieces = sorted(set().union(*per_word.values()))
            sql = ('SELECT c.* FROM catalog_names JOIN catalog c ON c.id=catalog_names.rowid WHERE catalog_names MATCH ?'
                   + (' AND ' + where if where else '') + ' LIMIT 3000')
            for row in self.db.execute(sql, [' OR '.join(f'name:"{g}"' for g in pieces), *values]).fetchall():
                if row['path'] in found: continue
                name_n = normalize(row['name'])
                # Cheap filter before the similarity check: share a third of the word's pieces.
                if not any(sum(g in name_n for g in grams) >= max(1, len(grams) // 3) for grams in per_word.values()): continue
                share, exact = self.name_share(terms, stems, row['name'], row['folders'], fuzzy=True)
                if share >= .5:
                    found[row['path']] = {'share': share * .85, 'exact': False, 'is_dir': bool(row['is_dir']),
                                          'mtime': row['mtime'], 'size': row['size'], 'fuzzy': True}
        return found

    @staticmethod
    def name_share(terms, stems, name, folders, fuzzy=False):
        if not terms: return 0.0, False
        name_n = normalize(name)
        tokens = re.findall(r'\w+', name_n)
        folder_tokens = re.findall(r'\w+', normalize(folders))
        total = 0.0
        for term, root in zip(terms, stems):
            if term in tokens: total += 1.0
            elif any(t.startswith(root) for t in tokens): total += .9
            elif len(root) >= 3 and root in name_n: total += .7
            elif any(t.startswith(root) for t in folder_tokens): total += .45
            elif fuzzy and len(root) >= 5:
                # «piramdes» ≈ «piramides»: similarity of the closest word, prefixes included.
                best = max((SequenceMatcher(None, root, t[:len(root) + 2]).ratio() for t in tokens
                            if len(t) >= 4 and t[0] == root[0] and abs(len(t) - len(root)) <= 4), default=0)
                if best >= .8: total += .7 * best
        joined = ' '.join(terms)
        stem_part = name_n.rsplit(' ', 1)[0] if ' ' in name_n else name_n
        exact = joined == stem_part or joined == name_n
        return total / len(terms), exact

    # -------------------------------------------------------------- palabras
    def by_words(self, terms, allowed_file):
        if not terms: return {}, {}
        patterns = []
        for term in terms:
            root = stem(term)
            patterns.append('"' + root.replace('"', '') + '"' + ('*' if root != term else ''))
        rows = self.db.execute('''SELECT words.rowid AS sid,s.file,snippet(words,1,'«','»','…',36) AS snippet
            FROM words JOIN segments s ON s.id=words.rowid WHERE words MATCH ? ORDER BY bm25(words,3,1) LIMIT 1000''',
            ('{text}: (' + ' OR '.join(patterns) + ')',)).fetchall()
        best = {}
        for row in rows:
            if row['file'] in best or not allowed_file(row['file']): continue
            best[row['file']] = (row['sid'], row['snippet'])
        coverage = {fid: 0 for fid in best}
        if best:
            ids = ','.join(str(f) for f in best)
            for pattern in patterns:
                for (fid,) in self.db.execute(f'''SELECT DISTINCT s.file FROM words JOIN segments s ON s.id=words.rowid
                        WHERE words MATCH ? AND s.file IN ({ids})''', ('{text}: ' + pattern,)):
                    coverage[fid] += 1
        return best, {fid: n / len(terms) for fid, n in coverage.items()}

    # ------------------------------------------------------------ significado
    def by_meaning(self, vector, code, allowed_file, image_query, keep=()):
        """Mejor fragmento por archivo, con puntuación z calibrada por modalidad.

        Pasada gruesa por distancia Hamming sobre las huellas binarias de todo el índice y coseno
        exacto solo para los candidatos, una muestra fija para la calibración y los archivos de `keep`."""
        if not self.size: return {}, {}
        base = self.alive & (self.file_code[np.minimum(self.seg_file, len(self.file_code) - 1)] == code)
        indices = np.nonzero(base)[0]
        if not len(indices): return {}, {}
        vector = vector.astype(np.float32)
        distance = np.bitwise_count(self.bits[indices] ^ np.packbits(vector > 0)).sum(axis=1, dtype=np.uint16)
        wanted = min(CANDIDATES, max(3000, len(indices) // 15))  # ~7 % of the index: recall ≥ 0.98 at any size
        top = indices[np.argpartition(distance, wanted)[:wanted]] if len(indices) > wanted else indices
        sample = indices[::max(1, len(indices) // SAMPLE)]
        mods = self.seg_mod[indices]
        small = [m for m in (0, 1, 2) if 0 < int((mods == m).sum()) < 20]
        extra = [indices[mods == m] for m in small]
        if keep: extra.append(indices[np.isin(self.seg_file[indices], np.fromiter(keep, np.int64))])
        rows = np.unique(np.concatenate([top, sample, *extra]))
        cosine = self.exact(rows, vector)
        lookup = {int(r): i for i, r in enumerate(rows)}
        sample_pos = np.fromiter((lookup[int(r)] for r in sample), np.int64, len(sample))
        means, centered = {}, []
        for mod in (0, 1, 2):
            group = sample_pos[self.seg_mod[sample] == mod]
            if not len(group): continue
            scaled = group[self.seg_scale[rows[group]]]
            chosen = scaled if len(scaled) else group
            means[mod] = float(cosine[chosen].mean())
            centered.append(cosine[chosen] - means[mod])
        if not centered: return {}, {}
        deviation = max(float(np.std(np.concatenate(centered))), .02)
        row_mod = self.seg_mod[rows]
        mean_row = np.array([means.get(int(m), 0.0) for m in range(3)], np.float32)[row_mod]
        z = (cosine - mean_row) / deviation
        qualifies = z >= 2.0
        for mod in small:
            # Three example photos can be searched; a missing object should not
            # make the least unrelated photo appear as a confident result.
            group = row_mod == mod
            local = max(float(np.std(cosine[group])), .02)
            z[group] = (cosine[group] - means.get(mod, float(cosine[group].mean()))) / local
            qualifies[group] = (z[group] >= .95) & (cosine[group] >= .64)
        if image_query: qualifies |= cosine >= .85
        files_of = self.seg_file[rows]
        order = np.lexsort((-z, files_of))
        files, first = np.unique(files_of[order], return_index=True)
        best, folders = {}, {}
        keep = set(keep)
        for fid, index in zip(files.tolist(), first.tolist()):
            position = order[index]
            if not (qualifies[position] or fid in keep): continue  # only files that can matter reach the fusion
            entry = {'sid': int(self.ids[rows[position]]), 'z': float(z[position]), 'cosine': float(cosine[position]),
                     'qualifies': bool(qualifies[position])}
            if allowed_file(fid): best[fid] = entry
            if entry['qualifies'] and fid in self.files:
                parent = str(Path(self.files[fid][0]).parent)
                folder = folders.setdefault(parent, {'z': 0.0, 'count': 0, 'sid': entry['sid']})
                folder['count'] += 1
                if entry['z'] > folder['z']: folder.update(z=entry['z'], sid=entry['sid'])
        return best, folders

    def file_stats(self, path):
        """Qué sabe el índice de un archivo: fragmentos, tipos y si el análisis terminó."""
        with self.lock:
            row = self.db.execute('SELECT id,complete FROM files WHERE path=? AND error IS NULL', (str(path),)).fetchone()
            if not row: return None
            segments = self.db.execute('SELECT kind,count(*) FROM segments WHERE file=? GROUP BY kind', (row['id'],)).fetchall()
        return {'segments': sum(n for _, n in segments), 'kinds': [k for k, _ in segments], 'complete': bool(row['complete']),
                'indexed': None}

    def recent(self, limit=40):
        """Archivos modificados más recientemente: lo que aparece antes de escribir."""
        with self.lock:
            self.refresh()
            indexed = {info[0]: info[1] for info in self.files.values()}
            noise = {'.log', '.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg'}
            exts = sorted(set().union(*(CATEGORY_EXTS[c] for c in ('document', 'pdf', 'image', 'video', 'audio', 'sheet', 'slides'))) - noise)
            rows = self.db.execute(f"SELECT path,size,mtime FROM catalog WHERE is_dir=0 AND ext IN ({','.join('?' * len(exts))})"
                                   ' ORDER BY mtime DESC LIMIT ?', (*exts, limit * 3)).fetchall()
        hits = []
        for row in rows:
            if not Path(row['path']).is_file(): continue
            hits.append({'path': row['path'], 'name': Path(row['path']).name, 'kind': kind_of(row['path'], indexed.get(row['path'])),
                         'indexed': row['path'] in indexed, 'segment': None, 'loc': None, 'end': None, 'excerpt': '', 'score': 0,
                         'cosine': None, 'keyword': False, 'coverage': 0, 'meaning': False, 'name_match': 0, 'reasons': ['reciente'],
                         'mtime': row['mtime'], 'size': row['size'], 'copies': [], 'inferred_kind': None})
            if len(hits) >= limit: break
        return hits

    # ---------------------------------------------------------------- fusión
    def run(self, query='', limit=60, kind=None, lexical=False, image_path=None, semantic=True):
        began = time.perf_counter()
        query = (query or '').strip()[:1000]
        code = query.casefold().startswith('code:') or kind == 'code'
        if query.casefold().startswith('code:'): query = query[5:].strip()
        intent = parse(query)
        if kind: intent.categories = LEGACY_KINDS.get(kind, {kind}) if kind != 'code' else None
        if image_path and not intent.categories: intent.categories = {'image'}
        if (not query and not image_path) or (code and not query):
            return Results([], intent)
        terms = intent.terms
        with self.lock:
            self.refresh()
            files = self.files

            def allowed_file(fid):
                info = files.get(fid)
                if info is None or (info[1] == 'code') != code: return False
                return self.accepts(intent, info[0], info[4], info[2], info[3])

            names = {} if code else self.by_name(intent, terms)
            lexical_best, shares = self.by_words(terms, allowed_file)
            meaning, folders = {}, {}
            used_meaning = False
            text = intent.semantic if not code else query
            if semantic and not lexical and (text or image_path):
                vector = self.query_vector(text, code, image_path)
                keep = set(lexical_best) | {self.by_path[p] for p in names if p in self.by_path}
                meaning, folders = self.by_meaning(vector, code, allowed_file, bool(image_path), keep)
                used_meaning = True
            by_path = self.by_path
            term_stems = [stem(t) for t in terms]
            lookup = bool(terms) and all(any(c.isdigit() for c in t) for t in terms)
            now = time.time_ns()
            scored = {}

            def add(path, fid=None, catalog=None, folder=None):
                info = files.get(fid) if fid is not None else None
                is_dir = bool(catalog and catalog['is_dir']) or folder is not None
                mtime = info[2] if info else catalog['mtime'] if catalog else 0
                size = info[3] if info else catalog['size'] if catalog else 0
                found = meaning.get(fid)
                share = shares.get(fid, 0.0)
                name = catalog['share'] if catalog else 0.0
                if catalog is None and terms and not self.catalog_rows:
                    # No catalog yet (first minutes, tests): judge the name from the path itself.
                    name, _ = self.name_share(terms, term_stems, Path(path).stem.replace('_', ' ').replace('-', ' '), '')
                qualifies = bool(found and found['qualifies'])
                folder_z = folder['z'] + .35 * math.log(folder['count']) if folder else 0.0
                if not (qualifies or share >= .5 or name >= .5 or folder is not None
                        or (not terms and (catalog is not None or (found is None and info is not None)))):
                    return
                weight, word_weight, name_weight = (.5, 4, 6) if lookup else (1, 2, 6)
                score = weight * max(found['z'] if found else 0, 0) + word_weight * share ** 1.5 + name_weight * name ** 1.2
                score += .6 * folder_z
                if catalog and catalog['exact']: score += 1.0
                if name >= .9 and len(terms) >= 2: score += .5
                # Two or more of the phrase's words in a file name rarely happen by chance.
                if len(terms) >= 2 and name * len(terms) >= 1.8: score += 2.5
                if is_dir and name >= .9: score += .4
                if intent.soft_after and intent.soft_after <= mtime < intent.soft_before: score += .6
                age = max(now - mtime, 0) / 86400e9
                score += (.25 if (intent.recent or not terms) else .12) * math.exp(-age / 45)
                if not terms and (intent.recent or intent.filtered): score += 2 * math.exp(-age / 30)
                keyword = lexical_best.get(fid)
                use_meaning = bool(found and (found['qualifies'] or keyword is None))
                previous = scored.get(path)
                if previous and previous['score'] >= score: return
                scored[path] = {'score': score, 'fid': fid, 'is_dir': is_dir, 'mtime': mtime, 'size': size,
                                'fuzzy': bool(catalog and catalog.get('fuzzy')),
                                'found': found, 'keyword': keyword, 'share': share, 'name': name,
                                'use_meaning': use_meaning, 'folder': folder, 'indexed': info[1] if info else None}

            for path, entry in names.items():
                fid = by_path.get(path)
                if fid is not None and not allowed_file(fid): continue
                if fid is None and not self.accepts(intent, path, 'folder' if entry['is_dir'] else kind_of(path),
                                                    entry['mtime'], entry['size'], entry['is_dir']):
                    continue
                add(path, fid, entry)
            for fid in set(meaning) | set(lexical_best):
                info = files.get(fid)
                if info: add(info[0], fid, names.get(info[0]))
            if not terms and not names and (intent.filtered or intent.recent) and not used_meaning:
                for fid, info in files.items():
                    if allowed_file(fid): add(info[0], fid)
            if intent.categories == {'folder'}:
                for parent, folder in folders.items():
                    if folder['count'] >= 2 and self.accepts(intent, parent, 'folder', 0, 0, True):
                        add(parent, folder=folder)

            ranked = sorted(scored.items(), key=lambda item: (-item[1]['score'], item[0]))
            hits, seen = [], {}
            for path, entry in ranked:
                item = Path(path)
                if not (item.is_dir() if entry['is_dir'] else item.is_file()): continue
                # Copies and backup versions of one file collapse into a single result.
                original = None if entry['is_dir'] else seen.get(item.name)
                if original is not None and (original['size'] == entry['size']
                                             or abs(original['score'] - entry['score']) <= .02 * max(original['score'], 1)):
                    original['copies'].append(path)
                    continue
                hit = self.hit(path, entry, intent, terms, code)
                if not entry['is_dir']: seen.setdefault(item.name, hit)
                hits.append(hit)
                if len(hits) >= limit: break
        counts = {}
        for hit in hits: counts[hit['kind']] = counts.get(hit['kind'], 0) + 1
        return Results(hits, intent, used_meaning, time.perf_counter() - began, counts)

    def hit(self, path, entry, intent, terms, code):
        found, keyword = entry['found'], entry['keyword']
        sid = found['sid'] if entry['use_meaning'] else keyword[0] if keyword else None
        if entry['folder'] is not None: sid = None
        excerpt, loc, end, segment_kind = '', None, None, None
        if sid is not None:
            segment = self.db.execute('SELECT text,loc,end,kind FROM segments WHERE id=?', (sid,)).fetchone()
            if segment:
                excerpt = segment['text'][:800]
                if keyword and keyword[0] == sid: excerpt = keyword[1]
                loc, end, segment_kind = segment['loc'], segment['end'], segment['kind']
                # Media segments carry internal labels («Fotograma en 32 s»), not readable text.
                if segment_kind in {'image', 'name', 'video-frame', 'audio', 'pdf-image', 'doc-image'}: excerpt = ''
                # OCR on photos leaves stray letters («NS P»): show it only when it reads like text.
                if segment_kind == 'ocr' and len(re.findall(r'[^\W\d_]{3,}', excerpt)) < 3: excerpt = ''
                if code and loc is not None and terms:
                    declaration = re.compile(r'(?:def|class|function|func|fn)\s+(?:' + '|'.join(re.escape(w) for w in terms) + r')\b')
                    for offset, line in enumerate(segment['text'].splitlines()):
                        if declaration.search(line): loc += offset; break
        reasons = []
        if entry['name'] >= .5: reasons.append('nombre aproximado' if entry.get('fuzzy') else 'nombre')
        if keyword: reasons.append('palabras')
        if entry['use_meaning'] and found and found['qualifies']: reasons.append('significado')
        if entry['folder'] is not None: reasons.append(f"{entry['folder']['count']} archivos relacionados")
        if not reasons: reasons.append('filtros')
        kind = 'folder' if entry['is_dir'] else kind_of(path, entry['indexed'])
        return {'path': path, 'name': Path(path).name, 'kind': kind, 'indexed': entry['indexed'] is not None,
                'segment': segment_kind, 'loc': loc, 'end': end, 'excerpt': excerpt, 'score': entry['score'],
                'cosine': found['cosine'] if found else None, 'keyword': bool(keyword), 'coverage': entry['share'],
                'meaning': bool(entry['use_meaning'] and found), 'name_match': entry['name'], 'reasons': reasons,
                'mtime': entry['mtime'], 'size': entry['size'], 'copies': [], 'inferred_kind': intent.kind_word}


_searchers = {}
_guard = threading.Lock()


def searcher(db_path=None, engine=None):
    key = str(db_path) if db_path else None
    with _guard:
        current = _searchers.get(key)
        if current is None:
            current = _searchers[key] = Searcher(db_path, engine)
        elif engine is not None:
            current.engine = engine
        return current


def search(query='', limit=30, kind=None, lexical=False, db_path=None, engine=None, image_path=None):
    """API estable para la CLI y las pruebas: devuelve solo la lista de resultados."""
    return searcher(db_path, engine).run(query, limit, kind, lexical, image_path).hits
