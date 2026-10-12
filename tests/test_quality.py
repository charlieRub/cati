"""Escenarios reales de pirámides, Egipto, paridad de vectores y vigilancia Linux."""
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from cati.config import DEFAULTS
from cati.engine import Engine
from cati.indexer import index
from cati.readers import image
from cati.search import search
from cati.store import connect, opened
from cati.watch import Watcher
from test_integration import pdf

FIXTURES=Path(__file__).parent/'fixtures'


class Quality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='cati-quality-')
        cls.root=Path(cls.temp.name)
        cls.db=cls.root/'index.sqlite3'
        cls.engine=Engine()
        cls.config=DEFAULTS|{'folders':[str(cls.root)],'exclude':[],'media':True}
        for name in ['IMG_0001.jpg','IMG_0002.jpg']:shutil.copyfile(FIXTURES/name,cls.root/name)
        (cls.root/'A17.md').write_text('La civilización del valle del Nilo construyó tumbas monumentales en Guiza. Los faraones gobernaban desde Menfis. Keops mandó construir una gran pirámide como sepulcro.')
        (cls.root/'B18.txt').write_text('La pirámide alimentaria organiza frutas, verduras, cereales y proteínas para una dieta saludable.')
        (cls.root/'C19.txt').write_text('Una foto es un recuerdo. Quiero encontrar documentos sobre cómo redactar cartas y contratos comerciales.')
        pdf(cls.root/'D20.pdf',['This handbook describes a railway timetable and ticket reservations.',
                                'Egyptian archaeology: pharaohs, mummies, hieroglyphs and monumental tombs on the Nile.'])
        result=index(cls.config,db_path=cls.db,engine=cls.engine,report_status=False)
        assert result['errors']==0,result

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_visual_spanish_without_filename_clues(self):
        for query in ['foto de una pirámide','una foto de las pirámides de Egipto','quiero una imagen de las piramides','photo of pyramids in Egypt']:
            hits=search(query,db_path=self.db,engine=self.engine)
            self.assertTrue(hits,query)
            self.assertEqual(Path(hits[0]['path']).name,'IMG_0001.jpg',query)
            self.assertTrue(all(h['kind']=='image' for h in hits))
            self.assertFalse(hits[0]['keyword'])
        hits=search('foto de la torre Eiffel',db_path=self.db,engine=self.engine)
        self.assertEqual(Path(hits[0]['path']).name,'IMG_0002.jpg')
        self.assertEqual(search('foto de un gato',db_path=self.db,engine=self.engine),[])

    def test_natural_documents_and_pdf_page(self):
        hits=search('documentos sobre Egipto',db_path=self.db,engine=self.engine)
        self.assertTrue(hits)
        self.assertTrue(all(h['kind'] in {'document','pdf'} for h in hits))
        self.assertIn(Path(hits[0]['path']).name,{'A17.md','D20.pdf'})
        hits=search('PDF sobre momias y jeroglíficos',db_path=self.db,engine=self.engine)
        self.assertEqual(Path(hits[0]['path']).name,'D20.pdf')
        self.assertEqual(hits[0]['loc'],2)
        self.assertEqual(search('documentos sobre Egipto',lexical=True,db_path=self.db),[])

    def test_image_as_reference(self):
        hits=search(image_path=FIXTURES/'IMG_0001.jpg',db_path=self.db,engine=self.engine)
        self.assertEqual(Path(hits[0]['path']).name,'IMG_0001.jpg')
        self.assertGreater(hits[0]['cosine'],.999)

    def test_resumable_long_document_without_duplicates(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);path=root/'long.md';db=root/'index.sqlite3'
            path.write_text('\n'.join(f'Sección {i}: una historia del valle del Nilo y sus monumentos. '*15 for i in range(14)))
            config=self.config|{'folders':[str(root)]}
            first=index(config,db_path=db,engine=self.engine,candidates=[(path,'document')],max_segments=2,report_status=False)
            self.assertEqual(first['partial'],1)
            with opened(db) as connection:
                initial=connection.execute('SELECT count(*) FROM segments').fetchone()[0]
            self.assertEqual(initial,2)
            for _ in range(40):
                state=index(config,db_path=db,engine=self.engine,candidates=[(path,'document')],max_segments=2,report_status=False)
                if state['done']:break
            else:self.fail('No se completó el documento reanudado')
            with opened(db) as connection:
                made=[r[0] for r in connection.execute('SELECT vec FROM segments ORDER BY id')]
            expected=root/'expected.sqlite3'
            index(config,db_path=expected,engine=self.engine,candidates=[(path,'document')],report_status=False)
            with opened(expected) as connection:
                full=[r[0] for r in connection.execute('SELECT vec FROM segments ORDER BY id')]
            # Same segments, no duplicates; batched GPU inference differs only in rounding.
            self.assertEqual(len(made),len(full))
            for a,b in zip(made,full):
                self.assertGreater(float(np.frombuffer(a,'<f4')@np.frombuffer(b,'<f4')),.9995)

    def test_real_inotify_create_modify_move_and_delete(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);db=root/'index.sqlite3';path=root/'new.txt'
            config=self.config|{'folders':[str(root)],'scope':'folders','event_delay':.1,'scan_seconds':300,'job_seconds':2}
            watcher=Watcher(config,db,self.engine,status=False)
            thread=threading.Thread(target=watcher.run)
            thread.start()
            def wait_for(predicate):
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    if predicate():return
                    time.sleep(.1)
                self.fail('La vigilancia no actualizó el índice en 12 segundos')
            def finds(term):return bool(search(term,lexical=True,db_path=db))
            try:
                wait_for(lambda:root in watcher.directories)
                path.write_text('LIVECREATE77531: informe sobre monumentos históricos.')
                wait_for(lambda:finds('LIVECREATE77531'))
                with opened(db) as connection:before=connection.execute('SELECT vec FROM segments').fetchone()[0]
                watcher.rescan.set();time.sleep(.4)
                with opened(db) as connection:self.assertEqual(before,connection.execute('SELECT vec FROM segments').fetchone()[0])
                path.write_text('LIVEMODIFY88531: informe actualizado con una nueva referencia.')
                wait_for(lambda:finds('LIVEMODIFY88531') and not finds('LIVECREATE77531'))
                moved=root/'renamed.txt';path.rename(moved)
                wait_for(lambda:finds('LIVEMODIFY88531') and Path(search('LIVEMODIFY88531',lexical=True,db_path=db)[0]['path'])==moved)
                moved.unlink()
                wait_for(lambda:not finds('LIVEMODIFY88531'))
            finally:
                watcher.stop.set();watcher.rescan.set();thread.join(timeout=15)
                self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main(verbosity=2)
