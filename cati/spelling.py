"""Corrige erratas de la consulta con el vocabulario de tus propios archivos.

Una palabra se corrige solo si no existe ni en tus archivos (nombres y contenido) ni en el diccionario
de español e inglés, así «gato» nunca se convierte en «dato». Los candidatos salen de un índice de
trigramas y se ordenan por una distancia de edición que abarata los errores humanos (letra omitida
o repetida, tecla vecina, dos letras cambiadas de orden, b/v, c/z, g/j, y/i) y, a igualdad, por lo
frecuentes que son en tus archivos y en el idioma de la frase.

El servicio de indexación construye el vocabulario y lo guarda en disco; la interfaz solo lo carga.
"""
import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter

import numpy as np

from .config import DATA
from .query import VOCABULARY, normalize

DICTIONARIES = {'es': 'es_50k.txt', 'en': 'en_50k.txt'}
STRONG, WEAK = 50, 5  # uses in the language corpus: a word that common is never a typo; a rare one, rarely
FILE = 'lexicon.npz'
REBUILD = 1800  # seconds between rebuilds while files keep changing
ROWS = ['qwertyuiop', 'asdfghjklñ', 'zxcvbnm']
NEAR = {}
for r, keys in enumerate(ROWS):
    for i, key in enumerate(keys):
        around = {keys[j] for j in (i - 1, i + 1) if 0 <= j < len(keys)}
        for other in (r - 1, r + 1):
            if 0 <= other < len(ROWS):
                around |= {ROWS[other][j] for j in (i - 1, i, i + 1) if 0 <= j < len(ROWS[other])}
        NEAR[key] = around
SOUNDS = [set('bv'), set('cszk'), set('gj'), set('yi'), set('nñ'), set('qk'), set('ui')]
WORD = re.compile(r'[^\W\d_]+')
SPANISH = set('de del la las el los que con para por una un unos unas mis sobre en y'.split())


def substitution(a, b):
    if a == b: return 0.0
    if any(a in group and b in group for group in SOUNDS): return .6
    if b in NEAR.get(a, ()): return .8
    return 1.8  # a far key is a different word («nefrología»), not a slip of the finger


def distance(a, b, limit):
    """Distancia de Damerau (alineamiento óptimo) con costes humanos; corta en cuanto supera `limit`.
    `a` es lo escrito y `b` el candidato: una letra que falta en `a` es la errata más barata."""
    if abs(len(a) - len(b)) > limit: return limit + 1
    previous2, previous = None, [j * .8 + (.5 if j else 0) for j in range(len(b) + 1)]
    for i in range(1, len(a) + 1):
        row = [i * 1.0 + .5] + [0.0] * len(b)
        for j in range(1, len(b) + 1):
            first = .5 if i == 1 or j == 1 else 0.0  # people rarely mistype the first letter
            near = (i > 1 and a[i - 1] in NEAR.get(a[i - 2], ())) or (i < len(a) and a[i - 1] in NEAR.get(a[i], ()))
            extra = previous[j] + (.5 if i > 1 and a[i - 1] == a[i - 2] else .95 if near else 1.0)
            missing = row[j - 1] + (.5 if j > 1 and b[j - 1] == b[j - 2] else .8)
            change = substitution(a[i - 1], b[j - 1])
            row[j] = min(extra + (first if j == 1 else 0), missing + (first if i == 1 else 0), previous[j - 1] + change + (first if change else 0))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], previous2[j - 2] + .75)
        if min(row) > limit: return limit + 1
        previous2, previous = previous, row
    return previous[-1]


def grams(word):
    padded = f'^{word}$'
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def allowance(word):
    return 1.0 if len(word) <= 4 else 1.6 if len(word) <= 6 else 2.0


def fingerprint(words):
    """Huellas estables de 8 bytes (no dependen del proceso, como hash())."""
    return np.fromiter((int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), 'little') for w in words),
                       np.uint64)


def contains(sorted_hashes, word):
    if sorted_hashes is None or not len(sorted_hashes): return False
    value = fingerprint([word])[0]
    i = np.searchsorted(sorted_hashes, value)
    return bool(i < len(sorted_hashes) and sorted_hashes[i] == value)


def prepare_dictionary(folder):
    """Convierte las listas completas de frecuencias (*_full.txt) en lo que Cati usa y borra los originales:
    las 50 000 palabras más usadas de cada idioma y las huellas de todas las palabras reales (unos 5 MB)."""
    counts = Counter()
    for language in DICTIONARIES:
        source = folder / f'{language}_full.txt'
        if not source.exists(): continue
        top = []
        with source.open(encoding='utf-8', errors='ignore') as handle:
            for line in handle:
                word, _, count = line.strip().partition(' ')
                if not count.isdigit() or not WORD.fullmatch(word): continue
                if len(top) < 50000: top.append(f'{word} {count}')
                counts[normalize(word)] += int(count)
        (folder / DICTIONARIES[language]).write_text('\n'.join(top) + '\n', encoding='utf-8')
        source.unlink()
    for name, low, high in (('strong.npy', STRONG, None), ('weak.npy', WEAK, STRONG)):
        chosen = [w for w, c in counts.items() if c >= low and (high is None or c < high)]
        np.save(folder / name, np.sort(fingerprint(chosen)))


def plausible(word):
    """Palabra de verdad y no un trozo de hash o de identificador: «cfbdd», «xkqzt»."""
    return 3 <= len(word) <= 24 and re.search(r'[aeiouy]', word) and not re.search(r'[^aeiouy]{5}', word)


class Lexicon:
    """Vocabulario de tu equipo: frecuencia en nombres, en contenido y en el idioma de cada palabra."""

    def __init__(self, words, name_df, content_df, spanish, english, keyword, display, gram_list, offsets, postings):
        self.strong, self.weak = (np.load(DATA / 'dict' / n) if (DATA / 'dict' / n).exists() else None
                                  for n in ('strong.npy', 'weak.npy'))
        self.words = words
        self.position = {w: i for i, w in enumerate(words)}
        self.name_df, self.content_df, self.spanish, self.english, self.keyword = name_df, content_df, spanish, english, keyword
        self.display = display
        self.gram_code = {g: i for i, g in enumerate(gram_list)}
        self.offsets, self.postings = offsets, postings
        self.lengths = np.fromiter((len(w) for w in words), np.int16, len(words))

    # ------------------------------------------------------------ construcción
    @classmethod
    def create(cls, names, content, languages, display):
        words = sorted(set(names) | set(content) | set().union(*languages.values()) | VOCABULARY)
        position = {w: i for i, w in enumerate(words)}
        size = len(words)
        arrays = {k: np.zeros(size, np.int32) for k in ('name_df', 'content_df', 'es', 'en')}
        for source, key in ((names, 'name_df'), (content, 'content_df'), (languages['es'], 'es'), (languages['en'], 'en')):
            target = arrays[key]
            for word, count in source.items(): target[position[word]] = min(count, 2 ** 31 - 1)
        keyword = np.zeros(size, bool)
        for word in VOCABULARY: keyword[position[word]] = True
        table, codes, owners = {}, [], []
        for i, word in enumerate(words):
            usable = keyword[i] or arrays['es'][i] or arrays['en'][i] or arrays['name_df'][i] or arrays['content_df'][i] >= 2
            if len(word) < 3 or not usable: continue
            for gram in grams(word):
                codes.append(table.setdefault(gram, len(table))); owners.append(i)
        codes = np.asarray(codes, np.int32)
        owners = np.asarray(owners, np.int32)
        order = np.argsort(codes, kind='stable')
        offsets = np.zeros(len(table) + 1, np.int64)
        offsets[1:] = np.cumsum(np.bincount(codes, minlength=len(table)))
        gram_list = [None] * len(table)
        for gram, code in table.items(): gram_list[code] = gram
        return cls(words, arrays['name_df'], arrays['content_df'], arrays['es'], arrays['en'], keyword, display,
                   gram_list, offsets, owners[order])

    def save(self, path):
        temporary = path.with_name(path.name + '.tmp.npz')
        pairs = sorted(self.display.items())
        np.savez(temporary, words='\n'.join(self.words), name_df=self.name_df, content_df=self.content_df,
                 spanish=self.spanish, english=self.english, keyword=self.keyword,
                 display_keys='\n'.join(k for k, _ in pairs), display_values='\n'.join(v for _, v in pairs),
                 grams='\n'.join(g for g in self.gram_code), offsets=self.offsets, postings=self.postings)
        os.replace(temporary, path)
        path.chmod(0o600)

    @classmethod
    def load(cls, path):
        with np.load(path) as data:
            split = lambda key: str(data[key]).split('\n') if str(data[key]) else []
            return cls(split('words'), data['name_df'], data['content_df'], data['spanish'], data['english'], data['keyword'],
                       dict(zip(split('display_keys'), split('display_values'))), split('grams'), data['offsets'], data['postings'])

    # ------------------------------------------------------------------ consulta
    def known(self, word):
        """2: palabra de verdad (tuya o del idioma); 1: rara en el idioma («nefrología»); 0: desconocida."""
        i = self.position.get(word)
        if i is not None and (self.keyword[i] or self.spanish[i] or self.english[i] or self.name_df[i] or self.content_df[i] >= 3):
            return 2
        if contains(self.strong, word): return 2
        return 1 if contains(self.weak, word) else 0

    def frequency(self, word):
        """Cuántos nombres de archivo contienen la palabra (para pesar los términos raros)."""
        i = self.position.get(word)
        return int(self.name_df[i]) if i is not None else 0

    def documents(self, word):
        """En cuántos archivos analizados aparece la palabra dentro del contenido."""
        i = self.position.get(word)
        return int(self.content_df[i]) if i is not None else 0

    def prior(self, i, spanish):
        language = math.log1p(self.spanish[i]) + .4 * math.log1p(self.english[i])
        if spanish and self.english[i] and not self.spanish[i] and not self.name_df[i]:
            language -= 4  # «copilto» in a Spanish phrase is «copiloto», not «copilot»
        return (1.5 * math.log1p(self.name_df[i]) + math.log1p(self.content_df[i]) + .3 * language
                + (4 if self.keyword[i] else 0))

    def suggest(self, word, spanish=True, rare=False):
        """Mejor corrección de una palabra desconocida, o None si nada se parece lo bastante.
        Con `rare` (la palabra existe pero es poco usada) solo vale una errata clara hacia algo de tus archivos."""
        limit = 1.0 if rare else allowance(word)
        codes = [self.gram_code[g] for g in grams(word) if g in self.gram_code]
        if not codes: return None
        shared = np.bincount(np.concatenate([self.postings[self.offsets[c]:self.offsets[c + 1]] for c in codes]),
                             minlength=len(self.words))
        close = np.nonzero((shared >= max(1, len(codes) - 3 * math.ceil(limit)))
                           & (np.abs(self.lengths - len(word)) <= math.ceil(limit)))[0]
        if len(close) > 80: close = close[np.argpartition(-shared[close], 80)[:80]]
        best, best_score = None, None
        for i in close.tolist():
            if len(word) <= 3 and not self.keyword[i]: continue  # three letters only fix the parser's own words
            if rare and not (self.keyword[i] or self.name_df[i] or self.content_df[i] >= 2): continue
            cost = distance(word, self.words[i], limit)
            if cost > limit: continue
            score = cost - .04 * self.prior(i, spanish)
            if best_score is None or score < best_score: best, best_score = self.words[i], score
        return best

    def correct(self, query):
        """(frase corregida, [(original, corrección)]). Respeta comillas, números, siglas, rutas y extensiones."""
        changes = []
        quoted = [m.span() for m in re.finditer(r'"[^"]*"|«[^»]*»', query)]
        spanish = bool(SPANISH & set(normalize(query).split())) or not re.search(r'\b(?:the|of|and|with|from)\b', query.lower())

        def replace(match):
            token = match.group(0)
            start, end = match.span()
            if any(a <= start < b for a, b in quoted): return token
            if start and query[start - 1] in '._-/\\': return token
            if end < len(query) and query[end] in '._-/\\': return token
            if len(token) >= 2 and token.isupper(): return token  # «ITU», «NAC»
            word = normalize(token)
            if len(word) < 3: return token
            known = self.known(word)
            if known == 2: return token
            fixed = self.suggest(word, spanish, rare=known == 1)
            if not fixed or fixed == word: return token
            shown = self.display.get(fixed, fixed)
            if token[:1].isupper(): shown = shown[:1].upper() + shown[1:]
            changes.append((token, shown))
            return shown
        return WORD.sub(replace, query), changes


def dictionary():
    """Palabras frecuentes de cada idioma (si el instalador las descargó) y su grafía más usada con tildes."""
    languages, forms = {'es': Counter(), 'en': Counter()}, {}
    for language, name in DICTIONARIES.items():
        path = DATA / 'dict' / name
        if not path.exists(): continue
        with path.open(encoding='utf-8', errors='ignore') as handle:
            for line in handle:
                word, _, count = line.strip().partition(' ')
                if len(word) < 3 or not WORD.fullmatch(word): continue
                count = int(count) if count.isdigit() else 1
                key = normalize(word)
                languages[language][key] += count
                if language == 'es' and (key not in forms or forms[key][1] < count): forms[key] = (word.lower(), count)
    return languages, {key: word for key, (word, _) in forms.items() if word != key}


def build(db_path=None):
    db = sqlite3.connect(db_path or DATA / 'index.sqlite3', timeout=30)
    try:
        names = Counter()
        for (name,) in db.execute('SELECT name FROM catalog'):
            names.update({w for w in WORD.findall(normalize(name)) if plausible(w)})
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocabulary USING fts5vocab(main, 'words', 'row')")
        content = {term: doc for term, doc in db.execute('SELECT term,doc FROM temp.vocabulary WHERE doc>=2')
                   if WORD.fullmatch(term) and plausible(term)}
    finally:
        db.close()
    languages, display = dictionary()
    return Lexicon.create(names, content, languages, display)


def location(db_path=None):
    return DATA / FILE if db_path is None else os.fspath(db_path).rsplit('.', 1)[0] + '.lexicon.npz'


def save_if_stale(db_path=None, every=REBUILD):
    """Lo llama el servicio de indexación tras cada exploración del disco."""
    from pathlib import Path
    path = Path(location(db_path))
    try:
        if path.exists() and time.time() - path.stat().st_mtime < every: return False
    except OSError: pass
    build(db_path).save(path)
    return True


class Holder:
    """Mantiene cargado el vocabulario más reciente; si nadie lo ha construido aún, lo construye una vez."""

    def __init__(self, db_path=None):
        from pathlib import Path
        self.path = Path(location(db_path))
        self.db_path = db_path
        self.lexicon = None
        self.loaded = 0.0
        self.checked = 0.0
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    def get(self, wait=False):
        now = time.monotonic()
        if now - self.checked > 30:
            self.checked = now
            with self.lock:
                if self.thread is None or not self.thread.is_alive():
                    try: stamp = self.path.stat().st_mtime
                    except OSError: stamp = None
                    if self.lexicon is None or (stamp and stamp > self.loaded):
                        self.thread = threading.Thread(target=self._load, args=(stamp,), daemon=True, name='cati-lexicon')
                        self.thread.start()
        if wait and self.lexicon is None:
            self.ready.wait(120)
            if self.thread: self.thread.join(120)
        return self.lexicon

    def _load(self, stamp):
        try:
            if stamp:
                self.lexicon, self.loaded = Lexicon.load(self.path), stamp
            else:
                lexicon = build(self.db_path)
                lexicon.save(self.path)
                self.lexicon, self.loaded = lexicon, self.path.stat().st_mtime
        except (OSError, ValueError, KeyError, sqlite3.Error):
            pass
        finally:
            self.ready.set()
