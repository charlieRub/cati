"""Pruebas sin modelo: interpretación de frases, catálogo de nombres y orden de resultados."""
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from cati import catalog
from cati.query import parse
from cati.search import Searcher
from cati.store import connect

NOW = datetime(2026, 10, 12, 18, 0)


class Phrases(unittest.TestCase):
    def test_extension_beats_generic_type(self):
        intent = parse('documento html de pirámides', NOW)
        self.assertEqual(intent.exts, {'.html', '.htm'})
        self.assertIsNone(intent.categories)
        self.assertEqual(intent.terms, ['piramides'])
        self.assertEqual(intent.semantic, 'documento de pirámides')

    def test_type_words(self):
        self.assertEqual(parse('foto de una pirámide', NOW).categories, {'image'})
        self.assertEqual(parse('documentos sobre Egipto', NOW).categories, {'document', 'pdf'})
        self.assertEqual(parse('carpeta de egipto', NOW).categories, {'folder'})
        self.assertEqual(parse('notas de voz', NOW).categories, {'audio'})

    def test_topic_words_stay_terms(self):
        intent = parse('informe.pdf', NOW)
        self.assertEqual(intent.exts, {'.pdf'})
        self.assertEqual(intent.terms, ['informe'])

    def test_dates(self):
        yesterday = parse('fotos de ayer', NOW)
        self.assertEqual(datetime.fromtimestamp(yesterday.after / 1e9), datetime(2026, 10, 11))
        self.assertEqual(datetime.fromtimestamp(yesterday.before / 1e9), datetime(2026, 10, 12))
        self.assertEqual(yesterday.terms, [])
        last_week = parse('pdfs de la semana pasada', NOW)
        self.assertEqual(datetime.fromtimestamp(last_week.after / 1e9), datetime(2026, 10, 5))  # 12-oct-2026 es lunes
        march = parse('presentaciones de marzo', NOW)
        self.assertEqual(datetime.fromtimestamp(march.after / 1e9), datetime(2026, 3, 1))
        accented = parse('excel de gastos del año pasado', NOW)
        self.assertEqual(accented.terms, ['gastos'])
        self.assertEqual(accented.semantic, 'excel de gastos')

    def test_year_is_soft(self):
        intent = parse('facturas de 2024', NOW)
        self.assertIsNone(intent.after)
        self.assertIsNotNone(intent.soft_after)
        self.assertIn('2024', intent.terms)

    def test_size_and_place(self):
        big = parse('vídeos de más de 1 GB', NOW)
        self.assertEqual(big.min_size, 1024 ** 3)
        self.assertEqual(big.categories, {'video'})
        named = parse('fichas html en la carpeta travelike', NOW)
        self.assertEqual(named.location_word, 'travelike')
        self.assertEqual(named.terms, ['fichas'])


class Names(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='cati-names-')
        root = Path(cls.temp.name)
        files = ['Descargas/fondo_pantalla_actual.png', 'Documentos/egipto/ficha-egipto.html', 'Documentos/egipto/notas.md',
                 'Documentos/recetas/sopa.txt', 'Descargas/AK Lactante febril.pdf', 'Documentos/.oculto/secreto.txt',
                 'Documentos/claves/passwords.txt', 'Documentos/proyecto/node_modules/x/index.html']
        for name in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('x')
        old = time.time() - 400 * 86400
        os.utime(root / 'Documentos/recetas/sopa.txt', (old, old))
        cls.db_path = root / 'index.sqlite3'
        db = connect(cls.db_path)
        cls.count = catalog.sync(db, [root])
        db.close()
        cls.searcher = Searcher(cls.db_path)
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.searcher.db.close()
        cls.temp.cleanup()

    def names(self, query):
        return [Path(h['path']).name for h in self.searcher.run(query, semantic=False).hits]

    def test_private_and_dependency_folders_are_skipped(self):
        found = {Path(p).name for (p,) in self.searcher.db.execute('SELECT path FROM catalog')}
        self.assertNotIn('secreto.txt', found)
        self.assertNotIn('passwords.txt', found)
        self.assertNotIn('index.html', found)
        self.assertIn('egipto', found)

    def test_every_word_in_the_name_wins(self):
        self.assertEqual(self.names('fondo de pantalla')[0], 'fondo_pantalla_actual.png')
        self.assertEqual(self.names('pdf lactante febril')[0], 'AK Lactante febril.pdf')

    def test_extension_and_folders(self):
        self.assertEqual(self.names('html de egipto'), ['ficha-egipto.html'])
        self.assertEqual(self.names('carpeta egipto'), ['egipto'])
        self.assertEqual(self.names('ficha egipto notas')[0], 'ficha-egipto.html')
        self.assertEqual(self.names('receta de cocina tailandesa'), [])

    def test_dates_filter_by_modification(self):
        recent = self.names('documentos de este año')
        self.assertIn('notas.md', recent)
        self.assertNotIn('sopa.txt', recent)

    def test_events_update_the_catalog(self):
        db = connect(self.db_path)
        new = self.root / 'Documentos/presupuesto boda.txt'
        new.write_text('x')
        catalog.touch(db, [self.root], [new])
        self.assertEqual(self.names('presupuesto boda')[0], 'presupuesto boda.txt')
        new.unlink()
        catalog.touch(db, [self.root], [new])
        db.close()
        self.assertEqual(self.names('presupuesto boda'), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
