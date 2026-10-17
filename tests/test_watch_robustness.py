"""Vigilancia robusta: cola sin bloqueos, vigilancias que siguen a las carpetas y servicio sin huérfanos."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cati import catalog, service
from cati.config import DEFAULTS
from cati.store import connect
from cati.watch import Watcher


class WatchRobustness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base/'docs'
        self.root.mkdir()
        self.dbpath = self.base/'index.db'
        self.config = DEFAULTS | {'scope':'folders', 'folders':[str(self.root)], 'exclude':[], 'media':False}
        self.watcher = Watcher(self.config, self.dbpath, engine=object(), status=False)

    def tearDown(self):
        self.watcher.inotify.close()
        for folder in self.root.rglob('*'):
            if folder.is_dir(): folder.chmod(0o700)
        self.temp.cleanup()

    def test_debounced_event_does_not_block_due_jobs(self):
        # A file rewritten every second keeps a priority-0 job always two seconds away.
        self.watcher.enqueue(self.root/'log.txt', 0, 2)
        self.watcher.enqueue(self.root/'backlog.txt', 10)
        self.assertEqual(self.watcher.take(), self.root/'backlog.txt')
        self.assertIsNone(self.watcher.take())
        with patch('cati.watch.time.monotonic', return_value=time.monotonic()+3):
            self.assertEqual(self.watcher.take(), self.root/'log.txt')
        self.assertEqual(self.watcher.pending, {})

    def test_repeated_events_do_not_grow_queue(self):
        for _ in range(20000): self.watcher.enqueue(self.root/'busy.txt', 0, 2)
        self.assertEqual(len(self.watcher.pending), 1)
        self.assertLess(len(self.watcher.heap)+len(self.watcher.later), 5000)
        self.assertIn(self.root/'busy.txt', self.watcher.pending)

    def test_moved_folder_keeps_its_watch_in_any_order(self):
        source = self.root/'project'
        (source/'inner').mkdir(parents=True)
        self.watcher.scan()
        wd = self.watcher.directories[source]
        destination = self.root/'renamed'
        source.rename(destination)
        # The destination arrives first; the old name's scan comes in a later batch.
        self.watcher.scan([destination])
        self.watcher.scan([source])
        self.assertEqual(self.watcher.directories[destination], wd)
        self.assertEqual(self.watcher.watches[wd], destination)
        self.assertNotIn(source, self.watcher.directories)
        self.assertNotIn(source/'inner', self.watcher.directories)
        self.assertIn(destination/'inner', self.watcher.directories)
        (destination/'inner'/'new.txt').write_text('x')
        events = self.watcher.inotify.read(timeout=1000)
        self.assertTrue(any(self.watcher.watches.get(e.wd) == destination/'inner' for e in events))

    def test_full_scan_releases_stale_watches(self):
        sdk, gone, hidden = self.root/'Sdk', self.root/'gone', self.root/'visible'
        for folder in (sdk/'platforms', gone, hidden): folder.mkdir(parents=True)
        self.watcher.scan()
        self.assertTrue({sdk, sdk/'platforms', gone, hidden} <= set(self.watcher.directories))
        (sdk/'.knownPackages').write_text('')
        gone.rmdir()
        hidden.rename(self.root/'.hidden')
        self.config['exclude'] = []
        self.watcher.scan()
        for folder in (sdk, sdk/'platforms', gone, hidden):
            self.assertNotIn(folder, self.watcher.directories)
        self.assertEqual(set(self.watcher.watches.values()), set(self.watcher.directories))
        self.assertFalse(self.watcher.permitted(sdk/'platforms'/'icon.png'))

    @unittest.skipIf(os.geteuid() == 0, 'root ignora los permisos')
    def test_unreadable_folder_keeps_its_rows(self):
        folder = self.root/'mount'
        folder.mkdir()
        (folder/'report.txt').write_text('quarterly')
        self.watcher.scan()
        db = connect(self.dbpath)
        with db: db.execute("INSERT INTO files(path,mtime,size,kind,options) VALUES(?,0,0,'document','')", (str(folder/'report.txt'),))
        folder.chmod(0)  # a stalled network mount or an I/O error looks the same to scandir
        self.watcher.scan()
        names = {r[0] for r in db.execute('SELECT path FROM catalog')}
        self.assertIn(str(folder/'report.txt'), names)
        self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0], 1)
        folder.chmod(0o700)
        (folder/'report.txt').unlink()
        self.watcher.scan()
        self.assertNotIn(str(folder/'report.txt'), {r[0] for r in db.execute('SELECT path FROM catalog')})
        db.close()

    def test_permitted_matches_the_catalog_walk(self):
        bundle = self.root/'Tool.app'
        bundle.mkdir()
        (bundle/'readme.txt').write_text('x')
        (self.root/'plain.txt').write_text('x')
        self.assertFalse(self.watcher.permitted(bundle/'readme.txt'))
        self.assertTrue(self.watcher.permitted(self.root/'plain.txt'))
        self.assertTrue(self.watcher.permitted(self.root))
        self.assertFalse(self.watcher.permitted(self.base/'docsx'/'a.txt'))
        self.assertFalse(self.watcher.permitted(self.root/'node_modules'/'a.txt'))
        db = connect(self.dbpath)
        catalog.sync(db, [str(self.root)])
        self.assertEqual({r[0] for r in db.execute('SELECT path FROM catalog')}, {str(self.root/'plain.txt')})
        db.close()

    def test_full_scan_forgets_old_retries(self):
        self.watcher.retry_after[self.root/'old.pdf'] = time.monotonic()-1
        self.watcher.failures[self.root/'old.pdf'] = 3
        self.watcher.scan()
        self.assertEqual(self.watcher.retry_after, {})
        self.assertEqual(self.watcher.failures, {})

    def test_status_written_on_change_or_heartbeat(self):
        self.watcher.status_enabled = True
        with patch('cati.watch.write_status') as write:
            for _ in range(5): self.watcher.report()
            self.assertEqual(write.call_count, 1)
            self.watcher.phase = 'watching'
            self.watcher.report()
            self.assertEqual(write.call_count, 2)


class ServiceProcesses(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(service, 'DATA', Path(self.temp.name)), patch.object(service, 'systemd', return_value=False)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.temp.cleanup()

    def test_reused_pid_is_not_ours(self):
        service.pidfile('watch').write_text('1')
        self.assertIsNone(service.running('watch'))
        service.stop('watch')  # must not signal an unrelated process
        self.assertFalse(service.pidfile('watch').exists())

    def test_stop_waits_and_kills_a_stuck_process(self):
        script = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(flush=True); time.sleep(60)'
        child = subprocess.Popen([sys.executable, '-c', script, 'cati', 'watch'], stdout=subprocess.PIPE)
        child.stdout.readline()  # SIGTERM is ignored from here on
        child.stdout.close()
        service.pidfile('watch').write_text(str(child.pid))
        deadline = time.monotonic()+5
        while not service.running('watch') and time.monotonic() < deadline: time.sleep(.05)
        self.assertEqual(service.running('watch'), child.pid)
        began = time.monotonic()
        service.stop('watch', wait=1)
        self.assertEqual(child.wait(5), -9)
        self.assertLess(time.monotonic()-began, 5)


if __name__ == '__main__':
    unittest.main()
