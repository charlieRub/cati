"""Search performance invariants and cache correctness, without a live model."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import numpy as np
from cati.search import Searcher
from cati.store import connect, next_segment_id

class SearchEfficiency(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'index.db'
        self.writer = connect(self.path)
        with self.writer:
            self.writer.execute("INSERT INTO files(path,mtime,size,kind) VALUES(?,1,1,'document')", (str(self.root/'note.txt'),))
        self.search = Searcher(self.path)

    def tearDown(self):
        self.search.full = None
        self.search.db.close()
        self.writer.close()
        for attr in ('_lock', '_private'):
            if hasattr(self.search, attr): getattr(self.search, attr).close()
        self.temp.cleanup()

    def insert(self, vector):
        with self.writer:
            ident = int(next_segment_id(self.writer))
            self.writer.execute("INSERT INTO segments(id,file,text,kind,vec) VALUES(?,1,'note','text',?)", (ident, vector.astype('<f4').tobytes()))
        return ident

    def test_lexical_and_recent_never_load_vectors(self):
        with patch.object(self.search, 'refresh_vectors', side_effect=AssertionError('unnecessary vectors')):
            self.search.run('note', semantic=False, exact=True)
            self.search.recent()
            self.search.warm()
        if self.search.spelling.thread: self.search.spelling.thread.join(5)
        self.assertEqual(self.search.size, 0)
        self.assertFalse(self.path.with_suffix('.vectors.f16').exists())

    def test_semantic_refresh_after_metadata_refresh(self):
        self.insert(np.ones(768))
        self.search.refresh(vectors=False)
        self.search.refresh()
        self.assertEqual(self.search.size, 1)
        self.insert(-np.ones(768))
        self.search.refresh(vectors=False)
        self.search.refresh()
        self.assertEqual(self.search.size, 2)

    def test_replacing_last_segment_and_restart_updates_vectors(self):
        first = self.insert(np.ones(768))
        self.search.refresh()
        with self.writer: self.writer.execute('DELETE FROM segments')
        second = self.insert(-np.ones(768))
        self.assertGreater(second, first)
        self.search.refresh()
        np.testing.assert_array_equal(self.search.ids[self.search.alive], [second])
        np.testing.assert_array_equal(self.search.matrix()[self.search.alive], -np.ones((1,768)))
        self.search.save_snapshot()
        self.search._lock.close()
        restarted = Searcher(self.path)
        try:
            restarted.refresh()
            np.testing.assert_array_equal(restarted.ids[restarted.alive], [second])
            np.testing.assert_array_equal(restarted.matrix()[restarted.alive], -np.ones((1,768)))
        finally:
            restarted.full=None; restarted.db.close(); restarted._lock.close()

    def test_release_and_reload_preserves_index_and_new_changes(self):
        first = self.insert(np.ones(768))
        self.search.refresh()
        self.search.release_vectors()
        self.assertEqual(self.search.size, 0)
        self.assertIsNone(self.search.full)
        self.assertEqual(len(self.search.columns['bits']), 0)
        self.search.release_vectors()  # idempotent
        second = self.insert(-np.ones(768))
        self.search.refresh()
        np.testing.assert_array_equal(self.search.ids[self.search.alive], [first, second])
        np.testing.assert_array_equal(self.search.matrix()[1], -np.ones(768))

    def test_second_process_reads_shared_cache_without_copying_it(self):
        first = self.insert(np.ones(768))
        self.search.refresh()
        self.search.save_snapshot()
        second = self.insert(-np.ones(768))
        reader = Searcher(self.path)
        try:
            self.assertFalse(reader.owner())
            reader.refresh()
            self.assertEqual(reader.shared_rows, 1)  # the owner's rows are mapped, only the newer one is private
            np.testing.assert_array_equal(reader.ids, [first, second])
            np.testing.assert_array_equal(reader.exact(np.array([0, 1]), np.ones(768, np.float32)), [768, -768])
            self.assertEqual(list(self.root.glob('cati-vectors-*')), [])  # anonymous: nothing to leak if killed
        finally:
            reader.release_vectors(); reader.db.close(); reader._lock.close()

    def test_reader_rejects_cache_rewritten_after_snapshot(self):
        self.insert(np.ones(768))
        self.search.refresh()
        self.search.save_snapshot()
        self.search.vectors_path().write_bytes((-np.ones((1, 768), np.float16)).tobytes())
        reader = Searcher(self.path)
        try:
            reader.refresh()
            self.assertEqual(reader.shared_rows, 0)  # sign bits disagree: rebuilt privately from the index
            np.testing.assert_array_equal(reader.exact(np.array([0]), np.ones(768, np.float32)), [768])
        finally:
            reader.release_vectors(); reader.db.close(); reader._lock.close()

    def test_owner_rebuild_never_truncates_a_mapped_cache(self):
        self.insert(np.ones(768))
        self.search.refresh()
        mapped = self.search.matrix()
        self.search.release_vectors()
        self.search.snapshot_path().unlink()
        self.search.refresh()  # no snapshot: rebuilt into a fresh file
        np.testing.assert_array_equal(mapped[0], np.ones(768))  # the old mapping still reads valid data

    def test_owner_removes_named_copies_left_by_killed_processes(self):
        stale = self.root / 'cati-vectors-old'; stale.write_bytes(b'x')
        fresh = self.root / 'cati-vectors-new'; fresh.write_bytes(b'x')
        import os, time
        os.utime(stale, (time.time() - 3600,) * 2)
        Searcher.sweep(self.root)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_window_rebuilds_file_table_beside_searches(self):
        self.search.refresh(vectors=False)
        self.search.background_refresh = True
        with self.writer:
            self.writer.execute("INSERT INTO files(path,mtime,size,kind) VALUES(?,1,1,'document')", (str(self.root/'new.txt'),))
        self.search.refresh(vectors=False)  # answers with the current table at once
        self.search.rebuilding.join(5)
        self.assertEqual(self.search.files.lookup([str(self.root/'new.txt')]).keys(), {str(self.root/'new.txt')})
        built = self.search.rebuilding
        self.search.refresh(vectors=False)
        self.assertIs(self.search.rebuilding, built)  # nothing changed: no new rebuild
        real_load = Searcher.load
        def commit_meanwhile(db):
            loaded = real_load(db)
            late = connect(self.path)  # another process commits while the table is being built
            with late: late.execute("INSERT INTO files(path,mtime,size,kind) VALUES(?,1,1,'document')", (str(self.root/'late.txt'),))
            late.close()
            return loaded
        with self.writer:
            self.writer.execute("INSERT INTO files(path,mtime,size,kind) VALUES(?,1,1,'document')", (str(self.root/'other.txt'),))
        with patch.object(Searcher, 'load', side_effect=commit_meanwhile):
            self.search.refresh(vectors=False); self.search.rebuilding.join(5)
        self.assertNotIn(str(self.root/'late.txt'), self.search.files.lookup([str(self.root/'late.txt')]))
        self.search.refresh(vectors=False); self.search.rebuilding.join(5)  # the late commit is noticed
        self.assertIn(str(self.root/'late.txt'), self.search.files.lookup([str(self.root/'late.txt')]))

    def test_image_query_invalidates_when_source_changes(self):
        source=self.root/'image.png';source.write_bytes(b'first')
        self.search.engine=Mock()
        self.search.engine.media.side_effect=[np.ones(768), -np.ones(768)]
        with patch('cati.search.image', return_value=source):
            first=self.search.query_vector('',False,source)
            self.assertIs(first,self.search.query_vector('',False,source))
            source.write_bytes(b'other')
            second=self.search.query_vector('',False,source)
        self.assertEqual(self.search.engine.media.call_count,2)
        np.testing.assert_array_equal(second,-first)

    def test_file_lookup_uses_index(self):
        plan=' '.join(str(tuple(r)) for r in self.writer.execute('EXPLAIN QUERY PLAN SELECT kind FROM segments WHERE file=1'))
        self.assertIn('segments_file',plan)
        self.assertNotIn('SCAN segments',plan)

    def test_scoped_candidates_include_files_beyond_global_cutoff(self):
        s=self.search
        n=20000
        s.allocate(n);s.size=n;s.views()
        s.columns['ids'][:]=np.arange(1,n+1)
        s.columns['alive'][:]=True
        s.columns['seg_file'][:]=1
        s.columns['seg_file'][-1]=2
        s.columns['bits'][:]=255
        s.columns['bits'][-1]=0
        s.file_code=np.zeros(3,bool)
        s.files={1:('elsewhere','document',0,0,'document'),2:('target','document',0,0,'document')}
        seen=[]
        def exact(rows,vector):
            seen.extend(rows.tolist())
            return np.where(rows==n-1,.99,.1).astype(np.float32)
        with patch.object(s,'exact',side_effect=exact):
            found,_=s.by_meaning(np.ones(768),False,lambda fid:fid==2,False,scoped=True)
        self.assertIn(n-1,seen)
        self.assertIn(2,found)
        self.assertNotIn(1,found)

if __name__=='__main__': unittest.main()
