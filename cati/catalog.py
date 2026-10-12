"""Catálogo de nombres: cada archivo y carpeta, aunque su contenido no se analice."""
import ctypes
import os
import time
from pathlib import Path

from .readers import AUDIO, CODE, IMAGES, SKIP_DIRS, TEXT, VIDEO, allowed

SHEETS = {".xlsx", ".xls", ".ods", ".csv", ".tsv", ".numbers"}
SLIDES = {".pptx", ".ppt", ".odp", ".key"}
OFFICE = {".doc", ".rtf", ".epub", ".pages", ".tex", ".json", ".xml", ".yaml", ".yml", ".toml", ".log", ".ini", ".cfg"}
ARCHIVES = {".zip", ".tar", ".gz", ".tgz", ".xz", ".bz2", ".zst", ".7z", ".rar", ".iso", ".deb", ".rpm", ".appimage"}
EXTRA_IMAGES = {".svg", ".heic", ".heif", ".avif", ".ico", ".psd", ".xcf", ".raw", ".cr2", ".nef", ".dng", ".kra"}
EXTRA_CODE = {".css", ".scss", ".html", ".htm", ".ipynb", ".cs", ".dart", ".zig", ".hs", ".ex", ".exs", ".r", ".jl", ".m"}

CATEGORIES = ["folder", "document", "pdf", "image", "video", "audio", "sheet", "slides", "code", "archive", "other"]


def category(ext, is_dir=False):
    """Categoría visible de un elemento a partir de su extensión."""
    if is_dir: return "folder"
    if ext == ".pdf": return "pdf"
    # Spreadsheets and slides are also read as text: their own category comes first.
    if ext in SHEETS: return "sheet"
    if ext in SLIDES: return "slides"
    if ext in TEXT or ext in OFFICE: return "document"
    if ext in IMAGES or ext in EXTRA_IMAGES: return "image"
    if ext in VIDEO: return "video"
    if ext in AUDIO: return "audio"
    if ext in CODE or ext in EXTRA_CODE: return "code"
    if ext in ARCHIVES: return "archive"
    return "other"


class _Time(ctypes.Structure):
    _fields_ = [("sec", ctypes.c_int64), ("nsec", ctypes.c_uint32), ("_pad", ctypes.c_int32)]


class _Statx(ctypes.Structure):
    _fields_ = [("mask", ctypes.c_uint32), ("blksize", ctypes.c_uint32), ("attributes", ctypes.c_uint64),
                ("nlink", ctypes.c_uint32), ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32), ("mode", ctypes.c_uint16),
                ("_pad", ctypes.c_uint16), ("ino", ctypes.c_uint64), ("size", ctypes.c_uint64), ("blocks", ctypes.c_uint64),
                ("attributes_mask", ctypes.c_uint64), ("atime", _Time), ("btime", _Time), ("ctime", _Time), ("mtime", _Time),
                ("_rest", ctypes.c_uint8 * 144)]


try:
    _statx = ctypes.CDLL(None, use_errno=True).statx
    _statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_Statx)]
except (AttributeError, OSError):
    _statx = None
AT_FDCWD, AT_SYMLINK_NOFOLLOW, AT_STATX_DONT_SYNC, STATX_BTIME = -100, 0x100, 0x4000, 0x800


def birth(path, fallback=0):
    """Fecha de creación en ns (statx); `fallback` si el sistema de archivos no la guarda."""
    if _statx is None: return fallback
    buffer = _Statx()
    if _statx(AT_FDCWD, os.fsencode(path), AT_SYMLINK_NOFOLLOW | AT_STATX_DONT_SYNC, STATX_BTIME, ctypes.byref(buffer)) != 0:
        return fallback
    if not buffer.mask & STATX_BTIME or buffer.btime.sec <= 0: return fallback
    return buffer.btime.sec * 1_000_000_000 + buffer.btime.nsec


def describe(path, root, is_dir):
    """Campos buscables: nombre sin extensión separado en palabras y carpetas cercanas."""
    try: relative = path.relative_to(root).parent.parts
    except ValueError: relative = path.parent.parts
    ext = "" if is_dir else path.suffix.lower()
    name = path.name if is_dir else (path.name[: -len(path.suffix)] if path.suffix else path.name)
    name = name.replace("_", " ").replace("-", " ").replace(".", " ")
    return name + (" " + ext[1:] if ext else ""), " ".join(relative[-4:]), ext


def visible(name, is_dir):
    if name.startswith("."): return False
    if is_dir: return name not in SKIP_DIRS and not name.endswith((".noindex", ".app"))
    return allowed(Path(name))


def walk_all(roots, excluded=(), stop=None, on_directory=None):
    """Recorre carpetas y archivos con scandir; omite ocultos, dependencias y credenciales."""
    excluded = {Path(p) for p in excluded}
    for root in roots:
        root = Path(root)
        stack = [root]
        while stack:
            if stop and stop.is_set(): return
            folder = stack.pop()
            if folder in excluded: continue
            if on_directory: on_directory(folder)
            try: entries = list(os.scandir(folder))
            except OSError: continue
            for entry in entries:
                try:
                    if entry.is_symlink(): continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if not visible(entry.name, is_dir): continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError: continue
                path = Path(entry.path)
                if is_dir:
                    if path in excluded: continue
                    stack.append(path)
                yield root, path, is_dir, 0 if is_dir else stat.st_size, stat.st_mtime_ns


def row(root, path, is_dir, size, mtime, btime=None):
    name, folders, ext = describe(path, root, is_dir)
    return str(path), name, folders, ext, int(is_dir), size, mtime, birth(path, mtime) if btime is None else btime


def sync(db, roots, excluded=(), stop=None, on_directory=None, files=None):
    """Aplica solo las diferencias con el catálogo guardado; devuelve el número de elementos.
    Con `files`, acumula (ruta, tamaño, mtime) de cada archivo para no recorrer el disco dos veces."""
    # Rows saved before creation dates existed (btime NULL) count as changed and get it filled in.
    known = {path: (mtime, size) if btime is not None else None
             for path, mtime, size, btime in db.execute("SELECT path,mtime,size,btime FROM catalog")}
    seen = set()
    changed = []
    for root, path, is_dir, size, mtime in walk_all(roots, excluded, stop, on_directory):
        key = str(path)
        seen.add(key)
        if files is not None and not is_dir: files.append((path, size, mtime))
        if known.get(key) != (mtime, size): changed.append(row(root, path, is_dir, size, mtime))
        if len(changed) >= 5000:
            upsert(db, changed); changed = []
    if stop and stop.is_set(): return len(seen)
    upsert(db, changed)
    gone = [(p,) for p in known.keys() - seen]
    with db:
        db.executemany("DELETE FROM catalog WHERE path=?", gone)
    return len(seen)


def upsert(db, rows):
    if not rows: return
    with db:
        db.executemany("""INSERT INTO catalog(path,name,folders,ext,is_dir,size,mtime,btime) VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(path) DO UPDATE SET name=excluded.name,folders=excluded.folders,ext=excluded.ext,
          is_dir=excluded.is_dir,size=excluded.size,mtime=excluded.mtime,btime=excluded.btime""", rows)


def touch(db, roots, paths):
    """Actualiza rutas concretas avisadas por inotify; las que ya no existen se borran."""
    rows, gone = [], []
    for path in paths:
        path = Path(path)
        root = next((Path(r) for r in roots if path == Path(r) or Path(r) in path.parents), None)
        try:
            stat = path.lstat()
            is_dir = path.is_dir() and not path.is_symlink()
            if root is None or path.is_symlink() or not visible(path.name, is_dir): raise FileNotFoundError
            rows.append(row(root, path, is_dir, 0 if is_dir else stat.st_size, stat.st_mtime_ns))
        except OSError:
            gone.append(str(path))
    upsert(db, rows)
    if gone:
        with db:
            for path in gone:
                # A removed folder takes its whole subtree with it.
                db.execute("DELETE FROM catalog WHERE path=? OR (path>=? AND path<?)", (path, path + "/", path + "0"))


def recent(seconds):
    return int((time.time() - seconds) * 1e9)
