"""Banco de relevancia sobre un índice real: mide si lo que buscas sale arriba.

    python tests/relevance.py generate [--cases 120]   # crea consultas desde tu índice
    python tests/relevance.py run [--label antes]      # acierto@1, @5, @10 y MRR por grupo

Las consultas se generan de tus propios archivos («known-item»): palabras del nombre, palabras
distintivas del contenido y, para cada una, una variante con erratas realistas. Puedes añadir
casos a mano en el mismo JSON con {"query": ..., "targets": [rutas o patrones glob], "group": "manual"}.
El banco y los resultados viven en ~/.cache/cati-eval: contienen nombres de tus archivos y no se publican.
"""
import argparse
import fnmatch
import json
import random
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cati.config import DATA  # noqa: E402
from cati.query import STOP, normalize  # noqa: E402

HOME = Path.home()
FOLDER = HOME / '.cache/cati-eval'
BENCH = FOLDER / 'bench.json'
KEYS = 'qwertyuiop asdfghjklñ zxcvbnm'.split()
NOISE = re.compile(r'(?:^|/)(?:node_modules|\.cache|build[^/]*|tmp|__pycache__|test-results|runtimes)(?:/|$)')


def neighbours(char):
    for row, keys in enumerate(KEYS):
        if char in keys:
            i = keys.index(char)
            near = [keys[j] for j in (i - 1, i + 1) if 0 <= j < len(keys)]
            for other in (row - 1, row + 1):
                if 0 <= other < len(KEYS) and i < len(KEYS[other]): near.append(KEYS[other][i])
            return near
    return [char]


def typo(word, rng):
    """Una errata humana: falta, sobra, se cambia por una tecla vecina o se intercambian dos letras."""
    if len(word) < 4: return word
    i = rng.randrange(1, len(word) - 1)
    kind = rng.choice(['drop', 'drop', 'swap', 'near', 'double'])
    if kind == 'drop': return word[:i] + word[i + 1:]
    if kind == 'swap': return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if kind == 'near': return word[:i] + rng.choice(neighbours(word[i])) + word[i + 1:]
    return word[:i] + word[i] + word[i:]


def misspell(words, rng):
    long = [i for i, w in enumerate(words) if len(w) >= 5]
    if not long: return None
    chosen = set(rng.sample(long, max(1, len(long) // 2 + (len(long) % 2))))
    return [typo(w, rng) if i in chosen else w for i, w in enumerate(words)]


def readable(word):
    return (word.isalpha() and 4 <= len(word) <= 18 and word not in STOP and re.search(r'[aeiou]', word)
            and not re.search(r'[^aeiou]{4}', word) and not re.fullmatch(r'[a-f]+', word))


def generate(cases, seed=7, keep_manual=True):
    rng = random.Random(seed)
    db = sqlite3.connect(DATA / 'index.sqlite3')
    db.row_factory = sqlite3.Row
    bench = []

    # 1. Por nombre: archivos con nombres descriptivos de dos o más palabras.
    rows = db.execute("SELECT path,name FROM catalog WHERE is_dir=0").fetchall()
    rng.shuffle(rows)
    by_name = {}
    for row in rows:
        by_name.setdefault(Path(row['path']).name, []).append(row['path'])
    picked = 0
    for row in rows:
        if picked >= cases // 3: break
        if NOISE.search(row['path']): continue
        words = [w for w in re.findall(r'[^\W\d_]+', normalize(row['name'])) if readable(w)]
        words = list(dict.fromkeys(words))[:3]
        if len(words) < 2: continue
        query = ' '.join(words)
        targets = by_name[Path(row['path']).name]
        if len(targets) > 6: continue
        # Only distinctive names: a query matching dozens of sibling files has no single right answer.
        rivals = db.execute('SELECT count(DISTINCT name) FROM catalog_names WHERE catalog_names MATCH ?',
                            (' AND '.join(f'name:"{w}"' for w in words),)).fetchone()[0]
        if rivals > 3: continue
        bench.append({'query': query, 'targets': targets, 'group': 'nombre'})
        wrong = misspell(words, rng)
        if wrong and wrong != words:
            bench.append({'query': ' '.join(wrong), 'targets': targets, 'group': 'nombre-errata'})
        picked += 1

    # 2. Por contenido: tres palabras distintivas de un fragmento; vale cualquier archivo que las contenga todas.
    db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, 'words', 'row')")
    frequency = dict(db.execute('SELECT term,doc FROM temp.vocab'))
    segments = db.execute("""SELECT s.text,f.path FROM segments s JOIN files f ON f.id=s.file
                             WHERE s.kind IN ('text','page','ocr') AND length(s.text)>300 ORDER BY random() LIMIT 3000""").fetchall()
    picked = 0
    for segment in segments:
        if picked >= cases // 3: break
        if NOISE.search(segment['path']): continue
        words = [w for w in dict.fromkeys(re.findall(r'[^\W\d_]+', normalize(segment['text']))) if readable(w) and len(w) >= 5]
        words = [w for w in words if 2 <= frequency.get(w, 0) <= 40]
        if len(words) < 3: continue
        words = rng.sample(words, 3)
        files = {r[0] for r in db.execute('''SELECT DISTINCT f.path FROM words JOIN segments s ON s.id=words.rowid
                   JOIN files f ON f.id=s.file WHERE words MATCH ?''', ('{text}: ' + ' AND '.join(words),))}
        if not files or len(files) > 5: continue
        bench.append({'query': ' '.join(words), 'targets': sorted(files), 'group': 'contenido'})
        wrong = misspell(words, rng)
        if wrong and wrong != words:
            bench.append({'query': ' '.join(wrong), 'targets': sorted(files), 'group': 'contenido-errata'})
        picked += 1

    manual = []
    if keep_manual and BENCH.exists():
        manual = [c for c in json.loads(BENCH.read_text()) if c['group'].startswith('manual')]
    FOLDER.mkdir(parents=True, exist_ok=True)
    BENCH.write_text(json.dumps(bench + manual, ensure_ascii=False, indent=1))
    print(f'{len(bench)} casos generados (+{len(manual)} manuales) en {BENCH}')


def matches(path, targets):
    path = unicodedata.normalize('NFC', path.replace(str(HOME), '~'))
    return any(path == t or fnmatch.fnmatch(path, t)
               for t in (unicodedata.normalize('NFC', t.replace(str(HOME), '~')) for t in targets))


def rank(hits, targets):
    for position, hit in enumerate(hits, 1):
        if matches(hit['path'], targets) or any(matches(c, targets) for c in hit.get('copies', [])):
            return position
    return None


def run(label, limit=20, show=False):
    from cati.search import Searcher
    bench = json.loads(BENCH.read_text())
    searcher = Searcher()
    searcher.warm()
    getattr(searcher, "spelling", None) and searcher.spelling.get(wait=True)
    groups, report, timings = {}, [], []
    for case in bench:
        began = time.perf_counter()
        result = searcher.run(case['query'], limit)
        timings.append(time.perf_counter() - began)
        position = rank(result.hits, case['targets'])
        groups.setdefault(case['group'], []).append(position)
        report.append({**case, 'rank': position, 'top': [h['path'] for h in result.hits[:5]],
                       'corrected': getattr(result.intent, 'corrected', None)})
        if show and (position is None or position > 1):
            print(f"  [{position}] {case['group']}: {case['query']!r} → {getattr(result.intent, 'corrected', None)!r}")
            for hit in result.hits[:3]: print('       ', round(hit['score'], 2), hit['path'].replace(str(HOME), '~'), hit['reasons'])
    print(f'\n{label}  ({len(bench)} consultas, mediana {statistics.median(timings) * 1000:.0f} ms, '
          f'p95 {sorted(timings)[int(len(timings) * .95)] * 1000:.0f} ms)')
    print(f"{'grupo':<18}{'n':>4}{'@1':>7}{'@5':>7}{'@10':>7}{'MRR':>7}")
    total = []
    for group, ranks in sorted(groups.items()):
        total += ranks
        print(f'{group:<18}{len(ranks):>4}' + ''.join(f'{sum(1 for r in ranks if r and r <= k) / len(ranks):>7.2f}' for k in (1, 5, 10))
              + f'{sum(1 / r for r in ranks if r) / len(ranks):>7.3f}')
    print(f"{'TOTAL':<18}{len(total):>4}" + ''.join(f'{sum(1 for r in total if r and r <= k) / len(total):>7.2f}' for k in (1, 5, 10))
          + f'{sum(1 / r for r in total if r) / len(total):>7.3f}')
    (FOLDER / f'result-{label}.json').write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['generate', 'run'])
    parser.add_argument('--cases', type=int, default=150)
    parser.add_argument('--label', default='actual')
    parser.add_argument('--show', action='store_true', help='muestra los fallos')
    parser.add_argument('--seed', type=int, default=7, help='otra semilla da otro banco (validación independiente)')
    parser.add_argument('--bench', type=Path, help=f'banco alternativo (por defecto {BENCH})')
    args = parser.parse_args()
    if args.bench: BENCH = args.bench
    generate(args.cases, args.seed, keep_manual=not args.bench) if args.command == 'generate' else run(args.label, show=args.show)
