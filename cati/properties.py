"""Propiedades de un archivo o carpeta, agrupadas por secciones legibles.

Devuelve [(sección, [(etiqueta, valor), …]), …]; todo se lee sin modificar el original.
"""
import grp
import json
import mimetypes
import os
import pwd
import re
import shutil
import stat
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from PIL import ExifTags, Image

from .preview import clock, human_size

MONTHS = 'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre'.split()


def when(timestamp):
    if not timestamp: return None
    moment = datetime.fromtimestamp(timestamp)
    return f'{moment.day} de {MONTHS[moment.month - 1]} de {moment.year}, {moment:%H:%M}'


def thousands(number):
    return f'{number:,}'.replace(',', '.')


def exact_size(size):
    return f'{human_size(size)} ({thousands(size)} bytes)'


def iso(value):
    """Fechas ISO de PDF y Office («2021-05-16T15:37:00Z») en formato legible y hora local."""
    try:
        moment = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return str(value)
    return when(moment.timestamp())


def output(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def birth(path):
    value = output(['stat', '-c', '%W', str(path)]).strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def general(path, is_dir):
    info = path.lstat()
    rows = [('Nombre', path.name), ('Ubicación', str(path.parent))]
    if is_dir:
        rows.append(('Tipo', 'Carpeta'))
    else:
        mime = output(['file', '-b', '--mime-type', str(path)]).strip() or mimetypes.guess_type(path.name)[0] or ''
        description = output(['file', '-b', str(path)]).strip()
        rows += [('Tipo', description[:160] or mime), ('Formato MIME', mime), ('Tamaño', exact_size(info.st_size))]
    rows += [('Creado', when(birth(path))), ('Modificado', when(info.st_mtime)), ('Último acceso', when(info.st_atime))]
    try: owner = pwd.getpwuid(info.st_uid).pw_name
    except KeyError: owner = str(info.st_uid)
    try: group = grp.getgrgid(info.st_gid).gr_name
    except KeyError: group = str(info.st_gid)
    rows += [('Propietario', f'{owner} (grupo {group})'),
             ('Permisos', f'{stat.filemode(info.st_mode)}  ({oct(info.st_mode & 0o777)[2:]})'),
             ('Ejecutable', 'sí' if not is_dir and os.access(path, os.X_OK) else None)]
    return [r for r in rows if r[1]]


def fraction(value):
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number


def gps(info):
    def degrees(values, ref):
        try:
            d, m, s = (float(v) for v in values)
        except (TypeError, ValueError):
            return None
        value = d + m / 60 + s / 3600
        return -value if ref in {'S', 'W'} else value
    lat = degrees(info.get(2), info.get(1))
    lon = degrees(info.get(4), info.get(3))
    return f'{lat:.6f}, {lon:.6f}' if lat is not None and lon is not None else None


def image(path):
    rows = []
    with Image.open(path) as source:
        width, height = source.size
        rows += [('Dimensiones', f'{width} × {height} px ({width * height / 1e6:.1f} MP)'.replace('.', ',')), ('Formato', source.format),
                 ('Color', source.mode)]
        dpi = source.info.get('dpi')
        if dpi: rows.append(('Resolución', f'{round(float(dpi[0]))} ppp'))
        exif = source.getexif()
    if exif:
        tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
        detail = {ExifTags.TAGS.get(k, k): v for k, v in exif.get_ifd(0x8769).items()}
        camera = ' '.join(str(tags.get(k, '')).strip() for k in ('Make', 'Model')).strip()
        rows.append(('Cámara', camera))
        rows.append(('Objetivo', str(detail.get('LensModel', '')).strip()))
        taken = detail.get('DateTimeOriginal') or tags.get('DateTime')
        if taken:
            try: rows.append(('Fecha de la toma', when(time.mktime(datetime.strptime(str(taken), '%Y:%m:%d %H:%M:%S').timetuple()))))
            except ValueError: rows.append(('Fecha de la toma', str(taken)))
        exposure = fraction(detail.get('ExposureTime'))
        if exposure: rows.append(('Exposición', f'1/{round(1 / exposure)} s' if exposure < 1 else f'{exposure:g} s'))
        aperture = fraction(detail.get('FNumber'))
        if aperture: rows.append(('Apertura', f'ƒ/{aperture:g}'))
        if detail.get('ISOSpeedRatings'): rows.append(('ISO', str(detail['ISOSpeedRatings'])))
        focal = fraction(detail.get('FocalLength'))
        if focal: rows.append(('Distancia focal', f'{focal:g} mm'))
        rows.append(('Software', str(tags.get('Software', '')).strip()))
        location = gps(exif.get_ifd(0x8825))
        if location: rows.append(('Ubicación GPS', location))
    return [r for r in rows if r[1]]


def pdf(path):
    info = {}
    for line in output(['pdfinfo', '-isodates', str(path)]).splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            info[key.strip()] = value.strip()
    rows = [('Páginas', info.get('Pages')), ('Título', info.get('Title')), ('Autor', info.get('Author')),
            ('Asunto', info.get('Subject')), ('Creado con', info.get('Creator')), ('Generado por', info.get('Producer')),
            ('Fecha de creación', iso(info['CreationDate']) if info.get('CreationDate') else None),
            ('Última modificación', iso(info['ModDate']) if info.get('ModDate') else None), ('Tamaño de página', info.get('Page size')),
            ('Versión PDF', info.get('PDF version')),
            ('Protegido', 'sí' if info.get('Encrypted', 'no').startswith('yes') else None),
            ('Formulario', None if info.get('Form', 'none') == 'none' else info.get('Form'))]
    return [r for r in rows if r[1]]


def media(path):
    try:
        data = json.loads(output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], 15))
    except ValueError:
        return []
    fmt = data.get('format', {})
    rows = []
    seconds = float(fmt.get('duration') or 0)
    if seconds:
        rows.append(('Duración', clock(seconds)))
    if fmt.get('bit_rate'): rows.append(('Tasa de bits', f"{int(fmt['bit_rate']) / 1000:.0f} kbps"))
    rows.append(('Contenedor', fmt.get('format_long_name')))
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video' and stream.get('width') and stream.get('disposition', {}).get('attached_pic') != 1:
            rate = stream.get('avg_frame_rate', '0/1')
            try:
                num, den = rate.split('/')
                fps = f', {float(num) / float(den):.3g} fps' if float(den) else ''
            except ValueError:
                fps = ''
            rows.append(('Vídeo', f"{stream.get('codec_name', '').upper()}, {stream['width']} × {stream['height']}{fps}"))
        elif stream.get('codec_type') == 'audio':
            channels = {1: 'mono', 2: 'estéreo'}.get(stream.get('channels'), f"{stream.get('channels')} canales")
            rate = f", {int(stream['sample_rate']) / 1000:g} kHz" if stream.get('sample_rate') else ''
            rows.append(('Audio', f"{stream.get('codec_name', '').upper()}, {channels}{rate}"))
    names = {'title': 'Título', 'artist': 'Artista', 'album': 'Álbum', 'date': 'Fecha', 'genre': 'Género',
             'comment': 'Comentario', 'encoder': 'Codificado con'}
    tags = {k.lower(): v for k, v in (fmt.get('tags') or {}).items()}
    rows += [(label, tags.get(key)) for key, label in names.items()]
    return [r for r in rows if r[1]]


def office(path):
    """Metadatos de DOCX/XLSX/PPTX (docProps/core.xml) y ODF (meta.xml)."""
    rows = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            source = 'docProps/core.xml' if 'docProps/core.xml' in names else 'meta.xml' if 'meta.xml' in names else None
            if not source: return []
            tree = ElementTree.fromstring(archive.read(source))
    except (zipfile.BadZipFile, OSError, ElementTree.ParseError):
        return []
    found = {}
    for element in tree.iter():
        tag = element.tag.rsplit('}', 1)[-1]
        if element.text and element.text.strip(): found.setdefault(tag, element.text.strip())
    for key, label in (('title', 'Título'), ('subject', 'Asunto'), ('creator', 'Autor'), ('initial-creator', 'Autor'),
                       ('lastModifiedBy', 'Última edición por'), ('created', 'Creado (documento)'),
                       ('creation-date', 'Creado (documento)'), ('modified', 'Modificado (documento)'),
                       ('date', 'Modificado (documento)'), ('revision', 'Revisión'), ('editing-cycles', 'Revisión'),
                       ('generator', 'Aplicación')):
        if key in found and label not in dict(rows):
            value = iso(found[key]) if label.startswith(('Creado', 'Modificado')) else found[key][:120]
            rows.append((label, value))
    return rows


def text(path):
    with path.open('rb') as handle:
        raw = handle.read(8 * 1024 * 1024)
    if b'\x00' in raw[:4096]: return []
    try:
        content, encoding = raw.decode('utf-8'), 'UTF-8'
    except UnicodeDecodeError:
        content, encoding = raw.decode('latin-1'), 'Latin-1 (probable)'
    truncated = path.stat().st_size > len(raw)
    more = '+' if truncated else ''
    words = len(re.findall(r'\w+', content))
    rows = [('Líneas', thousands(content.count('\n') + 1) + more), ('Palabras', thousands(words) + more),
            ('Caracteres', thousands(len(content)) + more),
            ('Codificación', encoding),
            ('Saltos de línea', 'Windows (CRLF)' if '\r\n' in content[:65536] else 'Unix (LF)')]
    title = re.search(r'<title>(.*?)</title>', content[:65536], re.S | re.I) if path.suffix.lower() in {'.html', '.htm'} else None
    heading = re.search(r'^#\s+(.+)$', content[:65536], re.M) if path.suffix.lower() in {'.md', '.markdown'} else None
    if title or heading: rows.insert(0, ('Título', (title or heading).group(1).strip()[:120]))
    return rows


def folder(path, budget=1.5):
    """Recuento recursivo con límite de tiempo: una carpeta enorme no bloquea el panel."""
    files = folders = total = 0
    kinds = {}
    deadline = time.monotonic() + budget
    complete = True
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            complete = False
            break
        try: entries = list(os.scandir(stack.pop()))
        except OSError: continue
        for entry in entries:
            if entry.name.startswith('.'): continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    folders += 1; stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    files += 1; total += entry.stat(follow_symlinks=False).st_size
                    ext = Path(entry.name).suffix.lower() or 'sin extensión'
                    kinds[ext] = kinds.get(ext, 0) + 1
            except OSError:
                continue
    more = '' if complete else ' (recuento parcial)'
    common = ', '.join(f'{ext} ({n})' for ext, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:5])
    return [('Contenido', f"{thousands(files)} {'archivo' if files == 1 else 'archivos'} y {thousands(folders)} {'carpeta' if folders == 1 else 'carpetas'}{more}"),
            ('Tamaño total', exact_size(total) + more), ('Tipos más comunes', common)]


SKIP_GROUPS = {'ExifTool', 'System'}


def words(tag):
    """CreateDate → Create Date; GPSLatitude → GPS Latitude."""
    return re.sub(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', tag)


def everything(path):
    """Todos los metadatos que conoce ExifTool, agrupados por origen (EXIF, XMP, IPTC, QuickTime…)."""
    if not shutil.which('exiftool'): return None
    try:
        data = json.loads(output(['exiftool', '-j', '-a', '-G1', '-charset', 'filename=utf8', '-api', 'LargeFileSupport=1',
                                  str(path)], 20) or '[]')
    except ValueError:
        return []
    groups = {}
    for key, value in (data[0] if data else {}).items():
        if ':' not in key: continue
        group, tag = key.split(':', 1)
        if group in SKIP_GROUPS: continue
        if isinstance(value, list): value = ', '.join(map(str, value))
        value = str(value).strip()
        if not value: continue
        groups.setdefault(group, []).append((words(tag), value[:400]))
    return sorted(groups.items(), key=lambda item: (item[0] == 'Composite', item[0]))


def collect(path, kind):
    path = Path(path)
    is_dir = path.is_dir()
    sections = [('General', general(path, is_dir))]
    detail = []
    try:
        if is_dir: detail = folder(path)
        elif kind == 'image': detail = image(path)
        elif kind == 'pdf': detail = pdf(path)
        elif kind in {'video', 'audio'}: detail = media(path)
        elif path.suffix.lower() in {'.docx', '.xlsx', '.pptx', '.odt', '.ods', '.odp'}: detail = office(path)
        elif kind in {'document', 'code'} or path.suffix.lower() in {'.csv', '.tsv'}: detail = text(path)
    except Exception as exc:
        detail = [('No disponible', str(exc)[:160])]
    titles = {'image': 'Imagen', 'pdf': 'Documento PDF', 'video': 'Vídeo', 'audio': 'Audio', 'folder': 'Carpeta'}
    if detail: sections.append((titles.get('folder' if is_dir else kind, 'Contenido'), detail))
    return sections


def sha256(path):
    import hashlib
    with open(path, 'rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()
