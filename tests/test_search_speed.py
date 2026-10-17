"""Atajos de velocidad y memoria de la búsqueda: deben dar exactamente lo mismo que el camino lento."""
import random
import tempfile
import unicodedata
import unittest
from pathlib import Path

from cati import spelling
from cati.query import Intent, normalize, parse
from cati.search import Admitted, FileTable, Searcher, kind_of, suffix_of
from cati.store import connect


def slow_normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))


ROWS = [(3, '/home/u/Fotos/playa.JPG', 'image', 300, 2_000_000),
        (5, '/home/u/Documentos/factura dentista.pdf', 'pdf', 500, 90_000),
        (6, '/home/u/Documentos/egipto/notas agosto.txt', 'document', 100, 4_000),
        (9, '/home/u/code/main.py', 'code', 900, 1_000),
        (10, '/home/u/Música/canción ñandú.mp3', 'audio', 700, 9_000_000),
        (12, '/home/u/.bashrc', 'document', 50, 10),
        (14, '/home/u/sin extension', 'document', 60, 20)]


class Normalize(unittest.TestCase):
    def test_same_as_character_filter(self):
        random.seed(5)
        samples = ['Canción Ñandú', 'ÁRBOL', 'ǅemal', 'ﬁchero', 'ẞtraße', 'İstanbul', 'Ⅻ ①', 'Σίσυφος', 'á', '', 'plain ascii']
        samples += [''.join(chr(random.randrange(1, 0x2FFFF)) for _ in range(12)) for _ in range(3000)]
        for text in samples: self.assertEqual(normalize(text), slow_normalize(text), repr(text))


class Words(unittest.TestCase):
    def test_lookup_iteration_and_lengths(self):
        words = sorted(['año', 'casa', 'dentista', 'egipto', 'ñandú', 'zeta', 'αβγ'])
        table = spelling.Words(words)
        self.assertEqual(list(table), words)
        self.assertEqual([table[i] for i in range(len(table))], words)
        for i, word in enumerate(words): self.assertEqual(table.index(word), i)
        for missing in ('', 'a', 'cas', 'casas', 'zz', 'ω'): self.assertIsNone(table.index(missing))
        empty = spelling.Words('')
        self.assertEqual((len(empty), list(empty), empty.index('x')), (0, [], None))

    def test_lexicon_survives_save_and_load(self):
        lexicon = spelling.Lexicon.create({'piramides': 3, 'egipto': 2}, {'dentista': 4}, {'es': {'casa': 90}, 'en': {}}, {})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lexicon.npz'
            lexicon.save(path)
            loaded = spelling.Lexicon.load(path)
        self.assertEqual(list(loaded.words), list(lexicon.words))
        self.assertEqual(loaded.frequency('piramides'), 3)
        self.assertEqual(loaded.documents('dentista'), 4)
        self.assertEqual(loaded.correct('piramdes de egipto')[0], 'piramides de egipto')
        self.assertEqual(list(loaded.lengths), [len(w) for w in lexicon.words])


class Files(unittest.TestCase):
    def setUp(self):
        self.table = FileTable(ROWS)
        self.old = {fid: (path, kind, mtime, size, kind_of(path, kind)) for fid, path, kind, mtime, size in ROWS}

    def test_reads_like_the_old_dict(self):
        self.assertEqual(dict(self.table.items()), self.old)
        self.assertEqual(list(self.table), sorted(self.old))
        self.assertEqual(len(self.table), len(self.old))
        for fid in (-1, 0, 4, 13, 99): self.assertNotIn(fid, self.table); self.assertIsNone(self.table.get(fid))
        self.assertEqual(self.table[5][0], '/home/u/Documentos/factura dentista.pdf')
        with self.assertRaises(KeyError): self.table[4]
        self.assertEqual(self.table.code_mask().nonzero()[0].tolist(), [9])

    def test_lookup_by_path(self):
        paths = [row[1] for row in ROWS] + ['/home/u/nada', '']
        self.assertEqual(self.table.lookup(paths), {row[1]: row[0] for row in ROWS})
        self.assertEqual(FileTable(()).lookup(['/x']), {})

    def test_suffix_matches_pathlib(self):
        for path in ['/a/b.TXT', '/a/.bashrc', '/a/b.', '/a/b.tar.gz', 'x/.', '/', 'a/b/', 'dir/..', 'a..b', 'sin']:
            self.assertEqual(suffix_of(path), Path(path).suffix.lower(), path)

    def test_admitted_matches_accepts(self):
        searcher = Searcher.__new__(Searcher)
        searcher.files = self.table
        intents = [parse(q) for q in ('fotos', 'pdfs', 'documentos que no sean pdf', 'egipto', 'archivos de más de 1 MB',
                                      'de menos de 5 KB', 'no imágenes', 'notas de agosto', 'código', 'en la carpeta egipto')]
        intents += [Intent(raw='', location=Path('/home/u/Documentos')), Intent(raw='', exts={'.py'}),
                    Intent(raw='', after=200, before=800), Intent(raw='', after=200, date_words={'agosto'}),
                    Intent(raw='', after=200, date_field='created'), Intent(raw='', excluded_terms=['factura'])]
        for intent in intents:
            for code in (False, True):
                births = {6: 650}

                def old(fid):
                    info = self.old.get(fid)
                    if info is None or (info[1] == 'code') != code: return False
                    return searcher.accepts(intent, info[0], info[4], info[2], info[3], btime=births.get(fid))
                expected = [fid for fid in range(-2, 20) if old(fid)]
                self.assertEqual([fid for fid in range(-2, 20) if Admitted(searcher, intent, code, births)(fid)], expected, intent)
                self.assertEqual(Admitted(searcher, intent, code, births).accepted().tolist(), expected, intent)


class OddQueries(unittest.TestCase):
    def test_absurd_sizes_do_not_overflow(self):
        for query in ('más de 99999999999999999999 GB', 'menos de ' + '9' * 400 + ' gb', 'unos ' + '9' * 400 + ' mb'):
            intent = parse(query)
            for value in (intent.min_size, intent.max_size):
                if value is not None: self.assertLess(value, 1 << 63)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'grande.txt').write_text('x')
            db = connect(root / 'index.db')
            with db:
                db.execute("INSERT INTO files(path,mtime,size,kind) VALUES(?,1,1,'document')", (str(root / 'grande.txt'),))
            search = Searcher(root / 'index.db')
            try:
                for query in ('más de 99999999999999999999 GB', '"', '***', '😀', 'code:', 'x' * 1000, 'NOT', '{text}: a'):
                    search.run(query, semantic=False, exact=True)
                search.run('', semantic=False, filters={'kind': 'document', 'period': 'year'})
            finally:
                search.db.close(); db.close()
                if hasattr(search, '_lock'): search._lock.close()


if __name__ == '__main__': unittest.main()
