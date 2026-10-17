"""Regresiones con SQLite y el modelo real; ejecuta desde la raíz del proyecto."""
import json
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from cati.config import DEFAULTS
from cati.engine import Engine
from cati.indexer import index
from cati.readers import walk, chunks
from cati.search import forget, search
from cati.store import connect, opened


def pdf(path, pages):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for text in pages:
        page_id = len(objects) + 1
        kids.append(f'{page_id} 0 R')
        stream = f'BT /F1 18 Tf 60 700 Td ({text}) Tj ET'.encode('latin1')
        objects.extend([f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id+1} 0 R >>'.encode(),
                        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream'])
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'.encode()
    data = b'%PDF-1.4\n'; offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data)); data += f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    for offset in offsets[1:]: data += f'{offset:010d} 00000 n \n'.encode()
    data += f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(data)


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='cati-acceptance-')
        cls.root = Path(cls.temp.name)
        cls.db = cls.root / 'test.sqlite3'
        cls.config = DEFAULTS | {'folders': [str(cls.root)], 'exclude': [], 'code': False}
        cls.engine = Engine()
        (cls.root / 'alquiler.md').write_text('La vivienda permite animales domésticos. El propietario autoriza mascotas, perros y gatos, siempre que el inquilino cubra los daños.')
        (cls.root / 'receta.txt').write_text('Para preparar la sopa, cocer zanahorias, patatas y verduras en agua durante veinte minutos.')
        (cls.root / 'invoice.txt').write_text('Factura F2026-042. Total 125 euros. Compra de material de oficina.')
        pdf(cls.root / 'manual.pdf', ['Introduction to the machine and its components.', 'Warranty: the repair is free for two years after purchase.'])
        pdf(cls.root / 'scan.pdf', [''])
        with zipfile.ZipFile(cls.root / 'nota.docx', 'w') as z:
            z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Reserva de hotel confirmada para septiembre.</w:t></w:r></w:p></w:body></w:document>')
        im = Image.new('RGB', (600, 240), 'white')
        font_path = next((p for p in [Path('/usr/share/fonts/TTF/DejaVuSans.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.exists()),None)
        font = ImageFont.truetype(str(font_path),32) if font_path else ImageFont.load_default(size=32)
        ImageDraw.Draw(im).text((40, 80), 'INVOICE 4815162342', fill='black', font=font)
        im.save(cls.root / 'captura.png')
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=2','-ar','16000','-ac','1',str(cls.root/'tone.wav')],check=True)
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=green:s=320x240:d=2','-f','lavfi','-i','sine=frequency=440:duration=2','-shortest',str(cls.root/'clip.mp4')],check=True)
        hidden = cls.root / '.private';hidden.mkdir();(hidden/'hidden.txt').write_text('Do not read')
        (cls.root/'credentials.txt').write_text('Do not read')
        (cls.root/'linked.txt').symlink_to(cls.root/'receta.txt')
        repo=cls.root/'project';repo.mkdir()
        subprocess.run(['git','init','-q',str(repo)],check=True)
        (repo/'.gitignore').write_text('ignored.py\n')
        (repo/'function.py').write_text('def retry_request():\n    """Retry a failed network request."""\n    return True\n')
        (repo/'ignored.py').write_text('ignored')
        cls.first = index(cls.config, db_path=cls.db, engine=cls.engine)

    @classmethod
    def tearDownClass(cls):
        forget(cls.db)
        cls.temp.cleanup()

    def test_01_modalities_and_pdf_location(self):
        self.assertEqual(self.first['errors'], 0)
        with opened(self.db) as db:
            kinds = {r[0] for r in db.execute('SELECT kind FROM segments')}
        self.assertTrue({'document','pdf','pdf-image','image','ocr','audio','video-frame'} <= kinds)
        hits = search('free repair warranty', db_path=self.db, engine=self.engine)
        manual = next(h for h in hits if Path(h['path']).name == 'manual.pdf')
        self.assertEqual(manual['loc'], 2)

    def test_02_spanish_semantic_search(self):
        hits = search('contrato que deja tener un perro en casa', db_path=self.db, engine=self.engine)
        self.assertEqual(Path(hits[0]['path']).name, 'alquiler.md')
        results = search('4815162342', lexical=True, db_path=self.db)
        self.assertEqual(Path(results[0]['path']).name, 'captura.png')
        self.assertTrue(results[0]['keyword'])

    def test_03_privacy_and_code_isolation(self):
        found = {p.name for p, _ in walk(self.config)}
        self.assertFalse({'hidden.txt','credentials.txt','linked.txt','function.py','ignored.py'} & found)
        config = self.config | {'code': True}
        found = {p.name for p, _ in walk(config)}
        self.assertIn('function.py', found)
        self.assertNotIn('ignored.py', found)
        index(config, db_path=self.db, engine=self.engine)
        self.assertEqual(search('retry_request', lexical=True, db_path=self.db), [])
        hit = search('code: retry_request', lexical=True, db_path=self.db)[0]
        self.assertEqual(hit['kind'], 'code')
        self.assertEqual(hit['loc'], 1)

    def test_04_incremental_changed_deleted_and_cancelled(self):
        with opened(self.db) as db:
            before = db.execute('SELECT vec FROM segments ORDER BY id').fetchall()
        state = index(self.config | {'code': True}, db_path=self.db, engine=self.engine)
        self.assertEqual(state['done'], 0)
        self.assertGreaterEqual(state['unchanged'], 9)
        with opened(self.db) as db:
            after = db.execute('SELECT vec FROM segments ORDER BY id').fetchall()
        self.assertEqual([r[0] for r in before], [r[0] for r in after])
        (self.root/'invoice.txt').write_text('Replacement invoice NEW97531')
        (self.root/'receta.txt').unlink()
        index(self.config | {'code': True}, db_path=self.db, engine=self.engine)
        self.assertEqual(search('F2026-042', lexical=True, db_path=self.db), [])
        self.assertEqual(Path(search('NEW97531', lexical=True, db_path=self.db)[0]['path']).name, 'invoice.txt')
        self.assertEqual(search('zanahorias', lexical=True, db_path=self.db), [])
        stop=threading.Event();stop.set()
        state=index(self.config,stop=stop,db_path=self.db,engine=self.engine)
        self.assertEqual(state['done'],0)

    def test_05_nonfinite_vectors_and_model_identity(self):
        v=self.engine.text(['task: search result | query: local files'])
        self.assertEqual(v.shape,(1,768))
        self.assertTrue(np.isfinite(v).all())
        self.assertAlmostEqual(float(np.linalg.norm(v)),1,places=5)
        other=self.root/'other.sqlite3'
        with opened(other) as db: db.execute("UPDATE meta SET value='wrong-model' WHERE key='fingerprint'")
        with self.assertRaises(RuntimeError): connect(other)


if __name__ == '__main__': unittest.main(verbosity=2)
