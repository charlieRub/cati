"""Vistas que no son una búsqueda (carpetas, recientes, más usados) con el mismo formato de resultado."""
import os
import stat
import time
from pathlib import Path

from . import usage
from .catalog import category
from .query import STOP, normalize, places as query_places
from .search import kind_of

SIZES = {'small': (None, 1 << 20), 'big': (10 << 20, None), 'huge': (500 << 20, None)}
SIZE_LABEL = {'small': 'Pequeños (< 1 MB)', 'big': 'Grandes (≥ 10 MB)', 'huge': 'Enormes (≥ 500 MB)'}
PERIOD_LABEL = {'today': 'Hoy', 'week': 'Últimos 7 días', 'month': 'Últimos 30 días', 'year': 'Este año'}


def make_hit(path, reasons, *, kind=None, size=None, mtime=None, excerpt='', score=0, copies=(), indexed=False, extra=None, is_dir=None):
    """Un resultado con todos los campos que la interfaz espera, a partir de lo que dice el disco.

    Si quien llama ya sabe `is_dir`, `size` y `mtime` (un listado con scandir), no se vuelve a consultar el disco."""
    path = str(path)
    info = None
    if is_dir is None or size is None or mtime is None:
        try: info = os.stat(path)
        except OSError: pass
        if is_dir is None: is_dir = bool(info and stat.S_ISDIR(info.st_mode))
    hit = {'path': path, 'name': os.path.basename(path.rstrip('/')) or path, 'kind': kind or kind_of(path, None, is_dir),
           'indexed': indexed, 'segment': None, 'loc': None, 'end': None, 'excerpt': excerpt, 'score': score, 'cosine': None,
           'keyword': False, 'coverage': 0, 'meaning': False, 'name_match': 0, 'reasons': list(reasons),
           'mtime': mtime if mtime is not None else (info.st_mtime_ns if info else 0),
           'size': size if size is not None else (0 if is_dir or not info else info.st_size), 'copies': list(copies), 'inferred_kind': None}
    if extra: hit.update(extra)
    return hit


SORTS = {'name': 'Nombre', 'date': 'Fecha', 'size': 'Tamaño', 'kind': 'Tipo'}


def listing(folder, sort='name', hidden=False):
    """Contenido de una carpeta: carpetas primero, luego archivos. Un enlace roto también aparece (para poder borrarlo)."""
    folder = Path(folder)
    base = str(folder).rstrip('/') + '/'
    rows = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.startswith('.') and not hidden: continue
            try:
                is_dir = entry.is_dir()
                try: info = entry.stat()
                except FileNotFoundError: info = entry.stat(follow_symlinks=False)   # dangling link
            except OSError: continue
            rows.append((entry.name, is_dir, info))
    key = {'name': lambda r: r[0].casefold(), 'date': lambda r: -r[2].st_mtime_ns,
           'size': lambda r: -r[2].st_size, 'kind': lambda r: (category(Path(r[0]).suffix.lower(), r[1]), r[0].casefold())}[sort]
    rows.sort(key=lambda r: (not r[1], key(r)))
    return [make_hit(base + name, [], size=0 if is_dir else info.st_size, mtime=info.st_mtime_ns, is_dir=is_dir,
                     kind='folder' if is_dir else None, extra={'children': None}) for name, is_dir, info in rows]


def count_children(path, hidden=False):
    try:
        with os.scandir(path) as entries: return sum(1 for e in entries if hidden or not e.name.startswith('.'))
    except OSError: return None


def places(config=None):
    """Sitios fijos de la barra lateral: carpeta personal, carpetas de usuario y los discos montados."""
    home = Path.home()
    out = []
    seen = set()

    def add(label, path, icon='folder'):
        path = Path(path)
        if path.is_dir() and path not in seen:
            seen.add(path)
            out.append({'label': label, 'path': str(path), 'icon': icon})
    add('Carpeta personal', home, 'home')
    for label, word, fallback in (('Escritorio', 'escritorio', 'Desktop'), ('Documentos', 'documentos', 'Documents'), ('Descargas', 'descargas', 'Downloads'),
                                  ('Imágenes', 'imagenes', 'Pictures'), ('Vídeos', 'videos', 'Videos'), ('Música', 'musica', 'Music')):
        add(label, query_places().get(word) or home / fallback)
    for root in (config or {}).get('folders', []):
        if Path(root) != home: add(Path(root).name or str(root), root)
    for base in (Path('/run/media') / home.name, Path('/media') / home.name, Path('/mnt')):
        if base.is_dir():
            for mount in sorted(base.iterdir()):
                if mount.is_dir() and os.access(mount, os.R_OK): add(mount.name, mount, 'drive')
    return out


def crumbs(path, limit_home=True):
    """Migas de pan: [(etiqueta, ruta)] desde la carpeta personal (o la raíz) hasta `path`."""
    path = Path(path)
    home = Path.home()
    anchor = home if home == path or home in path.parents else Path('/')
    parts = [('~' if anchor == home else '/', anchor)]
    for part in path.relative_to(anchor).parts: parts.append((part, parts[-1][1] / part))
    return [(label, str(p)) for label, p in parts]


def used_hits(order, limit=60, db_path=None):
    """Abiertos hace poco o con más frecuencia, como resultados normales."""
    hits = []
    for path, opens, last in usage.rows(order, limit, db_path):
        reason = f'{opens} {"vez" if opens == 1 else "veces"}' if order == 'frequent' else 'abierto ' + ago(last / 1e9)
        hits.append(make_hit(path, [reason]))
    return hits


def ago(stamp, now=None):
    seconds = max(0, (now or time.time()) - stamp)
    for limit, unit, name in ((60, 1, 'ahora'), (3600, 60, 'min'), (86400, 3600, 'h'), (86400 * 30, 86400, 'd')):
        if seconds < limit: return name if unit == 1 else f'hace {int(seconds // unit)} {name}'
    return f'hace {int(seconds // (86400 * 30))} meses'


def scan(folder, terms, limit=300, seconds=3.0):
    """Búsqueda por nombre en vivo para carpetas que el índice no recorre (un disco recién conectado, /tmp…)."""
    from .catalog import walk_all
    words = [w for w in (normalize(t) for t in terms) if w] or []
    if not words: return []
    deadline = time.monotonic() + seconds
    found = []
    for _, path, is_dir, size, mtime in walk_all([folder]):
        name = normalize(path.name)
        if all(w in name for w in words):
            found.append((not name.startswith(words[0]), len(name), -mtime, path, is_dir, size, mtime))
        if time.monotonic() > deadline: break
    found.sort(key=lambda f: f[:3])
    return [make_hit(path, ['nombre'], kind='folder' if is_dir else None, size=size, mtime=mtime) for *_, path, is_dir, size, mtime in found[:limit]]
