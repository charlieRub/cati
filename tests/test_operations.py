"""Operaciones sobre archivos reales: nunca pisar nada, deshacer completo, papelera freedesktop, duplicados y voz.

Todo ocurre en carpetas temporales propias (bajo ~/.cache para estar en el mismo disco que la carpeta personal y
poder probar movimientos entre discos con /tmp); el historial de uso y la papelera real no se tocan."""
import errno
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cati import batch, browse, disk, properties, saved, voice, widgets
from cati.store import connect

PROJECT = Path(__file__).resolve().parent.parent


def fill(db, folder):
    for base, dirs, names in os.walk(folder):
        for name in dirs + names:
            path = Path(base) / name
            if path.is_symlink(): continue
            info = path.stat()
            db.execute('INSERT INTO catalog(path,name,folders,ext,is_dir,size,mtime) VALUES(?,?,?,?,?,?,?)',
                       (str(path), name, '', path.suffix.lower(), int(path.is_dir()), 0 if path.is_dir() else info.st_size, info.st_mtime_ns))
    db.commit()


def within(seconds, work):
    """Ejecuta `work` en un hilo; falla la prueba si se queda bloqueado (p. ej. leyendo una tubería)."""
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault('value', work()), daemon=True)
    thread.start(); thread.join(seconds)
    if thread.is_alive(): raise AssertionError(f'bloqueado más de {seconds} s')
    return result.get('value')


class Operations(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / '.cache'
        cache.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='cati-test-', dir=cache)
        self.root = Path(self.tmp.name)
        for target in ('moved', 'forget'):
            patcher = mock.patch.object(batch.usage, target)
            patcher.start(); self.addCleanup(patcher.stop)

    def tearDown(self):
        for base, dirs, _ in os.walk(self.root):   # unreadable folders left by a test
            for name in dirs: os.chmod(os.path.join(base, name), 0o755)
        self.tmp.cleanup()

    def leftovers(self, folder):
        return [p.name for p in Path(folder).rglob('.cati-*')]

    # ------------------------------------------------------------ mover y copiar
    def test_move_undo_never_overwrites_a_file_created_meanwhile(self):
        (self.root / 'dest').mkdir()
        (self.root / 'a.txt').write_text('a'); (self.root / 'b.txt').write_text('b')
        out = batch.move([self.root / 'a.txt', self.root / 'b.txt'], self.root / 'dest')
        (self.root / 'a.txt').write_text('trabajo nuevo')
        with self.assertRaisesRegex(RuntimeError, 'a.txt: ya existe'): out.undo()
        self.assertEqual((self.root / 'a.txt').read_text(), 'trabajo nuevo')
        self.assertEqual((self.root / 'dest/a.txt').read_text(), 'a')
        self.assertEqual((self.root / 'b.txt').read_text(), 'b')         # the rest is still undone

    def test_move_between_disks_and_back(self):
        with tempfile.TemporaryDirectory(prefix='cati-test-') as other:     # /tmp: another filesystem
            if os.stat(other).st_dev == os.stat(self.root).st_dev: self.skipTest('/tmp está en el mismo disco')
            folder = self.root / 'carpeta'; (folder / 'sub').mkdir(parents=True)
            (folder / 'sub/x.txt').write_text('x'); os.symlink('sub/x.txt', folder / 'enlace')
            out = batch.move([folder], other)
            self.assertEqual(out.errors, [])
            moved = Path(other) / 'carpeta'
            self.assertFalse(folder.exists())
            self.assertEqual((moved / 'sub/x.txt').read_text(), 'x')
            self.assertEqual(os.readlink(moved / 'enlace'), 'sub/x.txt')
            out.undo()
            self.assertEqual((folder / 'sub/x.txt').read_text(), 'x')
            self.assertFalse(moved.exists())
            self.assertEqual(self.leftovers(other) + self.leftovers(self.root), [])

    def test_failed_copy_between_disks_keeps_the_original_and_leaves_nothing(self):
        folder = self.root / 'origen'; folder.mkdir()
        (folder / 'bien.txt').write_text('ok')
        secret = folder / 'secreto.txt'; secret.write_text('s'); secret.chmod(0)
        if os.access(secret, os.R_OK): self.skipTest('ejecutando como root')
        (self.root / 'dest').mkdir()
        exdev = OSError(errno.EXDEV, 'cross-device')
        real = batch.rename_new

        def fake(source, target):
            if Path(target).parent == self.root / 'dest' and not Path(source).name.startswith('.cati-'): raise exdev
            return real(source, target)
        with mock.patch.object(batch, 'rename_new', fake):
            out = batch.move([folder], self.root / 'dest')
        secret.chmod(0o600)
        self.assertEqual(len(out.errors), 1)
        self.assertEqual(sorted(os.listdir(folder)), ['bien.txt', 'secreto.txt'])
        self.assertEqual(os.listdir(self.root / 'dest'), [])               # no half copy

    def test_copy_failure_removes_partial_copy(self):
        folder = self.root / 'origen'; folder.mkdir()
        (folder / 'bien.txt').write_text('ok')
        secret = folder / 'secreto.txt'; secret.write_text('s'); secret.chmod(0)
        if os.access(secret, os.R_OK): self.skipTest('ejecutando como root')
        (self.root / 'dest').mkdir()
        out = batch.copy([folder], self.root / 'dest')
        secret.chmod(0o600)
        self.assertEqual((len(out.done), len(out.errors)), (0, 1))
        self.assertEqual(os.listdir(self.root / 'dest'), [])

    def test_copy_through_a_link_into_itself_is_refused(self):
        folder = self.root / 'real'; (folder / 'dentro').mkdir(parents=True)
        os.symlink(folder, self.root / 'atajo')
        out = batch.copy([self.root / 'atajo'], folder / 'dentro')
        self.assertIn('dentro de sí misma', out.errors[0][1])

    def test_rename_new_refuses_to_replace(self):
        (self.root / 'a').write_text('a'); (self.root / 'b').write_text('b')
        with self.assertRaises(FileExistsError): batch.rename_new(self.root / 'a', self.root / 'b')
        self.assertEqual((self.root / 'b').read_text(), 'b')

    # ------------------------------------------------------------ renombrar
    def test_rename_undo_never_overwrites(self):
        (self.root / 'a.txt').write_text('A')
        out = batch.rename(batch.plan_rename([self.root / 'a.txt'], 'replace', 'a', 'b'))
        (self.root / 'a.txt').write_text('nuevo')
        with self.assertRaisesRegex(RuntimeError, 'a.txt: ya existe'): out.undo()
        self.assertEqual(((self.root / 'a.txt').read_text(), (self.root / 'b.txt').read_text()), ('nuevo', 'A'))

    def test_rename_swaps_and_cycles_and_undoes_them(self):
        for name in ('1', '2', '3'): (self.root / name).write_text(name)
        paths = [self.root / n for n in ('1', '2', '3')]
        plan = batch.plan_rename(paths, 'number', '{n}', start=2)            # 1→2, 2→3, 3→4
        self.assertTrue(all(p is None for *_, p in plan))
        out = batch.rename(plan)
        self.assertEqual(out.errors, [])
        self.assertEqual({n: (self.root / n).read_text() for n in ('2', '3', '4')}, {'2': '1', '3': '2', '4': '3'})
        out.undo()
        self.assertEqual(sorted(os.listdir(self.root)), ['1', '2', '3'])
        self.assertEqual([(self.root / n).read_text() for n in ('1', '2', '3')], ['1', '2', '3'])
        swap = batch.rename([(self.root / '1', '2', None), (self.root / '2', '1', None)])
        self.assertEqual(((self.root / '1').read_text(), (self.root / '2').read_text()), ('2', '1'))
        swap.undo()
        self.assertEqual(((self.root / '1').read_text(), (self.root / '2').read_text()), ('1', '2'))
        self.assertEqual(self.leftovers(self.root), [])

    def test_rename_chain_blocked_by_an_outside_file_is_refused_whole(self):
        for name in ('a', 'b', 'c'): (self.root / name).write_text(name)
        plan = [(self.root / 'a', 'b', None), (self.root / 'b', 'c', None)]
        out = batch.rename(plan)                                             # b→c fails, so a→b must not happen
        self.assertEqual(sorted(os.listdir(self.root)), ['a', 'b', 'c'])
        self.assertEqual([(self.root / n).read_text() for n in 'abc'], ['a', 'b', 'c'])
        self.assertEqual(len(out.errors), 2)
        self.assertEqual(self.leftovers(self.root), [])

    def test_plan_rename_allows_swaps_but_flags_blocked_chains(self):
        for name in ('a.txt', 'b.txt', 'c.txt'): (self.root / name).write_text(name)
        with mock.patch.object(batch, 'new_name', side_effect=['b.txt', 'a.txt']):
            swap = batch.plan_rename([self.root / 'a.txt', self.root / 'b.txt'], 'replace')
        self.assertEqual([p for *_, p in swap], [None, None])
        with mock.patch.object(batch, 'new_name', side_effect=['b.txt', 'c.txt']):
            chain = batch.plan_rename([self.root / 'a.txt', self.root / 'b.txt'], 'replace')
        self.assertEqual([p for *_, p in chain], ['ya existe', 'ya existe'])

    def test_same_new_name_in_different_folders_is_not_a_clash(self):
        for sub in ('x', 'y'):
            (self.root / sub).mkdir(); (self.root / sub / 'Foto.JPG').write_text(sub)
        plan = batch.plan_rename([self.root / 'x/Foto.JPG', self.root / 'y/Foto.JPG'], 'case', 'lower')
        self.assertEqual([p for *_, p in plan], [None, None])

    def test_rename_rejects_invalid_names_instead_of_crashing(self):
        (self.root / 'x').write_text('x')
        for name in ('.', '..', 'a/b', '', 'n' * 300):
            out = batch.rename([(self.root / 'x', name, None)])
            self.assertEqual(out.errors[0][1], 'nombre no válido', name)
        self.assertEqual(os.listdir(self.root), ['x'])

    def test_new_name_keeps_folder_names_whole_and_rejects_giant_formats(self):
        self.assertEqual(batch.new_name('mi.carpeta', 0, 'case', 'upper', is_dir=True), 'MI.CARPETA')
        self.assertEqual(batch.new_name('foto.jpg', 0, 'case', 'upper'), 'FOTO.jpg')
        self.assertEqual(batch.new_name('foto.jpg', 0, 'number', '{n:>999999999}'), 'foto.jpg')

    # ------------------------------------------------------------ comprimir
    def test_compress_is_robust_to_odd_contents(self):
        folder = self.root / 'carpeta'; folder.mkdir()
        old = folder / 'viejo.txt'; old.write_text('1970'); os.utime(old, (0, 0))       # before 1980
        os.mkfifo(folder / 'tuberia')                                                   # would block forever
        outside = self.root / 'fuera.txt'; outside.write_text('secreto de fuera')
        os.symlink(outside, folder / 'enlace')                                         # stored as a link
        locked = folder / 'cerrada'; locked.mkdir(); (locked / 'dentro.txt').write_text('x'); locked.chmod(0)
        (folder / 'raro\nnombre.txt').write_text('salto')
        try:
            out = within(10, lambda: batch.compress([folder]))
        finally: locked.chmod(0o755)
        self.assertEqual(Path(out.done[0]).name, 'carpeta.zip')
        with zipfile.ZipFile(out.done[0]) as archive:
            names = archive.namelist()
            self.assertEqual(archive.read('carpeta/viejo.txt'), b'1970')
            self.assertEqual(archive.read('carpeta/enlace'), str(outside).encode())
            self.assertTrue(stat.S_ISLNK(archive.getinfo('carpeta/enlace').external_attr >> 16))
            self.assertIn('carpeta/raro\nnombre.txt', names)
        self.assertNotIn('carpeta/tuberia', names)
        problems = {Path(p).name for p, _ in out.errors}
        self.assertIn('tuberia', problems)
        if not os.access(locked, os.R_OK) or os.geteuid() != 0: self.assertIn('cerrada', problems)   # not silently missing
        self.assertEqual(self.leftovers(self.root), [])

    def test_compress_never_overwrites_and_keeps_both_same_named_files(self):
        for sub in ('a', 'b'):
            (self.root / sub).mkdir(); (self.root / sub / 'notas.txt').write_text(sub)
        (self.root / 'mio.zip').write_text('valioso')
        out = batch.compress([self.root / 'a/notas.txt', self.root / 'b/notas.txt'], self.root / 'mio.zip')
        self.assertEqual((self.root / 'mio.zip').read_text(), 'valioso')
        with zipfile.ZipFile(out.done[0]) as archive:
            self.assertEqual({n: archive.read(n) for n in archive.namelist()}, {'notas.txt': b'a', 'notas (2).txt': b'b'})

    def test_compress_skips_its_own_archive_when_it_lands_inside(self):
        (self.root / 'x.txt').write_text('x')
        out = batch.compress([self.root], self.root / 'todo.zip')
        with zipfile.ZipFile(out.done[0]) as archive:
            self.assertFalse([n for n in archive.namelist() if 'cati-part' in n or n.endswith('todo.zip')])

    # ------------------------------------------------------------ papelera
    def write_trash(self, data, original, name, date):
        trash = data / 'Trash'
        (trash / 'files').mkdir(parents=True, exist_ok=True); (trash / 'info').mkdir(exist_ok=True)
        (trash / 'files' / name).write_text(f'{name} {date}')
        quoted = ''.join(c if c.isalnum() or c in '/-_.' else ''.join(f'%{b:02X}' for b in c.encode()) for c in str(original))
        (trash / 'info' / f'{name}.trashinfo').write_text(f'[Trash Info]\nPath={quoted}\nDeletionDate={date}\n')
        return trash

    def test_restore_reads_freedesktop_trash_latest_first_and_never_overwrites(self):
        data = self.root / 'share'
        original = self.root / 'doc con espacio ñ.txt'
        self.write_trash(data, original, 'doc.txt', '2024-01-01T10:00:00')
        trash = self.write_trash(data, original, 'doc.2.txt', '2025-06-01T10:00:00')
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}):
            original.write_text('otro')
            with self.assertRaisesRegex(RuntimeError, 'ya existe'): batch.restore(original)
            self.assertEqual(original.read_text(), 'otro')
            original.unlink()
            batch.restore(original)
        self.assertEqual(original.read_text(), 'doc.2.txt 2025-06-01T10:00:00')    # the most recent one
        self.assertEqual(sorted(os.listdir(trash / 'info')), ['doc.txt.trashinfo'])

    def test_trash_and_undo_end_to_end(self):
        """Gio de verdad, en un proceso aparte con XDG_DATA_HOME redirigido: nunca la papelera del usuario."""
        data = self.root / 'share'
        work = self.root / 'trabajo'; work.mkdir()
        script = '''
import os, sys
from pathlib import Path
from unittest import mock
from cati import batch
work = Path(sys.argv[1])
names = ['uno.txt', 'con espacio %.txt', 'salto\\nlinea.txt', 'carpeta']
for n in names[:3]: (work / n).write_text(n)
(work / 'carpeta').mkdir(); (work / 'carpeta/dentro.txt').write_text('d')
with mock.patch.object(batch.usage, 'forget'), mock.patch.object(batch, 'restore_virtual', side_effect=RuntimeError('gvfs')):
    out = batch.trash([work / n for n in names])
    assert not out.errors, out.errors
    assert not os.listdir(work)
    out.undo()
assert sorted(os.listdir(work)) == sorted(names), os.listdir(work)
assert (work / 'carpeta/dentro.txt').read_text() == 'd'
trash = Path(os.environ['XDG_DATA_HOME']) / 'Trash'
assert not os.listdir(trash / 'info') and not os.listdir(trash / 'files')
print('ok')
'''
        env = {**os.environ, 'XDG_DATA_HOME': str(data), 'PYTHONPATH': str(PROJECT)}
        result = subprocess.run([sys.executable, '-c', script, str(work)], capture_output=True, text=True, env=env, timeout=60)
        if 'Unable to find or create trash' in result.stderr: self.skipTest('Gio no puede crear la papelera aquí')
        self.assertEqual(result.stdout.strip(), 'ok', result.stderr[-2000:])

    # ------------------------------------------------------------ duplicados y espacio
    def catalog(self):
        db_path = self.root / 'index.sqlite3'
        db = connect(db_path); fill(db, self.root / 'data'); db.close()
        return db_path

    def test_duplicates_drop_files_that_change_while_compared(self):
        data = self.root / 'data'; data.mkdir()
        for name in ('a', 'b', 'c'): (data / name).write_bytes(b'z' * 300_000)
        db_path = self.catalog()
        real = disk.digest

        def changing(path, full=False, size=None):
            value = real(path, full, size)
            if full and Path(path).name == 'c':
                with open(path, 'r+b') as handle: handle.write(b'Q')     # edited right after being read
            return value
        with mock.patch.object(disk, 'digest', changing):
            groups = disk.duplicates(min_size=100, db_path=db_path)
        self.assertEqual([sorted(Path(p).name for p in g['paths']) for g in groups], [['a', 'b']])
        self.assertFalse(disk.still_identical([data / 'a', data / 'c']))
        self.assertTrue(disk.still_identical([data / 'a', data / 'b']))

    def test_duplicates_read_small_files_once_and_ignore_links(self):
        data = self.root / 'data'; data.mkdir()
        for name in ('a', 'b'): (data / name).write_bytes(b'k' * 9000)
        db_path = self.catalog()
        os.symlink(data / 'a', data / 'enlace')
        db = connect(db_path)
        db.execute('INSERT INTO catalog(path,name,folders,ext,is_dir,size,mtime) VALUES(?,?,?,?,?,?,?)',
                   (str(data / 'enlace'), 'enlace', '', '', 0, 9000, 0)); db.commit(); db.close()
        with mock.patch.object(disk, 'digest', wraps=disk.digest) as spy:
            groups = disk.duplicates(min_size=100, db_path=db_path)
        self.assertEqual([sorted(Path(p).name for p in g['paths']) for g in groups], [['a', 'b']])
        self.assertEqual(spy.call_count, 2)                                  # one read each, no second full pass

    def test_duplicates_survive_a_file_vanishing(self):
        data = self.root / 'data'; data.mkdir()
        for name in ('a', 'b', 'c'): (data / name).write_bytes(b'v' * 9000)
        db_path = self.catalog()
        real = disk.digest

        def vanish(path, full=False, size=None):
            value = real(path, full, size)
            if Path(path).name == 'c': os.unlink(path)
            return value
        with mock.patch.object(disk, 'digest', vanish):
            groups = disk.duplicates(min_size=100, db_path=db_path)
        self.assertEqual([sorted(Path(p).name for p in g['paths']) for g in groups], [['a', 'b']])

    def test_usage_on_disk_counts_hard_links_once_and_does_not_follow_links(self):
        folder = self.root / 'sin_indice'; (folder / 'sub').mkdir(parents=True)
        (folder / 'sub/grande.bin').write_bytes(b'g' * 50_000)
        os.link(folder / 'sub/grande.bin', folder / 'sub/otra.bin')
        (self.root / 'enorme.bin').write_bytes(b'e' * 200_000)
        os.symlink(self.root / 'enorme.bin', folder / 'enlace.bin')
        rows, total = disk.usage(folder, db_path=self.root / 'vacio.sqlite3')
        sizes = {Path(p).name: size for p, _, size, _ in rows}
        self.assertEqual(sizes['sub'], 50_000)
        self.assertLess(sizes['enlace.bin'], 1000)
        self.assertLess(total, 51_000)

    # ------------------------------------------------------------ vistas y propiedades
    def test_listing_shows_dangling_links_and_matches_disk(self):
        (self.root / 'a.txt').write_text('abc'); (self.root / 'carpeta').mkdir()
        os.symlink(self.root / 'no-existe', self.root / 'roto')
        hits = {h['name']: h for h in browse.listing(self.root)}
        self.assertIn('roto', hits)
        self.assertEqual((hits['a.txt']['size'], hits['carpeta']['kind']), (3, 'folder'))
        self.assertEqual(hits['a.txt']['mtime'], os.stat(self.root / 'a.txt').st_mtime_ns)

    def test_properties_of_a_named_pipe_do_not_block(self):
        os.mkfifo(self.root / 'datos.txt')
        sections = within(10, lambda: properties.collect(self.root / 'datos.txt', 'document'))
        self.assertEqual(sections[0][0], 'General')
        with self.assertRaises(OSError): properties.sha256(self.root / 'datos.txt')

    def test_short_path_only_shortens_inside_home(self):
        home = str(Path.home())
        self.assertEqual(widgets.short_path(home + '/Documentos'), '~/Documentos')
        self.assertEqual(widgets.short_path(home), '~')
        self.assertEqual(widgets.short_path(home + '2/x'), home + '2/x')

    def test_saved_searches_are_private_and_survive_a_corrupt_file(self):
        target = self.root / 'saved.json'
        with mock.patch.object(saved, 'PATH', target), mock.patch.object(saved, 'DATA', self.root):
            target.write_text('{"no": "lista"}')
            self.assertEqual(saved.load(), [])
            saved.add('x', 'x')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith('.tmp')], [])

    # ------------------------------------------------------------ voz
    def test_voice_server_that_never_answers_is_not_left_running(self):
        tool = self.root / 'whisper-server'
        tool.write_text('#!/bin/sh\nexec sleep 60\n'); tool.chmod(0o755)
        with mock.patch.object(voice, 'DATA', self.root), mock.patch.object(voice, 'whisper', return_value=(tool, tool)):
            server = voice.Server(startup=.5)
            server.port = 1                                                  # nothing listens there
            self.assertFalse(server.ensure())
            self.assertIsNone(server.process)
            self.assertFalse((self.root / 'voice-server.pid').exists())

    def test_leftover_voice_server_from_a_crashed_session_is_stopped(self):
        tool = self.root / 'whisper-server'
        tool.write_text('#!/bin/sh\nexec sleep 60\n'); tool.chmod(0o755)
        orphan = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '--port', '18770'])
        try:
            with mock.patch.object(voice, 'DATA', self.root):
                server = voice.Server()
                server.port = 18770
                server.pidfile.write_text(str(orphan.pid))
                server.leftover()                                            # not a whisper-server: untouched
                self.assertIsNone(orphan.poll())
                with mock.patch.object(Path, 'read_bytes', return_value=b'/x/whisper-server\0--port\x0018770\0'):
                    server.pidfile.write_text(str(orphan.pid))
                    server.leftover()
            self.assertEqual(orphan.wait(5), -15)
        finally:
            if orphan.poll() is None: orphan.kill(); orphan.wait()

    def test_finish_reaps_a_stubborn_process(self):
        process = subprocess.Popen(['sh', '-c', 'trap "" TERM; sleep 30'], stdout=subprocess.PIPE)
        time.sleep(.2)
        voice.finish(process, grace=.3)
        self.assertIsNotNone(process.returncode)


if __name__ == '__main__':
    unittest.main()
