"""Acciones sobre varios archivos a la vez; todas devuelven un Outcome con su forma de deshacerse.

Ninguna reemplaza nunca algo que ya existe: los cambios de nombre usan renameat2(RENAME_NOREPLACE) y las copias
se escriben con un nombre provisional oculto que solo toma el definitivo cuando están completas."""
import ctypes
import errno
import os
import re
import shutil
import stat
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import usage


@dataclass
class Outcome:
    done: list = field(default_factory=list)      # rutas resultantes
    errors: list = field(default_factory=list)    # (ruta, motivo)
    undo: object = None                           # callable o None
    message: str = ''


def unique(path):
    """«nombre (2).ext» si ya existe «nombre.ext»."""
    path = Path(path)
    if not os.path.lexists(path): return path
    stem, suffix = (path.name, '') if path.is_dir() else (path.stem, path.suffix)
    number = 2
    while os.path.lexists(path.with_name(f'{stem} ({number}){suffix}')): number += 1
    return path.with_name(f'{stem} ({number}){suffix}')


def plural(count, one, many): return f'{count} {one if count == 1 else many}'


# ---------------------------------------------------------------- piezas seguras
_renameat2 = None


def rename_new(source, target):
    """os.rename que falla con FileExistsError en lugar de reemplazar el destino, aunque aparezca en el último instante."""
    global _renameat2
    if _renameat2 is None:
        try:
            _renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
            _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        except (OSError, AttributeError): _renameat2 = False
    if _renameat2:
        if _renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0: return   # AT_FDCWD, RENAME_NOREPLACE
        code = ctypes.get_errno()
        if code not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise OSError(code, os.strerror(code), str(source), None, str(target))
    # Disk without RENAME_NOREPLACE (some FUSE/network mounts): best effort.
    if os.path.lexists(target): raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(source), None, str(target))
    os.rename(source, target)


def remove(path):
    path = Path(path)
    if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
    elif os.path.lexists(path): path.unlink()


def scratch(target, kind='part'):
    """Nombre oculto junto al destino (el indexador ignora los ocultos) para trabajar sin tocar el definitivo."""
    return Path(target).with_name(f'.cati-{kind}-{uuid.uuid4().hex[:12]}')


def duplicate(source, target, follow=True):
    """Copia `source` como `target` sin reemplazar nada; si falla a medias no deja restos.

    Con follow=False un enlace simbólico se copia como enlace (al mover), no como lo que apunta."""
    partial = scratch(target)
    try:
        if source.is_symlink() and not follow: os.symlink(os.readlink(source), partial)
        elif source.is_dir(): shutil.copytree(source, partial, symlinks=True)
        else: shutil.copy2(source, partial)
        rename_new(partial, target)
    except BaseException:
        try: remove(partial)
        except OSError: pass
        raise


def relocate(source, target):
    """Mueve sin reemplazar; entre discos distintos copia y borra el original solo cuando la copia está completa."""
    try: return rename_new(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV: raise
    duplicate(source, target, follow=False)
    try: remove(source)
    except OSError as exc: raise OSError(f'copiado, pero el original no se pudo borrar del todo: {exc}') from exc


def inside(folder, path):
    """¿`folder` es `path` o está dentro (también a través de enlaces)?"""
    folder, path = Path(os.path.realpath(folder)), Path(os.path.realpath(path))
    return folder == path or path in folder.parents


def reason(exc):
    if isinstance(exc, FileExistsError): return 'ya existe'
    return exc.strerror if isinstance(exc, OSError) and exc.strerror and exc.errno else str(exc)


def undoing(steps, action):
    """Ejecuta `action` sobre cada paso aunque alguno falle, y avisa de todos los que fallaron."""
    failed = []
    for step in steps:
        try: action(*step)
        except Exception as exc: failed.append(f'{Path(step[0]).name}: {reason(exc)}')
    if failed: raise RuntimeError('; '.join(failed))


# ---------------------------------------------------------------- mover, copiar
def move(paths, folder):
    folder = Path(folder)
    out = Outcome()
    moves = []
    for path in map(Path, paths):
        target = folder / path.name
        try:
            if path.parent == folder: continue
            if path.is_dir() and not path.is_symlink() and inside(folder, path): raise OSError('no se puede mover una carpeta dentro de sí misma')
            if os.path.lexists(target): raise FileExistsError
            relocate(path, target)
        except FileExistsError: out.errors.append((str(path), 'ya existe allí')); continue
        except OSError as exc: out.errors.append((str(path), str(exc))); continue
        usage.moved(path, target)
        moves.append((path, target)); out.done.append(str(target))

    def back(old, new):
        relocate(new, old)    # never onto something created meanwhile at the old place
        usage.moved(new, old)
    out.undo = (lambda: undoing(reversed(moves), back)) if moves else None
    out.message = f'{plural(len(moves), "elemento movido", "elementos movidos")} a {folder.name or folder}'
    return out


def copy(paths, folder):
    folder = Path(folder)
    out = Outcome()
    made = []
    for path in map(Path, paths):
        try:
            if path.is_dir() and inside(folder, path): raise OSError('no se puede copiar una carpeta dentro de sí misma')
            target = unique(folder / path.name)
            duplicate(path, target)
        except FileExistsError: out.errors.append((str(path), 'ya existe allí')); continue
        except OSError as exc: out.errors.append((str(path), str(exc))); continue
        made.append(target); out.done.append(str(target))
    out.undo = (lambda: undoing(((t,) for t in reversed(made)), remove)) if made else None
    out.message = f'{plural(len(made), "elemento copiado", "elementos copiados")} a {folder.name or folder}'
    return out


# ---------------------------------------------------------------- papelera
def mount_point(path):
    path = Path(os.path.abspath(path))
    while path != path.parent and not os.path.ismount(path): path = path.parent
    return path


def trash_dirs(original):
    """Papeleras (especificación freedesktop) donde pudo ir `original`, con la carpeta a la que se refieren
    sus rutas relativas: la personal y las de la raíz de su disco."""
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    top = mount_point(Path(original).parent)
    uid = os.getuid()
    return [(data / 'Trash', data), (top / '.Trash' / str(uid), top), (top / f'.Trash-{uid}', top)]


def trashed(originals):
    """{ruta original: (fecha, elemento en files/, ficha en info/)} con lo más reciente enviado desde cada ruta.

    Lee las fichas .trashinfo directamente (Path= con codificación %, DeletionDate=), una sola vez por papelera."""
    wanted = {os.path.normpath(os.path.abspath(p)) for p in originals}
    found, seen = {}, set()
    for original in wanted:
        for trash, top in trash_dirs(original):
            if trash in seen: continue
            seen.add(trash)
            try: entries = list(os.scandir(trash / 'info'))
            except OSError: continue
            for entry in entries:
                if not entry.name.endswith('.trashinfo'): continue
                try:
                    text = Path(entry.path).read_bytes().decode('utf-8', 'replace')
                    written = entry.stat().st_mtime_ns
                except OSError: continue
                fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
                where = fields.get('Path', '').strip()
                if not where: continue
                where = os.fsdecode(urllib.parse.unquote_to_bytes(where))
                where = os.path.normpath(where if where.startswith('/') else os.path.join(top, where))
                if where not in wanted: continue
                item = ((fields.get('DeletionDate', '').strip(), written), trash / 'files' / entry.name[:-len('.trashinfo')], Path(entry.path))
                if where not in found or item[0] > found[where][0]: found[where] = item
    return found


def restore(original, found=None):
    """Devuelve a su sitio el último elemento enviado a la papelera desde esa ruta, en cualquier disco."""
    original = os.path.normpath(os.path.abspath(original))
    if os.path.lexists(original): raise RuntimeError('ya existe otro archivo con ese nombre')
    item = (trashed([original]) if found is None else found).get(original)
    if item is None: return restore_virtual(original)
    _, stored, info = item
    if not os.path.lexists(stored): raise RuntimeError('ya no está en la papelera')
    Path(original).parent.mkdir(parents=True, exist_ok=True)
    try: relocate(stored, Path(original))
    except FileExistsError: raise RuntimeError('ya existe otro archivo con ese nombre') from None
    info.unlink(missing_ok=True)


def restore_virtual(original):
    """Último recurso: la vista trash:/// de GVfs (papeleras que no están donde dice la especificación)."""
    from gi.repository import Gio
    trash = Gio.File.new_for_uri('trash:///')
    found = []
    for attempt in range(3):   # the virtual trash view updates a moment after the file is trashed
        listing = trash.enumerate_children('standard::name,trash::orig-path,trash::deletion-date', Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        for info in listing:
            if info.get_attribute_byte_string('trash::orig-path') == original:
                found.append((info.get_attribute_string('trash::deletion-date') or '', info.get_name()))
        if found: break
        time.sleep(.15)
    if not found: raise RuntimeError('ya no está en la papelera')
    trash.get_child(max(found)[1]).move(Gio.File.new_for_path(original), Gio.FileCopyFlags.NONE, None, None, None)


def trash(paths):
    from gi.repository import Gio, GLib
    out = Outcome()
    sent = []
    for path in map(Path, paths):
        try: Gio.File.new_for_path(str(path)).trash(None)
        except GLib.Error as exc: out.errors.append((str(path), exc.message)); continue
        usage.forget(path)
        sent.append(path); out.done.append(str(path))

    def undo():
        found = trashed(sent)   # one pass over the trash for all of them

        def back(path):
            try: restore(path, found)
            except GLib.Error as exc: raise RuntimeError(exc.message) from None
        undoing(((p,) for p in reversed(sent)), back)
    out.undo = undo if sent else None
    out.message = f'{plural(len(sent), "elemento enviado", "elementos enviados")} a la papelera'
    return out


# ---------------------------------------------------------------- comprimir
def entry_name(name):
    """Los nombres que no son UTF-8 válido no caben en un ZIP: se guardan con � en vez de fallar."""
    return name.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')


def zip_time(seconds):
    moment = time.localtime(seconds)[:6]
    return max((1980, 1, 1, 0, 0, 0), min(moment, (2107, 12, 31, 23, 59, 58)))


def compress(paths, target=None):
    """ZIP con los elementos (carpetas incluidas) guardados con su nombre.

    Dentro de las carpetas los enlaces simbólicos se guardan como enlaces (no se cuela lo que hay fuera), las
    tuberías y dispositivos se omiten con aviso, y dos elementos con el mismo nombre no se pisan dentro del ZIP."""
    paths = [Path(p) for p in paths]
    if not paths: return Outcome(message='Nada que comprimir')
    if target is None:
        base = paths[0].stem if len(paths) == 1 and not paths[0].is_dir() else paths[0].name if len(paths) == 1 else 'Archivo'
        target = unique(paths[0].parent / f'{base}.zip')
    target = Path(target)
    out = Outcome()
    partial = scratch(target)
    taken = set()

    def free(name):
        stem, suffix = os.path.splitext(name) if '.' in name[1:] else (name, '')
        number = 2
        while name in taken: name, number = f'{stem} ({number}){suffix}', number + 1
        taken.add(name)
        return name

    try:
        with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as archive:
            own = os.fstat(archive.fp.fileno())

            def add(full, name, follow=False):
                try:
                    info = os.stat(full) if follow else os.lstat(full)
                    if (info.st_dev, info.st_ino) == (own.st_dev, own.st_ino): return
                    if stat.S_ISLNK(info.st_mode):
                        entry = zipfile.ZipInfo(name, zip_time(info.st_mtime))
                        entry.external_attr = (info.st_mode & 0xFFFF) << 16
                        archive.writestr(entry, os.readlink(os.fsencode(full)))
                    elif stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode): archive.write(full, name)
                    else: raise OSError('no es un archivo normal (tubería, dispositivo o socket)')
                except (OSError, ValueError) as exc: out.errors.append((str(full), str(exc)))

            for path in paths:
                top = free(entry_name(path.name))
                if path.is_dir() and not path.is_symlink():
                    add(path, top)
                    for base, dirs, names in os.walk(path, onerror=lambda exc: out.errors.append((exc.filename or '', reason(exc)))):
                        for name in dirs + names:
                            full = Path(base) / name
                            add(full, entry_name(str(Path(top) / full.relative_to(path))))
                else: add(path, top, follow=path.is_file())   # a selected link to a file: its content
        try: rename_new(partial, target)
        except FileExistsError:
            target = unique(target); rename_new(partial, target)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        partial.unlink(missing_ok=True)
        out.errors.append((str(target), str(exc))); out.message = 'No se pudo comprimir'
        return out
    out.done = [str(target)]
    out.undo = lambda: target.unlink()
    out.message = f'{target.name} creado ({plural(len(paths), "elemento", "elementos")})'
    return out


# ---------------------------------------------------------------- renombrar en lote
MODES = {'replace': 'Buscar y reemplazar', 'number': 'Numerar', 'case': 'Mayúsculas / minúsculas', 'affix': 'Añadir prefijo o sufijo'}
HUGE_FORMAT = re.compile(r'\{[^{}]*:[^{}]*\d{4}')   # «{n:>99999999}» would build a gigabyte string


def bad_name(name):
    return not name or name in {'.', '..'} or '/' in name or '\0' in name or len(os.fsencode(name)) > 255


def new_name(name, index, mode, a='', b='', *, start=1, is_dir=False):
    """Nuevo nombre para `name` (la extensión se conserva salvo al reemplazar texto; las carpetas no tienen)."""
    path = Path(name)
    stem, suffix = (path.name, '') if is_dir or not path.suffix else (path.stem, path.suffix)
    if mode == 'replace': return name.replace(a, b) if a else name
    if mode == 'number':
        if HUGE_FORMAT.search(a or ''): return name
        try: return (a or '{n}').format(n=index + start, name=stem) + suffix
        except (KeyError, IndexError, ValueError, AttributeError, TypeError): return name
    if mode == 'case':
        return {'lower': stem.lower(), 'upper': stem.upper(), 'title': stem.title(), 'capital': stem.capitalize()}.get(a, stem) + suffix
    if mode == 'affix': return f'{a}{stem}{b}{suffix}'
    return name


def same_entry(source, target):
    """`target` es `source` escrito con otras mayúsculas en un disco que no las distingue (FAT, exFAT…)."""
    try:
        return (source != target and os.path.lexists(target) and os.path.samefile(source, target)
                and target.name not in os.listdir(target.parent))
    except OSError: return False


def plan_rename(paths, mode, a='', b='', start=1):
    """[(ruta, nombre_nuevo, problema|None)]: detecta vacíos, «/», choques entre sí y con archivos existentes.

    Un destino ocupado por otro elemento de la lista que también cambia de nombre no es un choque (a→b, b→a)."""
    paths = [Path(p) for p in paths]
    plan = [(p, new_name(p.name, i, mode, a, b, start=start, is_dir=p.is_dir())) for i, p in enumerate(paths)]
    taking = {}
    for path, name in plan: taking.setdefault((str(path.parent), name), []).append(path)
    sources = {str(path): i for i, (path, _) in enumerate(plan)}
    problems, waits = [], {}
    for i, (path, name) in enumerate(plan):
        problem = None
        if name == path.name: problem = 'sin cambios'
        elif bad_name(name): problem = 'nombre no válido'
        elif len(taking[(str(path.parent), name)]) > 1: problem = 'repetido en la lista'
        else:
            target = path.with_name(name)
            if os.path.lexists(target) and not same_entry(path, target):
                if str(target) in sources: waits[i] = sources[str(target)]   # fine only if that one moves away
                else: problem = 'ya existe'
        problems.append(problem)
    changed = True
    while changed:
        changed = False
        for i, j in waits.items():
            if problems[i] is None and problems[j] is not None: problems[i] = 'ya existe'; changed = True
    return [(path, name, problem) for (path, name), problem in zip(plan, problems)]


def shuffle(steps):
    """Aplica [(origen, destino)] sin reemplazar nunca nada: ([hechos], [(ruta, motivo)]).

    Si un destino es el origen de otro paso (a→b, b→a) o el mismo archivo con otras mayúsculas en un disco que
    no las distingue, ese paso pasa antes por un nombre provisional; si al final no puede, vuelve a su nombre."""
    sources = {str(source) for source, _ in steps}
    direct, parked, done, failed = [], [], [], []
    for source, target in steps:
        if str(target) not in sources and not same_entry(source, target): direct.append((source, target)); continue
        temporary = scratch(source, 'rename')
        try: rename_new(source, temporary)
        except OSError as exc: failed.append((str(source), reason(exc))); continue
        parked.append((source, temporary, target))
    for source, target in direct:
        try: rename_new(source, target)
        except OSError as exc: failed.append((str(source), reason(exc))); continue
        done.append((source, target))
    for source, temporary, target in parked:
        try: rename_new(temporary, target)
        except OSError as exc:
            try:
                rename_new(temporary, source)
                failed.append((str(source), reason(exc)))
            except OSError: failed.append((str(source), f'{reason(exc)}; quedó como {temporary.name}'))
            continue
        done.append((source, target))
    return done, failed


def rename(plan):
    out = Outcome()
    steps = []
    for path, name, problem in plan:
        path = Path(path)
        if problem == 'sin cambios' or name == path.name: continue
        if problem: out.errors.append((str(path), problem)); continue
        if bad_name(name): out.errors.append((str(path), 'nombre no válido')); continue
        steps.append((path, path.with_name(name)))
    done, failed = shuffle(steps)
    out.errors += failed
    for old, new in done:
        usage.moved(old, new); out.done.append(str(new))

    def undo():
        back, failed = shuffle([(new, old) for old, new in reversed(done)])
        for new, old in back: usage.moved(new, old)
        original = {str(new): old for old, new in done}
        if failed: raise RuntimeError('; '.join(f'{original[path].name}: {why}' for path, why in failed))
    out.undo = undo if done else None
    out.message = f'{plural(len(done), "elemento renombrado", "elementos renombrados")}'
    return out
