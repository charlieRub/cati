"""Trabajo pesado de la vista previa: miniaturas, páginas PDF, HTML renderizado y metadatos.

Todo se ejecuta fuera del hilo de GTK y se guarda en caché por ruta, fecha y tamaño;
los originales nunca se modifican.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import sys
import zipfile
from collections import OrderedDict
from datetime import datetime
from functools import cache
from pathlib import Path

from PIL import Image, ImageOps

from .config import DATA
from .catalog import category

CACHE = DATA / 'previews'
TEXT_LIMIT = 400_000
OFFICE_LIMIT = 120_000   # una hoja de cálculo enorme: basta el principio para reconocerla
MONTHS = 'ene feb mar abr may jun jul ago sep oct nov dic'.split()


def cached(path, *parts, suffix='.png'):
    stat = Path(path).stat()
    identity = ':'.join(map(str, (path, stat.st_mtime_ns, stat.st_size, *parts)))
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    return CACHE / (hashlib.sha256(identity.encode()).hexdigest()[:32] + suffix)


def publish(dest, write):
    """Escribe a un temporal y lo renombra: dos vistas previas simultáneas nunca leen un archivo a medias."""
    partial = dest.with_name(f'{dest.stem}.{os.getpid()}.{threading.get_ident()}.part{dest.suffix}')
    try:
        write(partial)
        if partial.exists(): partial.replace(dest)
    finally:
        partial.unlink(missing_ok=True)
    return dest if dest.exists() else None


def run(args, timeout=20):
    return subprocess.run(args, check=True, capture_output=True, timeout=timeout)


def thumbnail(path, kind, size=128):
    """Miniatura cuadrada para la lista; None si el tipo no tiene imagen."""
    path = Path(path)
    if kind not in {'image', 'video', 'pdf'}: return None
    dest = cached(path, 'thumb', size)
    if dest.exists(): return dest
    def write(target):
        if kind == 'image':
            with Image.open(path) as source:
                source.draft('RGB', (size * 2, size * 2))
                bitmap = ImageOps.exif_transpose(source)
                bitmap = bitmap.convert('RGBA' if bitmap.mode in {'RGBA', 'LA', 'P'} else 'RGB')
                bitmap.thumbnail((size, size), Image.Resampling.LANCZOS)
                bitmap.save(target, 'PNG')
        elif kind == 'pdf':
            run(['pdftoppm', '-f', '1', '-l', '1', '-singlefile', '-scale-to', str(size), '-png', str(path), str(target.with_suffix(''))])
        else:
            length = duration(path) or 0
            run(['ffmpeg', '-v', 'error', '-y', '-ss', str(min(3, length / 3)), '-i', str(path), '-frames:v', '1',
                 '-vf', f'scale={size}:{size}:force_original_aspect_ratio=decrease', str(target)])
    return publish(dest, write)


def pdf_info(path):
    """Páginas y proporción alto/ancho de la primera, con una sola lectura del documento."""
    if poppler():
        document, lock = pdf_document(path)
        with lock:
            width, height = document.get_page(0).get_size() if document.get_n_pages() else (1, 1.414)
            return max(1, document.get_n_pages()), (height / width if width else 1.414)
    info = run(['pdfinfo', str(path)]).stdout.decode(errors='replace')
    pages = re.search(r'^Pages:\s+(\d+)', info, re.M)
    size = re.search(r'^Page size:\s+([\d.]+) x ([\d.]+)', info, re.M)
    return (int(pages[1]) if pages else 1), (float(size[2]) / float(size[1]) if size and float(size[1]) else 1.414)


def pdf_pages(path):
    return pdf_info(path)[0]


def pdf_size(path):
    """Proporción alto/ancho de la primera página, para reservar el hueco de cada página antes de dibujarla."""
    return pdf_info(path)[1]


@cache
def poppler():
    """Poppler en el propio proceso: renderiza una página hasta 15 veces más rápido que lanzar pdftoppm."""
    try:
        import gi
        gi.require_version('Poppler', '0.18')
        from gi.repository import Poppler
        import cairo  # noqa: F401
        return Poppler
    except (ImportError, ValueError):
        return None


_documents = OrderedDict()
_documents_lock = threading.Lock()


def pdf_document(path):
    """Documento abierto (y su cerrojo: Poppler no admite dos hilos en el mismo documento); guarda los últimos ocho."""
    from gi.repository import Gio
    stat = os.stat(path)
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _documents_lock:
        if key in _documents:
            _documents.move_to_end(key)
            return _documents[key]
    document = poppler().Document.new_from_file(Gio.File.new_for_path(str(path)).get_uri(), None)
    with _documents_lock:
        entry = _documents.setdefault(key, (document, threading.Lock()))
        while len(_documents) > 8: _documents.popitem(last=False)
        return entry


def pdf_texture(path, page, width=1400):
    """Textura de una página (1 = primera) a `width` píxeles de ancho, lista para GTK."""
    from gi.repository import Gdk, GLib
    if not poppler():
        return Gdk.Texture.new_from_filename(str(pdf_page(path, page, width)))
    import cairo
    document, lock = pdf_document(path)
    with lock:
        sheet = document.get_page(page - 1)
        if sheet is None: raise ValueError(f'el documento no tiene página {page}')
        points_wide, points_high = sheet.get_size()
        scale = width / max(points_wide, 1)
        height = max(1, round(points_high * scale))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        context = cairo.Context(surface)
        context.set_source_rgb(1, 1, 1)
        context.paint()
        context.scale(scale, scale)
        sheet.render(context)
    surface.flush()
    # Cairo guarda ARGB premultiplicado en el orden de bytes de la máquina.
    layout = Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED if sys.byteorder == 'little' else Gdk.MemoryFormat.A8R8G8B8_PREMULTIPLIED
    return Gdk.MemoryTexture.new(width, height, layout, GLib.Bytes.new(bytes(surface.get_data())), surface.get_stride())


def image_texture(path, limit=2400):
    """Datos de la imagen y su textura. GTK decodifica JPEG/PNG/TIFF a tamaño completo más rápido que
    escalar con GdkPixbuf (la GPU reduce al dibujar); las fotos giradas o enormes van por GdkPixbuf."""
    from gi.repository import Gdk
    info = image_info(path)
    width, height = info.get('size', (0, 0))
    if info.get('orientation', 1) == 1 and 0 < width * height <= 24_000_000:
        try: return info, Gdk.Texture.new_from_filename(str(path))
        except Exception: pass
    import gi
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), limit, limit, True)
    pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
    return info, Gdk.Texture.new_for_pixbuf(pixbuf)


class Textures:
    """Texturas recientes en memoria (por ruta, fecha y tamaño): volver a un archivo es instantáneo."""
    def __init__(self, budget=160 * 1024 * 1024):
        self.budget, self.used, self.items, self.lock = budget, 0, OrderedDict(), threading.Lock()

    @staticmethod
    def key(path, *parts):
        try: stat = os.stat(path)
        except OSError: return None
        return (str(path), stat.st_mtime_ns, stat.st_size, *parts)

    def get(self, key):
        with self.lock:
            if key not in self.items: return None
            self.items.move_to_end(key)
            return self.items[key][0]

    def clear(self):
        with self.lock:
            self.items.clear()
            self.used = 0

    def put(self, key, value, texture=None, cost=None):
        if key is None: return value
        if cost is None: cost = texture.get_width() * texture.get_height() * 4 if texture is not None else 1024
        with self.lock:
            if key in self.items: self.used -= self.items.pop(key)[1]
            self.items[key] = (value, cost)
            self.used += cost
            while self.used > self.budget and len(self.items) > 1:
                self.used -= self.items.popitem(last=False)[1][1]
        return value


OFFICE = {'.docx', '.doc', '.odt', '.rtf', '.pptx', '.ppt', '.odp', '.xlsx', '.xls', '.ods', '.epub'}


def office_pdf(path):
    """Word, PowerPoint, Excel y OpenDocument con su maquetación real, convertidos a PDF con LibreOffice
    (si está instalado). Perfil temporal propio: no toca la configuración de LibreOffice del usuario."""
    office = shutil.which('soffice') or shutil.which('libreoffice')
    if not office: return None
    dest = cached(path, 'office', suffix='.pdf')
    if dest.exists(): return dest
    def write(target):
        with tempfile.TemporaryDirectory(prefix='cati-office-') as work:
            run([office, f'-env:UserInstallation=file://{work}/profile', '--headless', '--norestore', '--convert-to', 'pdf',
                 '--outdir', work, str(path)], timeout=120)
            made = Path(work) / (Path(path).stem + '.pdf')
            if made.exists(): shutil.move(made, target)
    return publish(dest, write)


def pdf_page(path, page, width=1400):
    dest = cached(path, 'page', page, width)
    if dest.exists(): return dest
    return publish(dest, lambda target: run(['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile', '-scale-to-x', str(width),
                                             '-scale-to-y', '-1', '-png', str(path), str(target.with_suffix(''))], timeout=40))


def html_render(path, width=1280, height=2000):
    """Captura de la página con Chromium sin perfil ni red: los recursos remotos no se piden."""
    dest = cached(path, 'html', width, height)
    if dest.exists(): return dest
    browser = shutil.which('chromium') or shutil.which('google-chrome-stable') or shutil.which('chromium-browser')
    if not browser: return None
    def write(target):
        with tempfile.TemporaryDirectory(prefix='cati-html-') as profile:
            run([browser, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--mute-audio', '--no-first-run',
                 '--disable-extensions', '--disable-sync', '--proxy-server=127.0.0.1:9', '--proxy-bypass-list=<-loopback>',
                 f'--user-data-dir={profile}', f'--window-size={width},{height}', '--virtual-time-budget=1500',
                 f'--screenshot={target}', Path(path).resolve().as_uri()], timeout=30)
        # Trim the empty canvas below short pages.
        from PIL import ImageChops
        with Image.open(target) as shot:
            shot.load()
        background = Image.new(shot.mode, shot.size, shot.getpixel((shot.width - 1, shot.height - 1)))
        box = ImageChops.difference(shot, background).getbbox()
        if box and box[3] < shot.height - 40:
            shot.crop((0, 0, shot.width, min(shot.height, box[3] + 40))).save(target)
    return publish(dest, write)


def duration(path):
    try:
        out = run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)], 10).stdout
        return float(out.strip() or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def media_info(path):
    try:
        out = run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration,bit_rate:format_tags=title,artist,album,date:'
                   'stream=codec_type,codec_name,width,height,sample_rate,channels', '-of', 'json', str(path)], 10).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}
    fmt = data.get('format', {})
    info = {'duration': float(fmt.get('duration') or 0)}
    tags = {k.lower(): v for k, v in (fmt.get('tags') or {}).items()}
    for key in ('title', 'artist', 'album', 'date'):
        if tags.get(key): info[key] = tags[key]
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video' and stream.get('width'):
            info.setdefault('resolution', f"{stream['width']}×{stream['height']}")
            info.setdefault('video', stream.get('codec_name'))
        if stream.get('codec_type') == 'audio':
            info.setdefault('audio', stream.get('codec_name'))
    return info


def image_info(path):
    try:
        with Image.open(path) as source:
            width, height = source.size
            orientation = source.getexif().get(0x0112, 1)
            shown = (height, width) if orientation in {5, 6, 7, 8} else (width, height)
            return {'resolution': f'{shown[0]}×{shown[1]}', 'format': source.format, 'size': (width, height),
                    'orientation': orientation}
    except Exception:
        return {}


def read_text(path):
    """Texto legible de documentos, código y ofimática abierta."""
    from .readers import document
    path = Path(path)
    ext = path.suffix.lower()
    if ext in OFFICE:
        text = document(path, limit=OFFICE_LIMIT)
        return text[:OFFICE_LIMIT], len(text) > OFFICE_LIMIT
    with path.open('rb') as handle:
        raw = handle.read(TEXT_LIMIT + 1)
    if b'\x00' in raw[:4096]: raise ValueError('binario')
    return raw[:TEXT_LIMIT].decode('utf-8', errors='replace'), len(raw) > TEXT_LIMIT


def folder_listing(path, limit=400):
    entries = []
    total = folders = files = 0
    with os.scandir(path) as items:
        for item in items:
            if item.name.startswith('.'): continue
            try:
                is_dir = item.is_dir(follow_symlinks=False)
                stat = item.stat(follow_symlinks=False)
            except OSError:
                continue
            folders += is_dir
            files += not is_dir
            if not is_dir: total += stat.st_size
            entries.append((not is_dir, item.name.casefold(), item.name, is_dir, 0 if is_dir else stat.st_size, stat.st_mtime))
    entries.sort()
    return {'entries': [e[2:] for e in entries[:limit]], 'folders': folders, 'files': files, 'bytes': total,
            'images': [str(Path(path) / e[2]) for e in entries if not e[3] and category(Path(e[2]).suffix.lower()) == 'image'][:12]}


def archive_listing(path, limit=300):
    path = Path(path)
    names = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [(i.filename, i.file_size) for i in archive.infolist()[:limit]]
            count = len(archive.infolist())
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for member in archive:
                if len(names) < limit: names.append((member.name, member.size))
            count = len(archive.getmembers())
    else:
        return None
    return {'entries': names, 'count': count}


def describe(path):
    try: return run(['file', '-b', str(path)], 5).stdout.decode(errors='replace').strip()
    except (subprocess.SubprocessError, OSError): return ''


def human_size(size):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'.replace('.', ',')
        size /= 1024


def human_time(timestamp, now=None):
    now = now or datetime.now()
    moment = datetime.fromtimestamp(timestamp)
    delta = now - moment
    if delta.total_seconds() < 60: return 'ahora'
    if delta.total_seconds() < 3600: return f'hace {int(delta.total_seconds() // 60)} min'
    if moment.date() == now.date(): return f'hoy {moment:%H:%M}'
    if (now.date() - moment.date()).days == 1: return f'ayer {moment:%H:%M}'
    if moment.year == now.year: return f'{moment.day} {MONTHS[moment.month - 1]}'
    return f'{moment.day} {MONTHS[moment.month - 1]} {moment.year}'


def clock(seconds):
    seconds = int(seconds or 0)
    return f'{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}' if seconds >= 3600 else f'{seconds // 60}:{seconds % 60:02d}'
