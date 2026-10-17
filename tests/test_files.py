"""Navegación, uso, duplicados, espacio y acciones en lote: sin modelo, con carpetas temporales."""
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cati import batch, browse, disk, saved, usage
from cati.query import Intent
from cati.search import Searcher, apply_filters
from cati.store import connect


def fill(db, folder):
    for base, dirs, names in os.walk(folder):
        for name in dirs + names:
            path = Path(base) / name
            info = path.stat()
            db.execute('INSERT INTO catalog(path,name,folders,ext,is_dir,size,mtime) VALUES(?,?,?,?,?,?,?)',
                       (str(path), name, '', path.suffix.lower(), int(path.is_dir()), 0 if path.is_dir() else info.st_size, info.st_mtime_ns))
    db.commit()


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'data'
        self.root.mkdir()
        self.db = Path(self.tmp.name) / 'index.sqlite3'
        for sub in ('a', 'b', 'a/deep'): (self.root / sub).mkdir()
        (self.root / 'a/one.txt').write_text('x' * 9000)
        (self.root / 'b/copy.txt').write_text('x' * 9000)
        (self.root / 'a/deep/third.txt').write_text('x' * 9000)
        (self.root / 'a/other.txt').write_text('y' * 9000)          # same size, different content
        (self.root / 'b/small.txt').write_text('z')
        os.link(self.root / 'a/one.txt', self.root / 'b/hard.txt')  # hard link: not a duplicate
        connect(self.db).close()
        db = connect(self.db); fill(db, self.root); db.close()

    def tearDown(self): self.tmp.cleanup()

    def test_listing_puts_folders_first_and_hides_dotfiles(self):
        (self.root / '.hidden').write_text('h')
        names = [h['name'] for h in browse.listing(self.root)]
        self.assertEqual(names[:2], ['a', 'b'])
        self.assertNotIn('.hidden', names)
        self.assertIn('.hidden', [h['name'] for h in browse.listing(self.root, hidden=True)])
        biggest = browse.listing(self.root / 'a', sort='size')
        self.assertEqual(biggest[0]['kind'], 'folder')
        self.assertGreaterEqual(biggest[1]['size'], biggest[-1]['size'])

    def test_crumbs_reach_home_or_root(self):
        crumbs = browse.crumbs(Path.home() / 'x/y')
        self.assertEqual([c[0] for c in crumbs], ['~', 'x', 'y'])
        self.assertEqual(browse.crumbs(self.root)[0][0], '/')

    def test_duplicates_ignore_hard_links_and_same_size_different_content(self):
        groups = disk.duplicates(min_size=100, db_path=self.db)
        self.assertEqual(len(groups), 1)
        names = sorted(Path(p).name for p in groups[0]['paths'])
        self.assertEqual(len(names), 3)
        self.assertEqual(len({'one.txt', 'hard.txt'} & set(names)), 1)  # same inode: only one of them counts
        self.assertEqual(disk.reclaimable(groups), 9000 * 2)
        self.assertEqual(len(disk.spare_copies(groups)), 2)
        inside = disk.duplicates(min_size=100, folder=self.root / 'b', db_path=self.db)   # limited to one folder
        self.assertEqual(sorted(Path(p).name for p in inside[0]['paths']), ['copy.txt', 'hard.txt'])

    def test_usage_sums_children(self):
        rows, total = disk.usage(self.root, db_path=self.db)
        by_name = {Path(p).name: (is_dir, size) for p, is_dir, size, _ in rows}
        self.assertEqual(by_name['a'], (True, 27000))
        self.assertTrue(by_name['b'][0])
        self.assertEqual(rows[0][0].rsplit('/', 1)[-1], 'a')
        self.assertEqual(total, 27000 + 9001 + 9000)
        inner, _ = disk.usage(self.root / 'a', db_path=self.db)
        self.assertEqual({Path(p).name for p, *_ in inner}, {'deep', 'one.txt', 'other.txt'})

    def test_usage_falls_back_to_disk_for_unindexed_folders(self):
        outside = self.root / 'new'; outside.mkdir(); (outside / 'f.bin').write_bytes(b'0' * 500)
        rows, total = disk.usage(self.root / 'new', db_path=self.db)
        self.assertEqual((Path(rows[0][0]).name, total), ('f.bin', 500))

    def test_usage_history_and_moves(self):
        target = self.root / 'a/one.txt'
        for _ in range(3): usage.record(target, self.db)
        usage.record(self.root / 'b/copy.txt', self.db)
        self.assertEqual(usage.rows('frequent', 5, self.db)[0][:2], (str(target), 3))
        self.assertEqual(usage.rows('recent', 5, self.db)[0][0], str(self.root / 'b/copy.txt'))
        usage.moved(self.root / 'a', self.root / 'z', self.db)   # folder moved: history follows (target no longer exists)
        with mock.patch('pathlib.Path.exists', return_value=True):
            self.assertIn(str(self.root / 'z/one.txt'), [r[0] for r in usage.rows('frequent', 5, self.db)])
        usage.forget(self.root / 'z', self.db)
        with mock.patch('pathlib.Path.exists', return_value=True):
            self.assertEqual([r[0] for r in usage.rows('frequent', 5, self.db)], [str(self.root / 'b/copy.txt')])

    def test_move_copy_undo_and_conflicts(self):
        dest = self.root / 'dest'; dest.mkdir()
        (dest / 'one.txt').write_text('already')
        out = batch.move([self.root / 'a/one.txt', self.root / 'a/other.txt'], dest)
        self.assertEqual(len(out.done), 1)
        self.assertEqual(out.errors[0][1], 'ya existe allí')
        self.assertEqual((dest / 'one.txt').read_text(), 'already')   # never overwritten
        out.undo()
        self.assertTrue((self.root / 'a/other.txt').exists())
        again = batch.copy([self.root / 'a/other.txt', self.root / 'a/deep'], dest)
        self.assertEqual(sorted(Path(p).name for p in again.done), ['deep', 'other.txt'])
        twice = batch.copy([self.root / 'a/other.txt'], dest)
        self.assertEqual(Path(twice.done[0]).name, 'other (2).txt')
        again.undo()
        self.assertFalse((dest / 'deep').exists())
        self.assertEqual(batch.move([self.root / 'a'], self.root / 'a/deep').errors[0][1], 'no se puede mover una carpeta dentro de sí misma')

    def test_compress_keeps_structure_and_names_it_sensibly(self):
        out = batch.compress([self.root / 'a'])
        self.assertEqual(Path(out.done[0]).name, 'a.zip')
        with zipfile.ZipFile(out.done[0]) as archive: names = set(archive.namelist())
        self.assertTrue({'a/one.txt', 'a/deep/third.txt'} <= names)
        multiple = batch.compress([self.root / 'a/one.txt', self.root / 'b/small.txt'])
        self.assertEqual(Path(multiple.done[0]).name, 'Archivo.zip')
        out.undo()
        self.assertFalse(Path(out.done[0]).exists())

    def test_rename_plan_flags_problems_and_never_overwrites(self):
        files = [self.root / 'a/one.txt', self.root / 'a/other.txt']
        plan = batch.plan_rename(files, 'number', 'Foto {n:02}', start=5)
        self.assertEqual([n for _, n, _ in plan], ['Foto 05.txt', 'Foto 06.txt'])
        self.assertTrue(all(p is None for *_, p in plan))
        clash = batch.plan_rename(files[:1], 'replace', 'one', 'other')
        self.assertEqual(clash[0][2], 'ya existe')
        same = batch.plan_rename(files, 'number', 'igual')
        self.assertEqual(same[0][2], 'repetido en la lista')
        self.assertEqual(batch.plan_rename([files[0]], 'replace', '', 'x')[0][2], 'sin cambios')
        self.assertEqual(batch.plan_rename([files[0]], 'replace', 'one', 'a/b')[0][2], 'nombre no válido')
        out = batch.rename(batch.plan_rename(files, 'case', 'upper'))
        self.assertTrue((self.root / 'a/ONE.txt').exists())
        out.undo()
        self.assertTrue((self.root / 'a/one.txt').exists())
        self.assertEqual(batch.new_name('Mi Foto.JPG', 0, 'affix', 'IMG_', '_ok'), 'IMG_Mi Foto_ok.JPG')

    def test_search_filters_scope_and_live_scan(self):
        searcher = Searcher(self.db)
        try:
            self.assertTrue(searcher.covers(self.root) and searcher.covers(self.root / 'a'))
            self.assertFalse(searcher.covers(self.root / 'a/deep/nada'))
            inside = searcher.run('one', filters={'folder': str(self.root / 'a')}, semantic=False)
            self.assertEqual([h['name'] for h in inside.hits], ['one.txt'])
            outside = searcher.run('one', filters={'folder': str(self.root / 'b')}, semantic=False)
            self.assertEqual(outside.hits, [])
            only_big = searcher.run('', filters={'folder': str(self.root), 'size': 'small'}, semantic=False)
            self.assertTrue(all(h['size'] < 1 << 20 for h in only_big.hits if h['kind'] != 'folder'))
            # A folder the index does not walk is matched live, by name.
            fresh = self.root.parent / 'fresh'
            fresh.mkdir()
            (fresh / 'Informe final.txt').write_text('x')
            (fresh / 'otro.txt').write_text('x')
            live = searcher.run('informe', filters={'folder': str(fresh)}, semantic=False)
            self.assertEqual([h['name'] for h in live.hits], ['Informe final.txt'])
            self.assertEqual(searcher.run('informe', filters={'folder': str(fresh), 'size': 'huge'}, semantic=False).hits, [])
        finally:
            searcher.db.close()

    def test_saved_searches_roundtrip(self):
        with mock.patch.object(saved, 'PATH', self.root / 'saved.json'), mock.patch.object(saved, 'DATA', self.root):
            entry = saved.add('Facturas', 'facturas', {'period': 'year', 'kind': '', 'folder': '/x'})
            self.assertEqual(saved.load()[0]['filters'], {'period': 'year', 'folder': '/x'})
            saved.remove(entry['id'])
            self.assertEqual(saved.load(), [])
        self.assertEqual(saved.suggest_name('facturas', {'period': 'year', 'folder': '/home/u/Docs'}), 'Facturas en Docs este año')

    def test_filters_shape_the_intent(self):
        intent = apply_filters(Intent(raw=''), {'folder': str(self.root), 'period': 'week', 'size': 'big', 'kind': 'pdf'}, now=1_000_000_000)
        self.assertEqual(intent.location, self.root.resolve())
        self.assertEqual(intent.after, int((1_000_000_000 - 7 * 86400) * 1e9))
        self.assertEqual((intent.min_size, intent.categories), (10 << 20, {'pdf'}))
        today = apply_filters(Intent(raw=''), {'period': 'today'}, now=1_000_000_000)
        self.assertLessEqual(today.after, 1_000_000_000 * 10**9)
        self.assertGreater(today.after, (1_000_000_000 - 86400) * 10**9)


if __name__ == '__main__':
    unittest.main()
