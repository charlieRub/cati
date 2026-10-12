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
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from .config import DATA
from .catalog import category

CACHE = DATA / 'previews'
TEXT_LIMIT = 400_000
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


def pdf_pages(path):
    info = run(['pdfinfo', str(path)]).stdout.decode(errors='replace')
    match = re.search(r'^Pages:\s+(\d+)', info, re.M)
    return int(match[1]) if match else 1


def pdf_size(path):
    """Proporción alto/ancho de la primera página, para reservar el hueco de cada página antes de dibujarla."""
    info = run(['pdfinfo', str(path)]).stdout.decode(errors='replace')
    match = re.search(r'^Page size:\s+([\d.]+) x ([\d.]+)', info, re.M)
    return float(match[2]) / float(match[1]) if match and float(match[1]) else 1.414


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
            if orientation in {5, 6, 7, 8}: width, height = height, width
            return {'resolution': f'{width}×{height}', 'format': source.format}
    except Exception:
        return {}


def read_text(path):
    """Texto legible de documentos, código y ofimática abierta."""
    from .readers import document
    path = Path(path)
    ext = path.suffix.lower()
    if ext in OFFICE:
        text = document(path)
        return text[:TEXT_LIMIT], len(text) > TEXT_LIMIT
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
