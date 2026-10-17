"""Regresiones de caché, reanudación y límites; no necesitan un modelo local."""
import os
import tempfile
import threading
import time
import subprocess
import wave
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from cati.config import DEFAULTS
from cati.indexer import index
from cati.store import opened
from cati.textcache import DocumentTextCache
from cati.watch import Watcher
from cati.engine import Engine


class Efficiency(unittest.TestCase):
    def test_unchanged_read_errors_are_cached_across_watcher_restarts(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);source=root/'broken.xlsx';source.write_text('not a ZIP archive')
            config=DEFAULTS|{'folders':[str(root)],'scope':'folders','exclude':[],'media':False}
            result=index(config,db_path=root/'index.db',engine=object(),candidates=[(source,'document')],report_status=False)
            self.assertEqual(result['errors'],1)
            watcher=Watcher(config,root/'index.db',engine=object(),status=False)
            try:
                watcher.scan()
                self.assertEqual(watcher.pending,{})
                source.chmod(0o640)  # ctime changes even though size/mtime do not.
                watcher.scan()
                self.assertIn(source,watcher.pending)
            finally:watcher.inotify.close()
            self.assertIn('broken.xlsx',(root/'index-errors.log').read_text())  # beside its own index, never the user's

    def test_file_changing_while_read_is_retried_not_cached_as_failure(self):
        from cati.indexer import extract as real_extract
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);source=root/'note.txt';source.write_text('primera versión')
            config=DEFAULTS|{'folders':[str(root)],'scope':'folders','exclude':[],'media':False}
            def growing(path,*args,**kwargs):
                path.write_text('segunda versión, más larga')
                yield from real_extract(path,*args,**kwargs)
            engine=type('Engine',(),{'text':lambda self,texts,titles=None:[np.ones(768)]*len(texts)})()
            with patch('cati.indexer.extract',side_effect=growing):
                result=index(config,db_path=root/'index.db',engine=engine,candidates=[(source,'document')],report_status=False)
            self.assertEqual((result['errors'],result.get('changed')),(0,1))
            with opened(root/'index.db') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM files WHERE error IS NOT NULL').fetchone())

    def test_video_tail_without_audio_does_not_generate_empty_model_input(self):
        from cati.readers import extract
        with tempfile.TemporaryDirectory() as work:
            source=Path(work)/'video.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=64x64:r=1:d=27',
                            '-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],check=True)
            frames=[]
            for segment in extract(source,'video',DEFAULTS,cursor=(1,0)):
                with wave.open(segment.media,'rb') as audio: frames.append(audio.getnframes())
            self.assertEqual(len(frames),1)
            self.assertGreater(frames[0],0)
            self.assertEqual(list(extract(source,'video',DEFAULTS,cursor=(1,1))),[])

    def test_background_cooldown_does_not_block_interactive_search(self):
        background=Engine.__new__(Engine);background.background=True;background.config={'background_duty':.25}
        foreground=Engine.__new__(Engine);foreground.background=False
        background._encode=lambda _: 'indexed'
        foreground._encode=lambda _: 'search result'
        cooling=threading.Event();release=threading.Event();results=[]
        def cooldown(_):
            cooling.set()
            release.wait(3)
        with patch('cati.engine.yield_to_search'),patch('cati.engine.mark_search'),patch('cati.engine.time.sleep',side_effect=cooldown):
            thread=threading.Thread(target=lambda:results.append(background.encode(['document'])))
            thread.start()
            try:
                self.assertTrue(cooling.wait(2))
                self.assertEqual(foreground.encode(['query']),'search result')
                self.assertEqual(results,[])
            finally:release.set();thread.join(timeout=3)
        self.assertEqual(results,['indexed'])

    def test_failing_engine_does_not_busy_retry(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work); source=root/'document.txt'; source.write_text('source')
            config=DEFAULTS|{'folders':[str(root)],'scope':'folders','exclude':[],'media':False,'job_delay':0}
            watcher=Watcher(config,root/'index.db',engine=object(),status=False)
            watcher.enqueue(source)
            called=threading.Event()
            def fail(*args,**kwargs):
                called.set()
                raise RuntimeError('temporary engine failure')
            with patch('cati.watch.index',side_effect=fail) as indexer:
                thread=threading.Thread(target=watcher.work,args=('test',));thread.start()
                try:
                    self.assertTrue(called.wait(3))
                    time.sleep(1.2)
                    self.assertEqual(indexer.call_count,1)
                    self.assertGreater(watcher.pending[source][1],time.monotonic()+10)
                finally:
                    watcher.stop.set()
                    with watcher.condition:watcher.condition.notify_all()
                    thread.join(timeout=3);watcher.inotify.close()
                self.assertFalse(thread.is_alive())

    def test_persistent_cache_and_change_invalidation(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); source = root/'document.txt'; source.write_text('first')
            cache = DocumentTextCache(root/'cache')
            calls = []
            def extract(path):
                calls.append(path)
                return path.read_text()
            self.assertEqual(cache.get(source, extract), 'first')
            self.assertEqual(cache.get(source, extract), 'first')
            cold = DocumentTextCache(root/'cache')
            self.assertEqual(cold.get(source, extract), 'first')
            self.assertEqual(len(calls), 1)
            # Same length and restored mtime must still invalidate via ctime.
            before = source.stat(); source.write_text('other')
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(cold.get(source, extract), 'other')
            self.assertEqual(len(calls), 2)

    def test_memory_and_disk_eviction(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); cache = DocumentTextCache(root/'cache', memory_bytes=250, disk_bytes=300)
            for i in range(8):
                source = root/f'{i}.txt'; source.write_text(str(i)*100)
                self.assertEqual(cache.get(source, lambda p:p.read_text()), str(i)*100)
            self.assertLessEqual(cache.used, 250)
            self.assertLessEqual(sum(p.stat().st_size for p in (root/'cache').glob('*.txt')), 300)

    def test_failed_extraction_and_private_keys_are_not_cached(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work); source=root/'document.txt'; source.write_text('source')
            cache=DocumentTextCache(root/'cache')
            def fail(_): raise RuntimeError('read failed')
            with self.assertRaises(RuntimeError):cache.get(source, fail)
            with self.assertRaises(ValueError):cache.get(source, lambda _: '-----BEGIN RSA PRIVATE KEY-----')
            self.assertFalse(list((root/'cache').glob('*.txt')))
            self.assertEqual(cache.get(source, lambda _: 'recovered'), 'recovered')

    def test_resume_matches_full_index_without_reextracting(self):
        class Engine:
            def text(self, texts, *_):
                return [np.ones(768, dtype=np.float32)/np.sqrt(768) for _ in texts]
        with tempfile.TemporaryDirectory() as work:
            root=Path(work); source=root/'long.txt'
            source.write_text('\n'.join(f'Paragraph {i}: '+('Useful document content. '*80) for i in range(12)))
            config=DEFAULTS|{'folders':[str(root)],'scope':'folders','exclude':[],'media':False}
            cache=DocumentTextCache(root/'cache')
            from cati.readers import document
            with patch('cati.textcache.DOCUMENT_TEXT',cache), patch('cati.readers.document',wraps=document) as reader:
                for _ in range(60):
                    result=index(config,db_path=root/'partial.db',engine=Engine(),candidates=[(source,'document')],max_segments=2,report_status=False)
                    if result['done']:break
                else:self.fail('resumed document did not finish')
                self.assertEqual(reader.call_count,1)
                index(config,db_path=root/'full.db',engine=Engine(),candidates=[(source,'document')],report_status=False)
                with opened(root/'partial.db') as partial, opened(root/'full.db') as full:
                    actual=[tuple(r) for r in partial.execute('SELECT text,vec FROM segments ORDER BY id')]
                    expected=[tuple(r) for r in full.execute('SELECT text,vec FROM segments ORDER BY id')]
                self.assertGreater(len(actual),10)
                self.assertEqual(actual,expected)
                self.assertEqual(reader.call_count,1)


if __name__ == '__main__': unittest.main()
