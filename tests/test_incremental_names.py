"""Folder recall, bounded query caching and event-scoped indexing regressions."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cati import catalog
from cati.config import DEFAULTS
from cati.query import parse
from cati.search import Searcher
from cati.store import connect
from cati.watch import Watcher


class IncrementalNames(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base/'documents'
        self.root.mkdir()
        self.dbpath = self.base/'index.db'
        self.db = connect(self.dbpath)
        self.search = Searcher(self.dbpath)
        self.config = DEFAULTS | {'scope':'folders', 'folders':[str(self.root)], 'exclude':[], 'media':False}
        self.watcher = Watcher(self.config, self.dbpath, engine=object(), status=False)

    def tearDown(self):
        self.watcher.inotify.close()
        self.search.db.close()
        self.db.close()
        self.temp.cleanup()

    def test_folder_survives_thousands_of_parent_matches(self):
        target = self.root/'volcano'
        target.mkdir()
        # Insert folder last, beyond the old global candidate cutoff.
        rows = [(str(target/f'note-{i}.txt'), 'note txt', 'volcano', '.txt', 0, 1, 1, 1) for i in range(4000)]
        rows.append(catalog.row(self.root, target, True, 0, 1, 1))
        catalog.upsert(self.db, rows)
        found = self.search.run('volcano', exact=True, semantic=False).hits
        self.assertEqual(found[0]['path'], str(target))

    def test_complete_name_survives_common_single_word_matches(self):
        rows = [(str(self.root/f'common-{i}.txt'), 'volcano', '', '.txt', 0, 1, 1, 1) for i in range(4000)]
        target = self.root/'volcano atlas.txt'
        target.write_text('atlas')
        rows.append(catalog.row(self.root, target, False, 5, 1, 1))
        catalog.upsert(self.db, rows)
        found = self.search.run('volcano atlas', exact=True, semantic=False).hits
        self.assertEqual(found[0]['path'], str(target))

    def test_cache_reused_invalidated_and_bounded(self):
        target = self.root/'volcano'
        target.mkdir()
        catalog.touch(self.db, [self.root], [target])
        intent = parse('volcano')
        first = self.search.by_name(intent, intent.terms)
        statements = []
        self.search.db.set_trace_callback(statements.append)
        self.assertIs(first, self.search.by_name(intent, intent.terms))
        self.assertFalse(any('catalog_names MATCH' in s for s in statements))
        target.rmdir()
        catalog.touch(self.db, [self.root], [target])
        self.assertNotIn(str(target), self.search.by_name(intent, intent.terms))
        for i in range(25):
            intent = parse(f'zzzz{i}')
            self.search.by_name(intent, intent.terms)
        self.assertLessEqual(len(self.search.name_results), 16)
        self.assertLessEqual(sum(map(len, self.search.name_results.values())), 6000)

    def test_identical_event_does_not_write_or_invalidate_cache(self):
        target = self.root/'volcano'
        target.mkdir()
        catalog.touch(self.db, [self.root], [target])
        changes = self.db.total_changes
        catalog.touch(self.db, [self.root], [target])
        self.assertEqual(self.db.total_changes, changes)

    def test_subtree_scan_does_not_walk_or_remove_other_folders(self):
        other = self.root/'untouched'
        other.mkdir()
        (other/'preserved.txt').write_text('preserved')
        self.watcher.scan()
        target = self.root/'new'/'nested'
        target.mkdir(parents=True)
        source = target/'fresh.txt'
        source.write_text('fresh')
        walked = []
        original = catalog.os.scandir
        def scan(path):
            walked.append(Path(path))
            return original(path)
        with patch('cati.catalog.os.scandir', side_effect=scan):
            self.watcher.scan([target.parent])
        self.assertNotIn(other, walked)
        self.assertNotIn(self.root, walked)
        self.assertIn(source, self.watcher.pending)
        rows = {r['path']:r for r in self.db.execute('SELECT * FROM catalog')}
        self.assertIn(str(other/'preserved.txt'), rows)
        self.assertEqual(rows[str(source)]['folders'], 'new nested')
        source.unlink();target.rmdir();target.parent.rmdir()
        self.watcher.scan([target.parent])
        paths = {r[0] for r in self.db.execute('SELECT path FROM catalog')}
        self.assertNotIn(str(source), paths)
        self.assertIn(str(other/'preserved.txt'), paths)

    def test_excluded_events_do_not_enter_catalog(self):
        secret = self.root/'excluded'
        secret.mkdir()
        source = secret/'private.txt';source.write_text('private')
        self.watcher.config['exclude'] = [str(secret)]
        self.watcher.touched.update([secret, source])
        self.watcher.flush_catalog(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM catalog').fetchone()[0], 0)

    def test_live_directory_move_uses_subtree_updates(self):
        self.watcher.scan()
        self.watcher.full_scan = False
        self.watcher.rescan.clear()
        listener = threading.Thread(target=self.watcher.listen)
        listener.start()
        try:
            target = self.root/'arrival'
            target.mkdir()
            (target/'fresh.txt').write_text('fresh')
            deadline = time.monotonic()+3
            while not self.watcher.rescan.is_set() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(self.watcher.rescan.is_set())
            self.assertFalse(self.watcher.full_scan)
            self.assertIn(target, self.watcher.subtrees)
            self.watcher.scan([target])
            destination = self.root/'renamed'
            target.rename(destination)
            deadline = time.monotonic()+3
            while destination not in self.watcher.subtrees and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertIn(destination, self.watcher.subtrees)
            self.watcher.scan([target, destination])
            paths = {r[0] for r in self.db.execute('SELECT path FROM catalog')}
            self.assertIn(str(destination/'fresh.txt'), paths)
            self.assertNotIn(str(target/'fresh.txt'), paths)
            self.assertIn(destination, self.watcher.directories)
            self.assertNotIn(target, self.watcher.directories)
        finally:
            self.watcher.stop.set()
            listener.join(3)
            self.assertFalse(listener.is_alive())


if __name__ == '__main__':
    unittest.main()
