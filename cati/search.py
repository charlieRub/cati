"""Búsqueda instantánea: nombre, palabras y significado fusionados en una sola lista.

Los vectores viven en memoria (float16) y se actualizan por incrementos cuando el
indexador publica cambios, así que una consulta solo cuesta un producto matricial.
"""
import fcntl
import math
import os
from difflib import SequenceMatcher
import re
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .catalog import ARCHIVES, EXTRA_CODE, EXTRA_IMAGES, OFFICE, SHEETS, SLIDES, category
from .engine import Engine
from .query import STOP, Intent, normalize, parse, stem
from .spelling import Holder
from .readers import AUDIO, CODE, IMAGES, TEXT, VIDEO, image
from .config import DATA, give_back_memory
from .store import connect
from . import usage

CANDIDATES, SAMPLE = 10000, 4000
MODALITY = {'audio': 2, 'image': 1, 'pdf-image': 1, 'video-frame': 1, 'doc-image': 1}
CATEGORY_EXTS = {'document': (TEXT | OFFICE) - SHEETS - SLIDES, 'pdf': {'.pdf'}, 'image': IMAGES | EXTRA_IMAGES, 'video': VIDEO,
                 'audio': AUDIO, 'sheet': SHEETS, 'slides': SLIDES, 'code': CODE | EXTRA_CODE, 'archive': ARCHIVES}
LEGACY_KINDS = {'documents': {'document', 'pdf'}}


def mapped(source, rows):
    """Vectores en disco en solo lectura. La lectura anticipada del núcleo se deja activa: medido en
    frío, leer por bloques es más del doble de rápido que pedir solo las filas que puntúa la consulta."""
    return np.memmap(source, dtype=np.float16, mode='r', shape=(rows, 768))


@dataclass
class Results:
    hits: list
    intent: Intent
    semantic: bool = False
    elapsed: float = 0.0
    counts: dict = field(default_factory=dict)


PERIODS = {'today': 0, 'week': 7, 'month': 30, 'year': 365}
SIZES = {'small': (None, 1 << 20), 'big': (10 << 20, None), 'huge': (500 << 20, None)}


def apply_filters(intent, filters, now=None):
    """Filtros de la interfaz (carpeta, periodo, tamaño, tipo) sobre lo que ya entendió la frase."""
    now = now or time.time()
    if filters.get('folder'): intent.location = Path(filters['folder']).resolve()
    period = filters.get('period')
    if period in PERIODS:
        start = time.localtime(now)
        midnight = time.mktime((start.tm_year, start.tm_mon, start.tm_mday, 0, 0, 0, 0, 0, -1))
        since = midnight if period == 'today' else now - PERIODS[period] * 86400
        intent.after = max(intent.after or 0, int(since * 1e9))
    low, high = SIZES.get(filters.get('size'), (None, None))
    if low: intent.min_size = max(intent.min_size or 0, low)
    if high: intent.max_size = min(intent.max_size or high, high)
    if filters.get('kind'): intent.categories = {filters['kind']}
    return intent


def kind_of(path, indexed=None, is_dir=False):
    """Categoría estable para la interfaz; respeta el tipo con que se indexó."""
    if indexed == 'code': return 'code'
    return category(suffix_of(os.fspath(path)), is_dir)


def suffix_of(path):
    """Path(path).suffix.lower() sin construir un Path (se llama para cada archivo del índice)."""
    name = path.rpartition('/')[2]
    if not name or name == '.': return Path(path).suffix.lower()
    name = name.lstrip('.')
    i = name.rfind('.')
    return name[i:].lower() if i != -1 else ''


class FileTable:
    """Los archivos analizados, por id, en columnas. Un dict de tuplas (ruta, tipo, fecha, tamaño, categoría)
    y su inverso por ruta eran ~230 000 objetos pequeños (unos 25 MB con 47 000 archivos); así son unos 7.
    Se lee como aquel dict: `get(fid)` devuelve la misma tupla, y los filtros se evalúan sobre columnas."""

    def __init__(self, rows):
        from array import array
        ids, mtimes, sizes, kinds, suffixes, hashes, ends = (array('q') for _ in range(7))
        blob, kind_codes, suffix_codes = bytearray(), {}, {}
        for fid, path, kind, mtime, size in rows:
            ids.append(fid); mtimes.append(mtime); sizes.append(size); hashes.append(hash(path))
            kinds.append(kind_codes.setdefault(kind, len(kind_codes)))
            suffix = suffix_of(path)
            suffixes.append(suffix_codes.setdefault(suffix, len(suffix_codes)))
            blob += path.encode('utf-8', 'surrogatepass'); ends.append(len(blob))
        self.kinds = list(kind_codes)
        self.suffixes = list(suffix_codes)
        self.categories = [category(s) for s in self.suffixes]
        self.blob = bytes(blob)
        self.ids = np.frombuffer(ids, np.int64)
        self.mtime, self.size = np.frombuffer(mtimes, np.int64), np.frombuffer(sizes, np.int64)
        self.kind, self.suffix = np.frombuffer(kinds, np.int64).astype(np.int16), np.frombuffer(suffixes, np.int64).astype(np.int32)
        self.ends = np.frombuffer(ends, np.int64)
        self.starts = np.concatenate([np.zeros(1, np.int64), self.ends[:-1]])
        self.row = np.full(int(self.ids.max(initial=0)) + 1, -1, np.int32)
        self.row[self.ids] = np.arange(len(self.ids), dtype=np.int32)
        hashes = np.frombuffer(hashes, np.int64)
        self.by_hash = np.argsort(hashes, kind='stable').astype(np.int32)
        self.hashes = hashes[self.by_hash]
        # memoryview indexing returns plain ints, several times faster than numpy scalars in Python loops.
        self._row, self._mtime, self._size = memoryview(self.row), memoryview(self.mtime), memoryview(self.size)
        self._kind, self._suffix = memoryview(self.kind), memoryview(self.suffix)
        self._starts, self._ends = memoryview(self.starts), memoryview(self.ends)

    def __len__(self):
        return len(self.ids)

    def __iter__(self):
        return iter(self.ids.tolist())

    def __contains__(self, fid):
        return self.get(fid) is not None

    def __getitem__(self, fid):
        info = self.get(fid)
        if info is None: raise KeyError(fid)
        return info

    def position(self, fid):
        return self._row[fid] if 0 <= fid < len(self._row) else -1

    def path_at(self, i):
        return self.blob[self._starts[i]:self._ends[i]].decode('utf-8', 'surrogatepass')

    def get(self, fid, default=None):
        """(ruta, tipo indexado, mtime, tamaño, categoría visible), como el dict de antes."""
        i = self.position(fid)
        if i < 0: return default
        kind = self.kinds[self._kind[i]]
        return (self.path_at(i), kind, self._mtime[i], self._size[i],
                'code' if kind == 'code' else self.categories[self._suffix[i]])

    def items(self):
        for i, fid in enumerate(self.ids.tolist()): yield fid, self.get(fid)

    def values(self):
        for fid in self.ids.tolist(): yield self.get(fid)

    def lookup(self, paths):
        """{ruta: id} de las rutas que están en el índice (una búsqueda vectorizada por hash)."""
        paths = list(paths)
        if not paths or not len(self.hashes): return {}
        wanted = np.fromiter(map(hash, paths), np.int64, len(paths))
        places = np.searchsorted(self.hashes, wanted)
        found = {}
        for path, h, place in zip(paths, wanted.tolist(), places.tolist()):
            while place < len(self.hashes) and self.hashes[place] == h:
                i = int(self.by_hash[place])
                if self.path_at(i) == path: found[path] = int(self.ids[i]); break
                place += 1
        return found

    def code_mask(self):
        mask = np.zeros(len(self.row), bool)
        if 'code' in self.kinds: mask[self.ids[self.kind == self.kinds.index('code')]] = True
        return mask


class Admitted:
    """`allowed_file` de una consulta: qué archivos analizados pasan sus filtros. Las condiciones numéricas
    (tipo, extensión, tamaño, fecha) se evalúan de una vez sobre las columnas; las que miran el texto de la
    ruta (lugar, palabras excluidas, fecha en el nombre) las decide `accepts`, archivo a archivo y solo la
    primera vez. El resultado es el mismo que llamar a `accepts` con cada archivo."""
    UNKNOWN = 2

    def __init__(self, searcher, intent, code, births):
        table = searcher.files
        self.searcher, self.intent, self.births, self.table = searcher, intent, births, table
        is_code = table.kind == table.kinds.index('code') if 'code' in table.kinds else np.zeros(len(table), bool)
        keep = is_code.copy() if code else ~is_code

        def per_suffix(test):
            """Evalúa `test(extensión, categoría)` una vez por extensión distinta (hay pocas) y lo reparte a cada archivo."""
            answers = np.array([test(s, c) for s, c in zip(table.suffixes, table.categories)] + [False], bool)
            return answers[table.suffix]

        def visible_in(categories):
            # Same category accepts() sees: indexed code is «code» whatever its extension.
            return np.where(is_code, 'code' in categories, per_suffix(lambda s, c: c in categories))
        if intent.categories: keep &= visible_in(intent.categories)
        if intent.excluded_categories: keep &= ~visible_in(intent.excluded_categories)
        if intent.exts: keep &= per_suffix(lambda s, c: s in intent.exts)
        if intent.excluded_exts: keep &= ~per_suffix(lambda s, c: s in intent.excluded_exts)
        if intent.min_size: keep &= table.size >= intent.min_size
        if intent.max_size: keep &= table.size <= intent.max_size
        dates = bool(intent.after or intent.before)
        loose_dates = dates and bool(intent.date_words or intent.date_field == 'created')
        if dates and not loose_dates:
            if intent.after: keep &= table.mtime >= intent.after
            if intent.before: keep &= table.mtime < intent.before
        textual = bool(intent.excluded_terms or intent.location or intent.location_word or loose_dates)
        state = np.zeros(len(table.row), np.uint8)
        state[table.ids[keep]] = self.UNKNOWN if textual else 1
        self.state = bytearray(state.tobytes())
        self.textual = textual

    def decide(self, fid):
        info = self.table.get(fid)
        ok = self.searcher.accepts(self.intent, info[0], info[4], info[2], info[3], btime=self.births.get(fid))
        self.state[fid] = 1 if ok else 0
        return ok

    def __call__(self, fid):
        if not 0 <= fid < len(self.state): return False
        value = self.state[fid]
        return value == 1 or (value == self.UNKNOWN and self.decide(fid))

    def accepted(self):
        """Ids aceptados, en orden."""
        state = np.frombuffer(self.state, np.uint8)
        if self.textual:
            for fid in np.flatnonzero(state == self.UNKNOWN).tolist(): self.decide(fid)
            state = np.frombuffer(self.state, np.uint8)
        return np.flatnonzero(state == 1)


class Searcher:
    def __init__(self, db_path=None, engine=None):
        self.db_path = db_path
        self.engine = engine
        self.db = connect(db_path, threads=True)
        self.lock = threading.RLock()
        self.version = None
        self.vector_version = None
        self.last_id = 0
        self.size = 0
        self.columns = {}
        self.allocate(0)
        self.catalog_rows = 0
        self.catalog_rows_total = 0
        self.births = None
        self.files = FileTable(())
        self.file_code = np.zeros(1, bool)
        self.background_refresh = False  # the window turns it on; the CLI and tests see every change at once
        self.rebuilding = None
        self.vectors = OrderedDict()
        self.spelling = Holder(db_path)
        self.name_counts = OrderedDict()
        self.name_results = OrderedDict()
        self.name_cache_version = None

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
        """Un solo proceso mantiene la caché compartida; los demás (p. ej. «cati search») la leen sin copiarla."""
        if not hasattr(self, '_owner'):
            base = Path(self.db_path).with_suffix('.vectors.lock') if self.db_path else DATA / 'vectors.lock'
            self._lock = open(base, 'a')
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._owner = True
                self.sweep(base.parent)
            except BlockingIOError:
                self._owner = False
        return self._owner

    @staticmethod
    def sweep(folder, age=600):
        """Borra copias privadas con nombre que dejaron versiones anteriores al morir sin limpiar."""
        limit = time.time() - age
        for stale in folder.glob('cati-vectors-*'):
            try:
                if stale.stat().st_mtime < limit: stale.unlink()
            except OSError: pass

    def vectors_path(self):
        """Archivo de vectores del índice; solo lo escribe el proceso dueño de la caché."""
        return Path(self.db_path).with_suffix('.vectors.f16') if self.db_path else DATA / 'vectors.f16'

    def snapshot_path(self):
        return Path(self.db_path).with_suffix('.vectors.npz') if self.db_path else DATA / 'vectors.npz'

    def matrix(self):
        if self.full is None and self.size:
            self.full = mapped(self.vectors_path(), self.size)
        return self.full

    def private(self):
        """Filas que el proceso no dueño añade a la caché compartida: archivo anónimo (O_TMPFILE),
        así el sistema lo libera aunque el proceso muera de golpe."""
        if getattr(self, '_private', None) is None:
            self._private = tempfile.TemporaryFile(prefix='cati-vectors-', dir=self.vectors_path().parent)
        return self._private

    def append(self, rows):
        count = len(rows)
        if self.size + count > len(self.columns['ids']):
            self.allocate(max(4096, 2 * len(self.columns['ids']), self.size + count))  # amortised: no copy per refresh
        block = np.frombuffer(b''.join(r[4] for r in rows), dtype='<f4').reshape(count, 768)
        data = block.astype(np.float16).tobytes()
        if self.owner():
            path = self.vectors_path()
            if self.size == 0:
                # A fresh cache gets a new file: readers that mapped the old one keep valid pages.
                fresh = path.with_suffix('.new')
                fresh.write_bytes(b'')
                fresh.chmod(0o600)
                fresh.replace(path)
            with open(path, 'ab') as out: out.write(data)
        else:
            handle = self.private()
            handle.seek((self.size - self.shared_rows) * 1536)
            handle.write(data)
            handle.flush()
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

    shared_rows, shared = 0, None

    def exact(self, rows, vector):
        """Coseno completo (768 dimensiones) leyendo del disco solo las filas pedidas, en orden."""
        if self.owner(): return self.matrix()[rows].astype(np.float32) @ vector
        rows = np.asarray(rows)
        result = np.empty(len(rows), np.float32)
        split = np.searchsorted(rows, self.shared_rows)  # rows arrive sorted
        if split: result[:split] = self.shared[rows[:split]].astype(np.float32) @ vector
        if split < len(rows):
            if self.full is None:
                self.full = mapped(self.private(), self.size - self.shared_rows)
            result[split:] = self.full[rows[split:] - self.shared_rows].astype(np.float32) @ vector
        return result

    def load_snapshot(self):
        """Arranque sin releer SQLite: metadatos y huellas desde la copia, vectores desde su archivo.
        Un proceso no dueño mapea en solo lectura el archivo del dueño y comprueba que corresponde a la copia."""
        owner = self.owner()
        try:
            with np.load(self.snapshot_path()) as data:
                if int(data['format']) != 2: raise ValueError
                size = len(data['ids'])
                stored = self.vectors_path().stat().st_size
                if int(data['dims']) != 768 or (stored != size * 1536 if owner else stored < size * 1536): raise ValueError
                self.allocate(max(4096, size))
                for name in self.LAYOUT: self.columns[name][:size] = data[name]
                self.size = size
                self.last_id = int(data['last_id'])
                self.saved = size
                self.views()
            newest = int(self.db.execute("SELECT value FROM meta WHERE key='segment_sequence'").fetchone()[0])
            if self.last_id > newest: raise ValueError  # index rebuilt since the copy was saved
            if not owner and size:
                shared = mapped(self.vectors_path(), size)
                # The owner may have compacted between both reads: the sign bits must still match.
                probe = np.unique(np.linspace(0, size - 1, 64).astype(np.int64))
                if not np.array_equal(np.packbits(shared[probe] > 0, axis=1), self.columns['bits'][probe]): raise ValueError
                self.shared, self.shared_rows = shared, size
        except (OSError, KeyError, ValueError):
            self.size, self.last_id = 0, 0
            self.saved = 0
            self.shared, self.shared_rows = None, 0
            self.allocate(0)

    def save_snapshot(self):
        if not self.owner(): return
        path = self.snapshot_path()
        partial = path.with_name(path.name + '.part.npz')
        try:
            np.savez(partial, format=2, last_id=self.last_id, dims=768, **{name: getattr(self, name) for name in self.LAYOUT})
            partial.chmod(0o600)
            partial.replace(path)
            self.saved = self.size
        except OSError:
            partial.unlink(missing_ok=True)

    def compact(self):
        """Reescribe sin los fragmentos borrados cuando ya son más de una cuarta parte."""
        keep = np.nonzero(self.alive)[0]
        matrix = self.matrix()
        temporary = self.vectors_path().with_suffix('.tmp')
        with open(temporary, 'wb') as output:
            for offset in range(0, len(keep), 2048):
                output.write(matrix[keep[offset:offset + 2048]].tobytes())
        for name in self.LAYOUT: self.columns[name][:len(keep)] = self.columns[name][keep]
        self.size = len(keep)
        temporary.chmod(0o600)
        temporary.replace(self.vectors_path())
        self.views()
        self.save_snapshot()

    def refresh_vectors(self, version):
        if version == self.vector_version: return
        if self.vector_version is None: self.load_snapshot()
        # Bound peak memory even when no snapshot exists for a large index.
        cursor = self.db.execute('SELECT id,file,kind,sets_scale,vec FROM segments WHERE id>? ORDER BY id', (self.last_id,))
        added = False
        while rows := cursor.fetchmany(2048):
            self.append(rows)
            self.last_id = int(self.ids[-1])
            added = True
        count = self.db.execute('SELECT count(*) FROM segments').fetchone()[0]
        if count != int(self.alive.sum()):
            live = np.fromiter((r[0] for r in self.db.execute('SELECT id FROM segments')), np.int64)
            self.columns['alive'][:self.size] = np.isin(self.ids, live)
            self.views()
            if self.alive.sum() < .75 * self.size and self.owner(): self.compact()
        # Keep the copy on disk close to the index: rewrite after every few thousand new fragments.
        if self.size - getattr(self, 'saved', 0) >= 4000 or (added and getattr(self, 'saved', None) is None):
            self.save_snapshot()
        self.vector_version = version

    def refresh(self, vectors=True):
        version = self.db.execute('PRAGMA data_version').fetchone()[0]
        if vectors: self.refresh_vectors(version)
        if version == self.version: return
        if self.background_refresh and self.version is not None:
            # While the watcher commits every few seconds, a search keeps the current table
            # and the new one is built beside it, off the typing path.
            if self.rebuilding is None or not self.rebuilding.is_alive():
                self.rebuilding = threading.Thread(target=self.rebuild, name='cati-files', daemon=True)
                self.rebuilding.start()
            return
        self.apply(*self.load(self.db), version)

    @staticmethod
    def load(db):
        files = FileTable(db.execute('SELECT id,path,kind,mtime,size FROM files WHERE error IS NULL ORDER BY id'))
        rows = db.execute('SELECT count(*) FROM (SELECT 1 FROM catalog LIMIT 1)').fetchone()[0]
        total = db.execute('SELECT max(id) FROM catalog').fetchone()[0] or 0
        return files, rows, total

    def apply(self, files, rows, total, version):
        self.files, self.catalog_rows, self.catalog_rows_total = files, rows, total
        self.births = None
        self.file_code = files.code_mask()
        self.version = version

    def rebuild(self):
        try:
            # data_version only compares within one connection: note ours before reading, so the table
            # is at least that new, and a commit made meanwhile triggers another rebuild.
            with self.lock: version = self.db.execute('PRAGMA data_version').fetchone()[0]
            db = connect(self.db_path, threads=True)
            try: loaded = self.load(db)
            finally: db.close()
            with self.lock: self.apply(*loaded, version)
        except Exception:
            with self.lock: self.version = None  # the next search rebuilds in the foreground

    def query_vector(self, text, code, image_path):
        if image_path:
            path = Path(image_path).resolve()
            stat = path.stat()
            key = ('image', str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        else:
            key = (text, code)
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
        """Prepara nombres y vocabulario sin bloquear la búsqueda rápida con el modelo."""
        with self.lock: self.refresh(vectors=False)
        self.spelling.get()

    def release_vectors(self):
        """Drop the heavy search working set; persistent index and query embeddings survive."""
        with self.lock:
            if self.vector_version is None: return
            if self.size != getattr(self, 'saved', None): self.save_snapshot()
            self.full = None
            self.shared, self.shared_rows = None, 0
            if getattr(self, '_private', None) is not None: self._private.close(); self._private = None
            self.size = self.last_id = 0
            self.columns = {}
            self.allocate(0)
            self.vector_version = None
        give_back_memory()

    def close(self):
        self.release_vectors()
        if getattr(self, '_lock', None) is not None: self._lock.close()
        self.db.close()

    # --------------------------------------------------------------- filtros
    @staticmethod
    def dated(intent, mtime, btime, after, before):
        """¿Cae en el periodo? La fecha de creación cuenta solo si la frase la nombra («que creé», «descargado»):
        una foto copiada del móvil se creó hoy, pero se hizo cuando dice su fecha de modificación."""
        stamp = (btime or mtime) if intent.date_field == 'created' else mtime
        return (not after or stamp >= after) and (not before or stamp < before)

    def accepts(self, intent, path, kind, mtime, size, is_dir=False, btime=None):
        if intent.categories and kind not in intent.categories: return False
        if intent.excluded_categories and kind in intent.excluded_categories: return False
        if intent.exts or intent.excluded_exts:
            suffix = Path(path).suffix.lower()
            if intent.exts and (is_dir or suffix not in intent.exts): return False
            if intent.excluded_exts and suffix in intent.excluded_exts: return False
        if intent.excluded_terms and any(stem(t) in normalize(os.path.basename(path)) for t in intent.excluded_terms): return False
        if (intent.after or intent.before) and not self.dated(intent, mtime, btime, intent.after, intent.before):
            # «rentabilidad agosto.xlsx» answers «de agosto» even if it was saved in September.
            if not (intent.date_words and any(w in normalize(os.path.basename(path)) for w in intent.date_words)): return False
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
        if intent.excluded_exts:
            clauses.append(f"c.ext NOT IN ({','.join('?' * len(intent.excluded_exts))})"); values += sorted(intent.excluded_exts)
        if intent.excluded_categories:
            exts = set().union(*(CATEGORY_EXTS.get(c, set()) for c in intent.excluded_categories))
            if 'folder' in intent.excluded_categories: clauses.append('c.is_dir=0')
            if exts: clauses.append(f"(c.is_dir=1 OR c.ext NOT IN ({','.join('?' * len(exts))}))"); values += sorted(exts)
        if intent.after or intent.before:
            fields = ['COALESCE(c.btime,c.mtime)' if intent.date_field == 'created' else 'c.mtime']
            options = []
            for column in fields:
                parts = ([f'{column}>=?'] if intent.after else []) + ([f'{column}<?'] if intent.before else [])
                options.append('(' + ' AND '.join(parts) + ')')
                values += [v for v in (intent.after, intent.before) if v]
            for word in sorted(intent.date_words):
                options.append('c.name LIKE ?'); values.append(f'%{word}%')
            clauses.append('(' + ' OR '.join(options) + ')')
        if intent.min_size: clauses.append('c.size>=?'); values.append(intent.min_size)
        if intent.max_size: clauses.append('c.size<=?'); values.append(intent.max_size)
        if intent.location:
            root = str(intent.location)
            clauses.append('c.path>=? AND c.path<?'); values += [root + '/', root + '0']
        return clauses, values

    # ---------------------------------------------------------------- nombre
    def birth_times(self):
        """Fecha de creación de los archivos analizados; solo se lee si una frase la pide («que creé…»)."""
        if self.births is None:
            self.births = {fid: btime for fid, btime in self.db.execute(
                'SELECT f.id,c.btime FROM files f JOIN catalog c ON c.path=f.path WHERE f.error IS NULL') if btime}
        return self.births

    def named_together(self, word, others):
        """¿Hay algún nombre con `word` y todas las otras palabras de la frase?"""
        quoted = lambda w: '"' + w.replace('"', '') + '"'
        others = [stem(w) for w in others if w not in STOP]  # «factura del dentista»: «del» says nothing
        if not others: return False
        match = 'name:(' + ' AND '.join(quoted(w) for w in [word, *others]) + ')'
        try: return self.db.execute('SELECT 1 FROM catalog_names WHERE catalog_names MATCH ? LIMIT 1', (match,)).fetchone() is not None
        except sqlite3.Error: return False

    def rarity(self, term):
        """En cuántos archivos aparece la palabra (nombre o contenido); None si no lo sabemos."""
        lexicon = self.spelling.lexicon
        if lexicon is None or any(c.isdigit() for c in term): return None
        return max(lexicon.frequency(term) + lexicon.documents(term), lexicon.frequency(stem(term)) + lexicon.documents(stem(term)))

    def weights(self, terms):
        """Peso de cada palabra según lo rara que es: en «viaje a albania» manda «albania».
        Una palabra que no aparece en ningún archivo no distingue entre ellos y pesa lo normal."""
        if self.spelling.lexicon is None or not terms: return None
        total = max(1, self.catalog_rows_total)
        result = []
        for term in terms:
            frequency = self.rarity(term)
            result.append(1.0 if not frequency else 1 + math.log((total + 1) / (frequency + 1)) / 4)
        return result

    def by_name(self, intent, terms, limit=3000):
        clauses, values = self.catalog_sql(intent)
        # Share candidate work between the immediate and semantic passes. Bound
        # the cache by rows, and invalidate on commits from the watcher or us.
        version = (self.db.execute('PRAGMA data_version').fetchone()[0], self.db.total_changes)
        if version != self.name_cache_version:
            self.name_results.clear()
            self.name_cache_version = version
        key = (tuple(clauses), tuple(values), tuple(terms), tuple(intent.hints), limit,
               intent.filtered, intent.recent, intent.date_field,
               id(self.spelling.lexicon), self.catalog_rows_total)
        if key in self.name_results:
            self.name_results.move_to_end(key)
            return self.name_results[key]
        stems = [stem(t) for t in terms]
        weights = self.weights(terms)
        long = [s for s in stems if len(s) >= 3]
        short = [s for s in stems if len(s) < 3]
        where = ' AND '.join(clauses)
        if long:
            quoted = ['"' + s.replace('"', '') + '"' for s in long]
            sql = ('SELECT c.* FROM catalog_names JOIN catalog c ON c.id=catalog_names.rowid WHERE catalog_names MATCH ?'
                   + (' AND ' + where if where else ''))
            # Reserve candidates for folder names and complete name matches.
            # Thousands of descendants with the same parent must not hide them.
            rows, selected = [], set()
            queries = [('name:(' + ' AND '.join(quoted) + ')', ' AND c.is_dir=1', min(256, limit)),
                       ('name:(' + ' AND '.join(quoted) + ')', '', limit),
                       ('name:(' + ' OR '.join(quoted) + ')', '', limit),
                       (' OR '.join(quoted), '', limit)]
            for match, extra, budget in queries:
                remaining = min(budget, limit - len(rows))
                if remaining <= 0: break
                # Include the already-selected rows in the SQL budget so they
                # cannot consume the remaining candidate slots a second time.
                for row in self.db.execute(sql + extra + ' ORDER BY rank LIMIT ?',
                                           [match, *values, remaining + len(rows)]):
                    if row['id'] in selected: continue
                    rows.append(row); selected.add(row['id'])
                    remaining -= 1
                    if remaining <= 0: break
        elif short:
            like = ' AND '.join('c.name LIKE ?' for _ in short)
            sql = 'SELECT c.* FROM catalog c WHERE ' + like + (' AND ' + where if where else '') + ' LIMIT ?'
            rows = self.db.execute(sql, [*('%' + s + '%' for s in short), *values, limit]).fetchall()
        else:
            if not intent.filtered and not intent.recent: return {}
            order = 'COALESCE(c.btime,c.mtime)' if intent.date_field == 'created' else 'c.mtime'
            sql = 'SELECT c.* FROM catalog c' + (' WHERE ' + where if where else '') + f' ORDER BY {order} DESC LIMIT ?'
            rows = self.db.execute(sql, [*values, 400]).fetchall()
        found = {}
        for row in rows:
            share, exact = self.name_share(terms, stems, row['name'], row['folders'], weights=weights, hints=intent.hints)
            if terms and share <= 0: continue
            found[row['path']] = {'share': share, 'exact': exact, 'is_dir': bool(row['is_dir']), 'name': row['name'],
                                  'mtime': row['mtime'], 'size': row['size'], 'btime': row['btime'], 'fuzzy': False}
        # Typos: a long word with no exact hit looks for names sharing one of its 4-letter pieces
        # (selective in the trigram index, so it stays fast), then keeps only close spellings.
        missing = [s for s in stems if len(s) >= 5 and not any(s in normalize(r['name']) for r in rows[:300])]
        if missing:
            for path, entry in found.items():
                share, _ = self.name_share(terms, stems, entry['name'], '', fuzzy=True, weights=weights)
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
                share, exact = self.name_share(terms, stems, row['name'], row['folders'], fuzzy=True, weights=weights)
                if share >= .5:
                    found[row['path']] = {'share': share * .85, 'exact': False, 'is_dir': bool(row['is_dir']), 'name': row['name'],
                                          'mtime': row['mtime'], 'size': row['size'], 'btime': row['btime'], 'fuzzy': True}
        if len(found) <= 6000:
            self.name_results[key] = found
            while len(self.name_results) > 16 or sum(map(len, self.name_results.values())) > 6000:
                self.name_results.popitem(last=False)
        return found

    @staticmethod
    def name_share(terms, stems, name, folders, fuzzy=False, weights=None, hints=()):
        if not terms: return 0.0, False
        name_n = normalize(name)
        tokens = re.findall(r'\w+', name_n)
        folder_tokens = re.findall(r'\w+', normalize(folders))
        weights = weights or [1.0] * len(terms)
        total = 0.0
        for term, root, weight in zip(terms, stems, weights):
            before = total
            if term in tokens: total += 1.0
            elif any(t.startswith(root) for t in tokens): total += .9
            elif len(root) >= 3 and root in name_n: total += .7
            elif any(t.startswith(root) for t in folder_tokens): total += .45
            elif fuzzy and len(root) >= 5:
                # «piramdes» ≈ «piramides»: similarity of the closest word, prefixes included.
                best = max((SequenceMatcher(None, root, t[:len(root) + 2]).ratio() for t in tokens
                            if len(t) >= 4 and t[0] == root[0] and abs(len(t) - len(root)) <= 4), default=0)
                if best >= .8: total += .7 * best
            total = before + (total - before) * weight
        joined = ' '.join(terms)
        stem_part = name_n.rsplit(' ', 1)[0] if ' ' in name_n else name_n
        exact = joined == stem_part or joined == name_n
        # «últimos movimientos»: a word the parser took as a filter still counts when the name has it.
        extra = sum(1.0 for h in hints if h in tokens) * (sum(weights) / len(weights))
        return (total + extra) / (sum(weights) + extra), exact

    # -------------------------------------------------------------- palabras
    def by_words(self, terms, allowed_file, weights=None):
        """Mejor fragmento por archivo, qué parte (ponderada) de las palabras contiene y cuáles."""
        if not terms: return {}, {}, {}
        patterns = []
        for term in terms:
            root = stem(term)
            patterns.append('"' + root.replace('"', '') + '"' + ('*' if root != term else ''))
        match = '{text}: (' + ' OR '.join(patterns) + ')'
        # The excerpt (snippet() was 3/4 of this query) is cut later, only for the results shown: see excerpt().
        rows = self.db.execute('''SELECT words.rowid AS sid,s.file FROM words JOIN segments s ON s.id=words.rowid
            WHERE words MATCH ? ORDER BY bm25(words,3,1) LIMIT 1000''', (match,)).fetchall()
        best = {}
        for sid, fid in rows:
            if fid in best or not allowed_file(fid): continue
            best[fid] = (sid, match)
        weights = weights or [1.0] * len(terms)
        coverage, matched = {fid: 0.0 for fid in best}, {fid: set() for fid in best}
        if best:
            ids = ','.join(str(f) for f in best)
            for term, pattern, weight in zip(terms, patterns, weights):
                for (fid,) in self.db.execute(f'''SELECT DISTINCT s.file FROM words JOIN segments s ON s.id=words.rowid
                        WHERE words MATCH ? AND s.file IN ({ids})''', ('{text}: ' + pattern,)):
                    coverage[fid] += weight
                    matched[fid].add(term)
        return best, {fid: n / sum(weights) for fid, n in coverage.items()}, matched

    def excerpt(self, match, sid):
        """Fragmento con las palabras resaltadas de un resultado de by_words."""
        row = self.db.execute("SELECT snippet(words,1,'«','»','…',36) FROM words WHERE words MATCH ? AND rowid=?",
                              (match, sid)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------ significado
    def by_meaning(self, vector, code, allowed_file, image_query, keep=(), scoped=False):
        """Mejor fragmento por archivo, con puntuación z calibrada por modalidad.

        Pasada gruesa por distancia Hamming sobre las huellas binarias de todo el índice y coseno
        exacto solo para los candidatos, una muestra fija para la calibración y los archivos de `keep`."""
        if not self.size: return {}, {}
        base = self.alive & (self.file_code[np.minimum(self.seg_file, len(self.file_code) - 1)] == code)
        indices = np.nonzero(base)[0]
        if not len(indices): return {}, {}
        vector = vector.astype(np.float32)
        # Popcount on 64-bit words (12 per code, not 96 bytes) and no copy when every row takes part.
        codes = self.bits if len(indices) == len(self.bits) else self.bits[indices]
        distance = np.bitwise_count(np.ascontiguousarray(codes).view(np.uint64) ^ np.packbits(vector > 0).view(np.uint64)).sum(
            axis=1, dtype=np.uint16)
        candidates, distances = indices, distance
        if scoped:
            accepted = (allowed_file.accepted() if hasattr(allowed_file, 'accepted')
                        else np.fromiter((fid for fid in self.files if allowed_file(fid)), np.int64))
            mask = np.isin(self.seg_file[indices], accepted)
            candidates, distances = indices[mask], distance[mask]
        wanted = min(CANDIDATES, max(3000, len(candidates) // 15))
        top = candidates[np.argpartition(distances, wanted)[:wanted]] if len(candidates) > wanted else candidates
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
        positions = order[first]
        # Only files that can matter reach the fusion: a confident passage or one the other searches found.
        chosen = qualifies[positions]
        if keep: chosen |= np.isin(files, np.fromiter(keep, np.int64, len(keep)))
        for fid, position in zip(files[chosen].tolist(), positions[chosen].tolist()):
            entry = {'sid': int(self.ids[rows[position]]), 'z': float(z[position]), 'cosine': float(cosine[position]),
                     'qualifies': bool(qualifies[position])}
            if allowed_file(fid): best[fid] = entry
            info = self.files.get(fid) if entry['qualifies'] else None
            if info is not None:
                parent = os.path.dirname(info[0]) or '.'
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

    def covers(self, folder):
        """¿Recorre el índice esta carpeta? Si no, solo se puede buscar mirando el disco."""
        root = str(folder).rstrip('/')
        with self.lock:
            return self.db.execute('SELECT 1 FROM catalog WHERE path>=? AND path<? LIMIT 1', (root + '/', root + '0')).fetchone() is not None

    def recent(self, limit=40):
        """Archivos modificados más recientemente: lo que aparece antes de escribir."""
        with self.lock:
            self.refresh(vectors=False)
            noise = {'.log', '.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg'}
            exts = sorted(set().union(*(CATEGORY_EXTS[c] for c in ('document', 'pdf', 'image', 'video', 'audio', 'sheet', 'slides'))) - noise)
            rows = self.db.execute(f"SELECT path,size,mtime FROM catalog WHERE is_dir=0 AND ext IN ({','.join('?' * len(exts))})"
                                   ' ORDER BY mtime DESC LIMIT ?', (*exts, limit * 3)).fetchall()
            indexed = {path: self.files.get(fid)[1] for path, fid in self.files.lookup(r['path'] for r in rows).items()}
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
    def run(self, query='', limit=60, kind=None, lexical=False, image_path=None, semantic=True, exact=False, wait=False, filters=None):
        """`exact` busca la frase tal cual, sin corregir erratas; `wait` espera al vocabulario si aún se carga."""
        began = time.perf_counter()
        query = (query or '').strip()[:1000]
        code = query.casefold().startswith('code:') or kind == 'code'
        if query.casefold().startswith('code:'): query = query[5:].strip()
        intent = parse(query)
        lexicon = None if code or exact or not query else self.spelling.get(wait)
        if lexicon is not None:
            corrected, changes = lexicon.correct(query, self.named_together)
            if changes:
                intent = parse(corrected)
                intent.corrected, intent.original = corrected, query
        if kind: intent.categories = LEGACY_KINDS.get(kind, {kind}) if kind != 'code' else None
        if image_path and not intent.categories: intent.categories = {'image'}
        if filters: apply_filters(intent, filters)
        if (not query and not image_path and not filters) or (code and not query):
            return Results([], intent)
        if filters and filters.get('folder') and query and not image_path and not code and not self.covers(filters['folder']):
            # A folder the index does not walk: match names live so browsing there still filters.
            from . import browse
            hits = [h for h in browse.scan(filters['folder'], intent.terms or query.split())
                    if self.accepts(intent, h['path'], h['kind'], h['mtime'], h['size'], h['kind'] == 'folder')]
            return Results(hits, intent, True, time.perf_counter() - began, {})
        terms = intent.terms
        with self.lock:
            self.refresh(vectors=bool(semantic and not lexical and (intent.semantic or image_path or code)))
            files = self.files

            births = self.birth_times() if intent.date_field == 'created' else {}
            used = usage.counts(self.db)

            allowed_file = Admitted(self, intent, code, births)
            names = {} if code else self.by_name(intent, terms)
            name_fids = files.lookup(names)
            weights = self.weights(terms)
            lexical_best, shares, contains = self.by_words(terms, allowed_file, weights)
            # Rare, specific words («albania», «kawasaki»): files that have them should lead.
            rare = [t for t in terms if (self.rarity(t) or 0) and self.rarity(t) <= 40] if len(terms) >= 2 else []
            meaning, folders = {}, {}
            used_meaning = False
            text = intent.semantic if not code else query
            if semantic and not lexical and (text or image_path):
                vector = self.query_vector(text, code, image_path)
                keep = set(lexical_best) | set(name_fids.values())
                meaning, folders = self.by_meaning(vector, code, allowed_file, bool(image_path), keep,
                                                  scoped=bool(intent.filtered or intent.location_word or intent.excluded_terms))
                used_meaning = True
            term_stems = [stem(t) for t in terms]
            lookup = bool(terms) and all(any(c.isdigit() for c in t) for t in terms)
            now = time.time_ns()
            scored = {}

            def add(path, fid=None, catalog=None, folder=None):
                info = files.get(fid) if fid is not None else None
                is_dir = bool(catalog and catalog['is_dir']) or folder is not None
                mtime = info[2] if info else catalog['mtime'] if catalog else 0
                btime = births.get(fid) if info else (catalog.get('btime') or mtime) if catalog else mtime
                btime = btime or mtime
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
                shown = None
                if rare or intent.hints:
                    shown = normalize(catalog['name'] if catalog and 'name' in catalog else os.path.basename(path))
                if rare:
                    has = [t for t in rare if t in contains.get(fid, ()) or stem(t) in shown]
                    if has: score_rare = 2.0 * len(has) / len(rare)
                    else: score_rare, weight = 0.0, weight * .6
                else: score_rare = 0.0
                score = weight * max(found['z'] if found else 0, 0) + word_weight * share ** 1.5 + name_weight * name ** 1.2
                score += score_rare
                score += .6 * folder_z
                if catalog and catalog['exact']: score += 1.0
                if name >= .95 and len(terms) >= 2: score += 3.0  # every word of the phrase in the name
                # Two or more of the phrase's words in a file name rarely happen by chance.
                if len(terms) >= 2 and name * len(terms) >= 1.8: score += 2.5
                if is_dir and name >= .9: score += .4
                if intent.soft_after and self.dated(intent, mtime, btime, intent.soft_after, intent.soft_before): score += 1.0
                if intent.hints and (name > 0 or not terms):
                    # «rentabilidad de agosto» → «RENTABILIDAD AGOSTO.xlsx»: words the parser read as filters, also in the name.
                    score += .8 * sum(1 for h in intent.hints if h in shown) / len(intent.hints)
                score += min(.9, .3 * math.log1p(used.get(path, 0)))  # what you open often comes first among equals
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
                fid = name_fids.get(path)
                if fid is not None and not allowed_file(fid): continue
                if fid is None and not self.accepts(intent, path, 'folder' if entry['is_dir'] else kind_of(path),
                                                    entry['mtime'], entry['size'], entry['is_dir'], entry.get('btime')):
                    continue
                add(path, fid, entry)
            for fid in set(meaning) | set(lexical_best):
                info = files.get(fid)
                if info: add(info[0], fid, names.get(info[0]))
            if not terms and not names and (intent.filtered or intent.recent) and not used_meaning:
                for fid in allowed_file.accepted().tolist():
                    add(files.get(fid)[0], fid)
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
                if keyword and keyword[0] == sid: excerpt = self.excerpt(keyword[1], sid) or excerpt
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


def forget(db_path=None):
    """Cierra el buscador compartido de ese índice: su conexión, su cerrojo y sus vectores."""
    key = str(db_path) if db_path else None
    with _guard: current = _searchers.pop(key, None)
    if current is not None: current.close()


def search(query='', limit=30, kind=None, lexical=False, db_path=None, engine=None, image_path=None, exact=False):
    """API estable para la CLI y las pruebas: devuelve solo la lista de resultados."""
    return searcher(db_path, engine).run(query, limit, kind, lexical, image_path, exact=exact, wait=True).hits
