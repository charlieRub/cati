"""Catálogo de nombres: cada archivo y carpeta, aunque su contenido no se analice."""
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
    if ext in TEXT or ext in OFFICE: return "document"
    if ext in IMAGES or ext in EXTRA_IMAGES: return "image"
    if ext in VIDEO: return "video"
    if ext in AUDIO: return "audio"
    if ext in SHEETS: return "sheet"
    if ext in SLIDES: return "slides"
    if ext in CODE or ext in EXTRA_CODE: return "code"
    if ext in ARCHIVES: return "archive"
    return "other"


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


def row(root, path, is_dir, size, mtime):
    name, folders, ext = describe(path, root, is_dir)
    return str(path), name, folders, ext, int(is_dir), size, mtime


def sync(db, roots, excluded=(), stop=None, on_directory=None):
    """Aplica solo las diferencias con el catálogo guardado; devuelve el número de elementos."""
    known = {path: (mtime, size) for path, mtime, size in db.execute("SELECT path,mtime,size FROM catalog")}
    seen = set()
    changed = []
    for root, path, is_dir, size, mtime in walk_all(roots, excluded, stop, on_directory):
        key = str(path)
        seen.add(key)
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
        db.executemany("""INSERT INTO catalog(path,name,folders,ext,is_dir,size,mtime) VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(path) DO UPDATE SET name=excluded.name,folders=excluded.folders,ext=excluded.ext,
          is_dir=excluded.is_dir,size=excluded.size,mtime=excluded.mtime""", rows)


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
