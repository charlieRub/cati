"""Pruebas sin modelo: interpretación de frases, catálogo de nombres y orden de resultados."""
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from cati import catalog, spelling
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


def when(value):
    return datetime.fromtimestamp(value / 1e9) if value else None


class Calendar(unittest.TestCase):
    def test_exact_days_in_every_spelling(self):
        for phrase in ['fotos del 7 de septiembre', 'fotos del 7/9/2026', 'fotos del 2026-09-07', 'fotos del 07.09.2026',
                       'fotos de september 7']:
            intent = parse(phrase, NOW)
            self.assertEqual((when(intent.after), when(intent.before)), (datetime(2026, 9, 7), datetime(2026, 9, 8)), phrase)
            self.assertEqual(intent.categories, {'image'})
        # A day still to come this year belongs to the last one.
        self.assertEqual(when(parse('el 3 de diciembre', NOW).after), datetime(2025, 12, 3))

    def test_ranges_and_relative_days(self):
        spring = parse('entre marzo y junio de 2025', NOW)
        self.assertEqual((when(spring.after), when(spring.before)), (datetime(2025, 3, 1), datetime(2025, 7, 1)))
        since = parse('documentos desde el 1 de septiembre', NOW)
        self.assertEqual((when(since.after), since.before), (datetime(2026, 9, 1), None))
        self.assertEqual(when(parse('antes de 2020', NOW).before), datetime(2020, 1, 1))
        week = parse('fotos de hace una semana', NOW)
        self.assertEqual((when(week.after), when(week.before)), (datetime(2026, 10, 2), datetime(2026, 10, 9)))
        monday = parse('lo del lunes pasado', NOW)  # 12-oct-2026 is a Monday: the previous one
        self.assertEqual(when(monday.after), datetime(2026, 10, 5))
        self.assertEqual(when(parse('vídeos del fin de semana', NOW).after), datetime(2026, 10, 10))
        three = parse('imágenes de los últimos tres días', NOW)
        self.assertEqual(when(three.after), datetime(2026, 10, 10))

    def test_hours_narrow_a_single_day(self):
        shots = parse('capturas de pantalla del 30 de agosto a las 14', NOW)
        self.assertEqual((when(shots.after), when(shots.before)), (datetime(2026, 8, 30, 14), datetime(2026, 8, 30, 15)))
        self.assertEqual(shots.terms, [])
        self.assertEqual(shots.categories, {'image'})
        evening = parse('lo que modifiqué ayer por la tarde', NOW)
        self.assertEqual((when(evening.after), when(evening.before)), (datetime(2026, 10, 11, 13), datetime(2026, 10, 11, 21)))
        self.assertEqual(evening.labels, ['modificado ayer por la tarde'])

    def test_creation_date_is_explicit(self):
        made = parse('html que creé el 10 de octubre', NOW)
        self.assertEqual(made.date_field, 'created')
        self.assertEqual(made.labels, ['HTML', 'creado 10 oct 2026'])
        self.assertEqual(parse('pdf que descargué ayer', NOW).date_field, 'created')
        self.assertEqual(parse('fotos de agosto', NOW).date_field, 'any')

    def test_month_next_to_a_topic_only_favours(self):
        topic = parse('rentabilidad de agosto', NOW)
        self.assertIsNone(topic.after)
        self.assertEqual(when(topic.soft_after), datetime(2026, 8, 1))
        self.assertEqual(topic.terms, ['rentabilidad'])
        self.assertIn('agosto', topic.date_words)
        alone = parse('fotos de agosto', NOW)
        self.assertEqual((when(alone.after), when(alone.before)), (datetime(2026, 8, 1), datetime(2026, 9, 1)))
        verb = parse('pdf de egipto creado en agosto', NOW)
        self.assertEqual(when(verb.after), datetime(2026, 8, 1))
        self.assertEqual(parse('fotos de 2023', NOW).terms, [])
        self.assertIsNotNone(parse('fotos de 2023', NOW).after)

    def test_size_about(self):
        about = parse('fotos de 3,5 mb', NOW)
        self.assertLess(about.min_size, 3.5 * 1024 ** 2)
        self.assertGreater(about.max_size, 3.5 * 1024 ** 2)


class Formats(unittest.TestCase):
    def test_any_extension_when_written_as_one(self):
        for phrase in ['archivos .kml', 'extensión kml', 'ext:kml', '*.kml', 'terminados en kml', 'formato kml', 'kml del viaje']:
            self.assertEqual(parse(phrase, NOW).exts, {'.kml'}, phrase)
        self.assertEqual(parse('extensión blend', NOW).exts, {'.blend'})
        self.assertEqual(parse('pngs de la boda', NOW).exts, {'.png'})
        self.assertEqual(parse('el appimage de orca', NOW).exts, {'.appimage'})

    def test_ordinary_words_are_not_formats(self):
        iso = parse('norma iso 9001', NOW)
        self.assertIsNone(iso.exts)
        self.assertEqual(iso.terms, ['norma', 'iso', '9001'])
        self.assertEqual(parse('archivo .iso', NOW).exts, {'.iso'})
        self.assertIsNone(parse('tipo de cambio', NOW).exts)

    def test_exclusions(self):
        docs = parse('documentos sin pdf', NOW)
        self.assertEqual(docs.excluded_exts, {'.pdf'})
        self.assertEqual(docs.terms, [])
        other = parse('pdfs que no sean de egipto', NOW)
        self.assertEqual(other.excluded_terms, ['egipto'])
        self.assertEqual(other.exts, {'.pdf'})
        self.assertEqual(other.terms, [])
        self.assertEqual(parse('fotos excepto capturas', NOW).excluded_categories, {'image'})

    def test_screenshot_phrase_is_a_kind(self):
        intent = parse('capturas de pantalla de egipto', NOW)
        self.assertEqual(intent.categories, {'image'})
        self.assertEqual(intent.terms, ['egipto'])

    def test_office_categories(self):
        self.assertEqual(catalog.category('.pptx'), 'slides')
        self.assertEqual(catalog.category('.xlsx'), 'sheet')
        self.assertEqual(catalog.category('.ods'), 'sheet')
        self.assertEqual(catalog.category('.docx'), 'document')


class Spelling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = {'factura': 4, 'dentista': 2, 'travelike': 30, 'generador': 3, 'fichajes': 5, 'kawasaki': 1, 'playa': 2}
        content = {'lactante': 12, 'febril': 9, 'alquiler': 7, 'contrato': 20, 'control': 40, 'neurologia': 5}
        languages = {'es': {'gato': 9000, 'dato': 20000, 'factura': 4000, 'contrato': 3000, 'generar': 900, 'generador': 300,
                            'nefrologia': 30, 'urologia': 80, 'ayer': 50000},
                     'en': {'copilot': 50, 'play': 9000}}
        cls.lexicon = spelling.Lexicon.create(names, content, languages, {'nefrologia': 'nefrología'})

    def fix(self, text):
        return self.lexicon.correct(text)[0]

    def test_typical_typos(self):
        self.assertEqual(self.fix('factra del dentsta'), 'factura del dentista')
        self.assertEqual(self.fix('lactnte febril'), 'lactante febril')
        self.assertEqual(self.fix('contrto de alquilr'), 'contrato de alquiler')
        self.assertEqual(self.fix('generadr de fichages'), 'generador de fichajes')
        self.assertEqual(self.fix('enfermedad de kawasky'), 'enfermedad de kawasaki')
        self.assertEqual(self.fix('logo de travelik'), 'logo de travelike')
        self.assertEqual(self.fix('fotso de ayr'), 'fotos de ayer')

    def test_real_words_stay(self):
        self.assertEqual(self.fix('foto de un gato'), 'foto de un gato')
        self.assertEqual(self.fix('tema de nefrología y urología'), 'tema de nefrología y urología')
        self.assertEqual(self.fix('clase de ITU'), 'clase de ITU')          # acronyms
        self.assertEqual(self.fix('informe.pfd'), 'informe.pfd')            # extensions and paths
        self.assertEqual(self.fix('"factra" del dentsta'), '"factra" del dentista')  # quoted text is literal

    def test_rest_of_the_phrase_breaks_a_tie(self):
        lexicon = spelling.Lexicon.create({'test': 9, 'stubs': 3, 'module': 2, 'mode': 40}, {}, {'es': {}, 'en': {'mode': 9000, 'module': 900}}, {})
        # A common word can outrank a closer one; the file names that hold the other words decide.
        common = spelling.Lexicon.create({'test': 9, 'stubs': 3, 'module': 1, 'mode': 400}, {},
                                         {'es': {}, 'en': {'mode': 900000, 'module': 2}}, {})
        self.assertEqual(common.correct('test modue stubs')[0], 'test mode stubs')
        named = lambda word, others: word == 'module' and {'test', 'stubs'} <= set(others)  # «test_no_module_stubs.py»
        self.assertEqual(common.correct('test modue stubs', named)[0], 'test module stubs')
        # Context never pulls in a spelling farther away than the preferred one.
        farther = lambda word, others: word == 'mode'
        self.assertEqual(lexicon.correct('test modue stubs', farther)[0], lexicon.correct('test modue stubs')[0])

    def test_far_keys_are_not_typos(self):
        self.assertGreater(spelling.distance('nefrologia', 'neurologia', 3), 1.6)
        self.assertLessEqual(spelling.distance('lactnte', 'lactante', 3), .8)
        self.assertLessEqual(spelling.distance('cehck', 'check', 3), .8)

    def test_saved_and_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lexicon.npz'
            self.lexicon.save(path)
            again = spelling.Lexicon.load(path)
        self.assertEqual(again.correct('factra del dentsta')[0], 'factura del dentista')
        self.assertEqual(again.frequency('travelike'), 30)


class Names(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='cati-names-')
        root = Path(cls.temp.name)
        files = ['Descargas/fondo_pantalla_actual.png', 'Documentos/egipto/ficha-egipto.html', 'Documentos/egipto/notas.md',
                 'Documentos/recetas/sopa.txt', 'Descargas/AK Lactante febril.pdf', 'Documentos/.oculto/secreto.txt',
                 'Documentos/claves/passwords.txt', 'Documentos/proyecto/node_modules/x/index.html',
                 'Documentos/RENTABILIDAD  AGOSTO.xlsx', 'Documentos/rentabilidad julio.xlsx', 'Descargas/viejo.kml',
                 'Documentos/presentaciones/Muerte subita.pptx']
        for name in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('x')
        old = time.time() - 400 * 86400
        os.utime(root / 'Documentos/recetas/sopa.txt', (old, old))
        os.utime(root / 'Descargas/viejo.kml', (old, old))  # downloaded today, modified long ago
        september = datetime(datetime.now().year - 1, 9, 17).timestamp()
        for name in ('Documentos/RENTABILIDAD  AGOSTO.xlsx', 'Documentos/rentabilidad julio.xlsx'):
            os.utime(root / name, (september, september))
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

    def test_creation_date_when_asked(self):
        self.assertNotIn('viejo.kml', self.names('kml de esta semana'))
        if catalog.birth(self.root / 'Descargas/viejo.kml'):  # filesystems without creation dates skip this
            self.assertEqual(self.names('kml que descargué esta semana'), ['viejo.kml'])

    def test_a_month_in_the_name_answers_the_month(self):
        year = datetime.now().year - 1
        found = self.names(f'rentabilidad de agosto de {year}')
        self.assertEqual(found[0], 'RENTABILIDAD  AGOSTO.xlsx')

    def test_slides_and_exclusions(self):
        self.assertEqual(self.names('presentación muerte súbita'), ['Muerte subita.pptx'])
        self.assertNotIn('ficha-egipto.html', self.names('documentos sin html'))
        self.assertNotIn('ficha-egipto.html', self.names('archivos de egipto que no sean ficha'))

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
