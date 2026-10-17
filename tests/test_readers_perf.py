"""Lectores: imágenes con transparencia, decodificación única y reducida, y fallos que no lo son."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from cati import readers
from cati.config import DEFAULTS
from cati.indexer import index
from cati.store import opened


class Readers(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)

    def tearDown(self):
        self.work.cleanup()

    def test_png_with_transparency_in_any_mode_is_read(self):
        # Android 9-patch PNGs: mode «1»/«L»/«RGB» with tRNS made Pillow fail when saving.
        cases = {'1': 255, 'L': 0, 'RGB': (0, 0, 0)}
        for mode, transparency in cases.items():
            source = self.root / f'{mode}.png'
            Image.new(mode, (300, 200)).save(source, transparency=transparency)
            dest = readers.image(source, self.root / f'{mode}-model.png', 140)
            with Image.open(dest) as out:
                self.assertEqual(out.mode, 'RGB')
                self.assertEqual(out.size, readers.fit(300, 200, 140))
        source = self.root / 'palette.png'
        Image.new('P', (300, 200)).save(source, transparency=0)
        segments = list(readers.extract(source, 'image', {'media': True}))
        self.assertEqual(segments[0].kind, 'image')

    def test_image_is_decoded_once_for_model_and_ocr(self):
        source = self.root / 'photo.png'
        Image.fromarray(np.random.default_rng(1).integers(0, 255, (900, 1200, 3), dtype=np.uint8)).save(source)
        with patch('cati.readers.ocr', return_value='') as ocr, patch('cati.readers.oriented', wraps=readers.oriented) as decode:
            model = [Image.open(s.media).tobytes() for s in readers.extract(source, 'image', {'media': True}) if s.media]
            self.assertEqual(decode.call_count, 1)
            ocr_input = Path(ocr.call_args[0][0])
        # Same pixels as reading each size on its own (search results do not change).
        expected = readers.image(source, self.root / 'expected.png', 140)
        with Image.open(expected) as want:
            self.assertEqual(model, [want.tobytes()])
        self.assertFalse(ocr_input.exists())  # temporary files are gone after the read

    def test_large_jpeg_is_decoded_reduced_with_the_same_final_size(self):
        source = self.root / 'big.jpg'
        exif = Image.Exif(); exif[0x0112] = 6  # rotated 90°: width and height swap
        Image.new('RGB', (6000, 4000), 'white').save(source, exif=exif)
        bitmap, size = readers.oriented(source, 140)
        self.assertEqual(size, (4000, 6000))
        self.assertLess(bitmap.width * bitmap.height, 6000 * 4000 // 4)
        target = readers.fit(*size, 140)
        self.assertGreaterEqual(bitmap.width, target[0] * 2)
        self.assertGreaterEqual(bitmap.height, target[1] * 2)
        with Image.open(readers.image(source, self.root / 'model.png', 140)) as out:
            self.assertEqual(out.size, target)

    def test_small_embedded_pictures_are_not_copied_out(self):
        source = self.root / 'doc.docx'
        big, small = self.root / 'big.png', self.root / 'small.png'
        rng = np.random.default_rng(2)
        Image.fromarray(rng.integers(0, 255, (400, 400, 3), dtype=np.uint8)).save(big)
        Image.fromarray(rng.integers(0, 255, (100, 200, 3), dtype=np.uint8)).save(small)
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="w"><w:p>hola</w:p></w:document>')
            archive.write(big, 'word/media/image1.png')
            archive.write(small, 'word/media/image2.png')
        out = self.root / 'out'; out.mkdir()
        found = readers.embedded_images(source, out)
        self.assertEqual(len(found), 1)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ['embedded-0.png'])
        # Resumed after the picture: it is not converted again.
        with patch('cati.readers.image', wraps=readers.image) as convert:
            self.assertEqual(list(readers.extract(source, 'document', {'media': True}, cursor=(1, 1))), [])
            self.assertEqual(convert.call_count, 0)


class Failures(unittest.TestCase):
    def run_index(self, root, source, kind, reader):
        config = DEFAULTS | {'folders': [str(root)], 'scope': 'folders', 'exclude': [], 'media': False}
        with patch('cati.indexer.extract', side_effect=reader):
            return index(config, db_path=root / 'index.db', engine=object(), candidates=[(source, kind)], report_status=False)

    def test_file_deleted_while_read_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); source = root / 'note.txt'; source.write_text('texto')
            def vanishing(path, *args, **kwargs):
                path.unlink()
                raise FileNotFoundError(path)
                yield
            result = self.run_index(root, source, 'document', vanishing)
            self.assertEqual((result['errors'], result.get('changed')), (0, 1))
            with opened(root / 'index.db') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM files').fetchone())
            self.assertFalse((root / 'index-errors.log').exists())

    def test_half_written_file_is_retried_not_cached_as_failure(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); source = root / 'shot.png'; source.write_bytes(b'\x89PNG')
            def truncated(path, *args, **kwargs):
                path.write_bytes(b'\x89PNG and the rest of the picture')
                raise OSError('image file is truncated')
                yield
            result = self.run_index(root, source, 'image', truncated)
            self.assertEqual((result['errors'], result.get('changed')), (0, 1))
            with opened(root / 'index.db') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM files WHERE error IS NOT NULL').fetchone())


class GeneratedMirrors(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.project = Path(self.work.name) / 'app'
        for source in ['src/ui/button.tsx', 'src/ui/badge.tsx', 'src/lib/api.ts', 'src/lib/db.ts', 'vite.config.ts', 'README.md']:
            (self.project / source).parent.mkdir(parents=True, exist_ok=True)
            (self.project / source).write_text('x')

    def tearDown(self):
        self.work.cleanup()

    def write(self, folder, names):
        for name in names:
            (self.project / folder / name).parent.mkdir(parents=True, exist_ok=True)
            (self.project / folder / name).write_text('nota')
        return str(self.project / folder)

    def test_note_per_source_copy_is_skipped_everywhere(self):
        cards = self.write('cards', ['INDEX.md', 'vite.config.md', 'src/ui/button.md', 'src/ui/badge.md', 'src/lib/api.md', 'src/lib/db.md'])
        self.assertTrue(readers.mirror(cards))
        config = DEFAULTS | {'folders': [str(self.project)], 'exclude': [], 'code': False, 'media': False}
        walked = {str(p) for p, _ in readers.walk(config)}
        self.assertFalse(any(p.startswith(cards) for p in walked))
        self.assertIn(str(self.project / 'README.md'), walked)

    def test_handwritten_notes_are_not_mistaken_for_a_copy(self):
        self.assertFalse(readers.mirror(self.write('docs', ['guia.md', 'api.md', 'despliegue.md', 'faq.md', 'diseño.md', 'img.png'])))
        self.assertFalse(readers.mirror(self.write('notas', ['api.md', 'db.md'])))  # too small to tell
        self.assertFalse(readers.mirror(self.write('apuntes', ['src/ui/ideas.md', 'src/ui/pendiente.md', 'src/lib/dudas.md',
                                                             'src/lib/plan.md', 'src/lib/notas.md'])))  # no source behind them


if __name__ == '__main__':
    unittest.main()
