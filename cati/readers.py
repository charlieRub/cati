"""Lectores Linux: Poppler, Tesseract, Pillow y FFmpeg; nunca modifican el original."""
import html
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import queue
import threading
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageOps

TEXT = {".txt", ".text", ".md", ".markdown", ".rst", ".org", ".html", ".htm", ".docx", ".odt",
        ".rtf", ".epub", ".xlsx", ".pptx", ".ods", ".odp"}
LEGACY_DOC = {".doc"}  # only with antiword or catdoc installed
ARCHIVE_MEDIA = {".docx": "word/media/", ".pptx": "ppt/media/", ".xlsx": "xl/media/", ".odt": "Pictures/",
                 ".odp": "Pictures/", ".ods": "Pictures/", ".epub": ""}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
AUDIO = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
VIDEO = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
CODE = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".c", ".h", ".cpp", ".java", ".swift", ".sh", ".lua", ".vue", ".svelte", ".kt", ".rb", ".php", ".sql"}
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "vendor", "Vendor", "third_party", "build", "dist", "target", "site-packages", "cache", "caches"}
PROJECT_MARKERS = {".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Package.swift", "CMakeLists.txt", "requirements.txt"}
SECRET_EXT = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".gpg", ".asc", ".crt", ".cer", ".der", ".ppk"}
SECRET_NAMES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", ".env", "secret", "credential", "password", "passwd", "contraseñ", "htpasswd")


@dataclass
class Segment:
    text: str
    kind: str
    loc: float | None = None
    end: float | None = None
    media: str | None = None
    cursor: tuple | None = None


def allowed(path):
    name = path.name.lower()
    return not (name.startswith(".") or name.startswith(SECRET_NAMES) or path.suffix.lower() in SECRET_EXT
                or re.search(r"(?:^|[-_. ])(?:passwords?|credentials?|secrets?|contraseñas?)(?:[-_. ]|$)", name))


def file_kind(path, code=False):
    ext = path.suffix.lower()
    if not allowed(path):
        return None
    if ext == ".pdf": return "pdf"
    if ext in TEXT: return "document"
    if ext in LEGACY_DOC and (shutil.which("antiword") or shutil.which("catdoc")): return "document"
    if ext in IMAGES: return "image"
    if ext in AUDIO: return "audio"
    if ext in VIDEO: return "video"
    if code and ext in CODE: return "code"
    return None


def walk(config, on_directory=None, stop=None):
    excluded = [Path(p).expanduser().resolve() for p in config["exclude"]]
    seen = set()
    for root in config["folders"]:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            continue
        projects = {}
        git_files = {}
        for folder, dirs, files in os.walk(root, followlinks=False):
            if stop and stop.is_set(): return
            base = Path(folder)
            if any(base == p or p in base.parents for p in excluded):
                dirs[:] = []
                continue
            if on_directory: on_directory(base)
            names = set(dirs) | set(files)
            in_project = bool(names & PROJECT_MARKERS) or projects.get(base.parent, False)
            projects[base] = in_project
            repo = next((p for p in [base, *base.parents] if p in git_files), None)
            if config['code'] and '.git' in names:
                try:
                    entries = command(['git', '-C', str(base), 'ls-files', '-c', '-o', '--exclude-standard', '-z'])
                    git_files[base] = {base / name for name in entries.split('\0') if name}
                    repo = base
                except (subprocess.SubprocessError, OSError):
                    git_files[base] = set()
                    repo = base
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS
                       and not (base / d).is_symlink() and not d.endswith((".noindex", ".app"))]
            datasets = set()
            if config.get('skip_datasets', False) and base != root:
                groups = defaultdict(list)
                for name in files:
                    candidate = base / name
                    media_kind = file_kind(candidate)
                    if media_kind in {'image', 'audio', 'video'}: groups[media_kind].append(candidate.stem)
                for media_kind, stems in groups.items():
                    machine = sum(bool(re.fullmatch(r'[0-9a-f_-]{8,}', s, re.I) or re.search(r'\d{5,}', s)) for s in stems)
                    if len(stems) >= 300 and machine / len(stems) >= .7: datasets.add(media_kind)
            for name in sorted(files):
                path = base / name
                if path.is_symlink() or path in seen:
                    continue
                kind = file_kind(path, config["code"])
                if not kind or kind in datasets:
                    continue
                if in_project and not config['code'] and config.get('project_screenshots_only', False):
                    if kind != 'image' or not name.lower().startswith(('screenshot', 'screen shot', 'captura', 'cleanshot', 'scr-')):
                        continue
                if config['code'] and repo and path not in git_files[repo]: continue
                if config['code'] and in_project and kind == 'document': kind = 'code'
                if not config["media"] and kind in {"image", "audio", "video"}:
                    continue
                seen.add(path)
                yield path, kind


def command(args, timeout=90, binary=False):
    result = subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def chunks(text, kind, loc=None, max_chars=1800):
    text = text.replace("\x00", "").strip()
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + max_chars // 2, end)
            if boundary > start: end = boundary
        part = text[start:end].strip()
        if part:
            line = text[:start].count("\n") + 1 if kind == "code" else loc
            yield Segment(part, kind, line)
        if end == len(text): break
        start = max(start + 1, end - 180)


def xml_text(data, paragraph="p"):
    tree = ElementTree.fromstring(data)
    return "\n".join(" ".join(t.strip() for t in x.itertext() if t.strip()) for x in tree.iter() if x.tag.endswith("}" + paragraph))


def numbered(names, prefix):
    """slide10.xml after slide9.xml."""
    return sorted((n for n in names if n.startswith(prefix) and n.endswith(".xml")),
                  key=lambda n: int(re.search(r"(\d+)\.xml$", n)[1]) if re.search(r"(\d+)\.xml$", n) else 0)


def spreadsheet(archive):
    """Celdas de XLSX por filas: «Hoja 1: concepto | importe».""" 
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        tree = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter() if t.tag.endswith("}t")) for si in tree if si.tag.endswith("}si")]
    lines = []
    for number, name in enumerate(numbered(archive.namelist(), "xl/worksheets/sheet"), 1):
        lines.append(f"Hoja {number}:")
        for row in ElementTree.fromstring(archive.read(name)).iter():
            if not row.tag.endswith("}row"): continue
            cells = []
            for cell in row:
                value = next((v.text for v in cell.iter() if v.tag.endswith("}v") or v.tag.endswith("}t")), None)
                if value is None: continue
                cells.append(shared[int(value)] if cell.get("t") == "s" and value.isdigit() and int(value) < len(shared) else value)
            if cells: lines.append(" | ".join(cells))
    return "\n".join(lines)


def rtf(raw):
    """Texto plano de RTF: quita grupos de control y decodifica \\'xx y \\uN."""
    raw = re.sub(r"\{\\\*[^{}]*\}|\{\\(?:fonttbl|colortbl|stylesheet|info|pict)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", "", raw)
    raw = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m[1], 16)]).decode("cp1252", "replace"), raw)
    raw = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m[1]) % 65536), raw)
    raw = re.sub(r"\\(?:par|line)\b ?", "\n", raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", raw)
    return raw.replace("\\\\", "\\")


def document(path):
    ext = path.suffix.lower()
    if ext in {".docx", ".odt", ".ods", ".odp", ".pptx", ".xlsx", ".epub"}:
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist() if i.filename.endswith((".xml", ".xhtml", ".html", ".htm"))) > 64 * 1024 * 1024:
                raise ValueError("Documento expandido demasiado grande")
            names = archive.namelist()
            if ext == ".docx": return xml_text(archive.read("word/document.xml"))
            if ext in {".odt", ".ods", ".odp"}: return xml_text(archive.read("content.xml"))
            if ext == ".xlsx": return spreadsheet(archive)
            if ext == ".pptx":
                return "\n\n".join(f"Diapositiva {n}:\n" + xml_text(archive.read(name)) for n, name in enumerate(numbered(names, "ppt/slides/slide"), 1))
            # EPUB: chapters in reading order when the OPF spine is readable, else by name.
            pages = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
            try:
                opf = next(n for n in names if n.endswith(".opf"))
                tree = ElementTree.fromstring(archive.read(opf))
                items = {i.get("id"): i.get("href") for i in tree.iter() if i.tag.endswith("}item")}
                base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
                ordered = [base + items[r.get("idref")] for r in tree.iter() if r.tag.endswith("}itemref") and r.get("idref") in items]
                pages = [p for p in ordered if p in names] or pages
            except (StopIteration, ElementTree.ParseError):
                pass
            return "\n\n".join(html_text(archive.read(name).decode("utf-8", "replace")) for name in pages)
    if ext == ".doc":
        tool = ["antiword", "-w", "0"] if shutil.which("antiword") else ["catdoc", "-w"]
        return command([*tool, str(path)], timeout=60)
    if ext == ".rtf":
        return rtf(path.read_bytes().decode("latin-1"))
    text = path.read_text(encoding="utf-8", errors="replace")
    if ext in {".html", ".htm"}: text = html_text(text)
    return text


def html_text(text):
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    text = re.sub(r"<(?:br|/p|/div|/h\d|/li|/tr)\b[^>]*>", "\n", text, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def embedded_images(path, work, limit=24, minimum=256):
    """Imágenes guardadas dentro de un DOCX/PPTX/XLSX/ODF/EPUB (sin iconos pequeños)."""
    prefix = ARCHIVE_MEDIA.get(path.suffix.lower())
    if prefix is None: return []
    found = []
    with zipfile.ZipFile(path) as archive:
        for info in sorted(archive.infolist(), key=lambda i: -i.file_size):
            name = info.filename
            if not name.startswith(prefix) or Path(name).suffix.lower() not in IMAGES or info.file_size < 12 * 1024: continue
            target = work / f"embedded-{len(found)}{Path(name).suffix.lower()}"
            target.write_bytes(archive.read(name))
            try:
                with Image.open(target) as im:
                    if min(im.size) < minimum: continue
            except Exception:
                continue
            found.append(target)
            if len(found) >= limit: break
    return found


def pdf_images(path, minimum=256, limit=40):
    """Fotos de un PDF por página según «pdfimages -list»: sin máscaras, iconos ni logotipos repetidos."""
    listing = command(["pdfimages", "-list", str(path)], timeout=60).splitlines()[2:]
    seen, pages, position = set(), {}, {}
    for line in listing:
        parts = line.split()
        if len(parts) < 12 or not parts[0].isdigit(): continue
        page = int(parts[0])
        # A one-page extraction numbers every image of that page (masks included) from 0.
        local = position.get(page, 0)
        position[page] = local + 1
        width, height, obj = int(parts[3]), int(parts[4]), parts[10] + parts[11]
        if parts[2] != "image" or min(width, height) < minimum or obj in seen or len(seen) >= limit: continue
        seen.add(obj)  # a logo repeated on every page is the same object: only its first use counts
        pages.setdefault(page, []).append(local)
    return pages


def image(path, dest, budget=140):
    with Image.open(path) as source:
        bitmap = ImageOps.exif_transpose(source).convert("RGB")
        width, height = bitmap.size
        factor = math.sqrt(budget * 48 * 48 / (width * height))
        target_width = int(width * factor / 48) * 48
        target_height = int(height * factor / 48) * 48
        if not target_height:
            target_height = 48
            target_width = min(width // height * 48, budget * 48)
        elif not target_width:
            target_width = 48
            target_height = min(height // width * 48, budget * 48)
        bitmap = bitmap.resize((target_width,target_height),Image.Resampling.BICUBIC)
        bitmap.save(dest, "PNG")
    return dest


def thumbnail_path(path, loc=None):
    from .config import DATA
    stat = path.stat()
    identity = f'{path}:{stat.st_mtime_ns}:{stat.st_size}:{loc}'
    return DATA / 'thumbnails' / (hashlib.sha256(identity.encode()).hexdigest() + '.jpg')


def cache_thumbnail(source, original, loc=None):
    dest = thumbnail_path(original, loc)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Image.open(source) as im:
        bitmap = im.convert('RGB')
        bitmap.thumbnail((180,180))
        bitmap.save(dest,'JPEG',quality=80)


OCR_INSTANCES = 2
_pool = queue.Queue()
_created = 0
_pool_lock = threading.Lock()
_unavailable = False


def _new_ocr():
    from rapidocr import RapidOCR
    import logging
    logging.getLogger('RapidOCR').setLevel(logging.ERROR)
    params = {'Rec.lang_type': 'es', 'Global.use_cls': False, 'Global.log_level': 'error',
              'EngineConfig.onnxruntime.intra_op_num_threads': 2, 'EngineConfig.onnxruntime.inter_op_num_threads': 1,
              'EngineConfig.onnxruntime.enable_cpu_mem_arena': False}
    try:
        import onnxruntime
        if 'CUDAExecutionProvider' in onnxruntime.get_available_providers():
            params['EngineConfig.onnxruntime.use_cuda'] = True  # NVIDIA: OCR on the GPU
    except Exception:
        pass
    return RapidOCR(params=params)


def rapid():
    """Toma una instancia de PP-OCRv6 del grupo (máx. OCR_INSTANCES, 2 hilos cada una, sin reserva
    de memoria de ONNX). Devuelve None si el OCR moderno no está disponible."""
    global _created, _unavailable
    if _unavailable: return None
    try:
        return _pool.get_nowait()
    except queue.Empty:
        pass
    with _pool_lock:
        if _created < OCR_INSTANCES:
            try:
                engine = _new_ocr()
            except Exception:
                _unavailable = True
                return None
            _created += 1
            return engine
    return _pool.get()


def give_back(engine):
    _pool.put(engine)


def release_ocr():
    """Libera las instancias inactivas; se recrean al volver a necesitarlas."""
    global _created
    with _pool_lock:
        while True:
            try: _pool.get_nowait()
            except queue.Empty: break
            _created -= 1
    import gc
    gc.collect()


def tesseract(path):
    from .config import DATA
    local = DATA / 'tessdata'
    args = ['--tessdata-dir', str(local)] if (local / 'spa.traineddata').exists() and (local / 'eng.traineddata').exists() else []
    languages = command(["tesseract", *args, "--list-langs"]).splitlines()[1:]
    selected = "+".join(x for x in ["spa", "eng"] if x in languages) or languages[0]
    return command(["tesseract", str(path), "stdout", *args, "-l", selected], timeout=60).strip()


def ocr(path):
    engine = rapid()
    if engine is not None:
        try:
            result = engine(str(path))
            return '\n'.join(result.txts or ()).strip()
        except Exception:
            pass
        finally:
            give_back(engine)
    return tesseract(path)


def duration(path):
    return float(command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).strip())


def extract(path, kind, config, stop=None, cursor=None):
    """Temporary media exists until the yielded segment has been consumed."""
    def cancelled():
        if stop and stop.is_set(): raise InterruptedError("Indexación pausada")

    position = tuple(cursor or (0,0))
    def section(items, section_id):
        for ordinal, item in enumerate(items):
            if (section_id, ordinal) < position: continue
            item.cursor = (section_id, ordinal+1)
            yield item

    if kind in {"document", "code"}:
        text = document(path)
        if "-----BEGIN " in text and "PRIVATE KEY-----" in text:
            raise ValueError("Archivo con clave privada excluido")
        yield from section(chunks(text, kind),0)
        if kind == "document" and config["media"] and path.suffix.lower() in ARCHIVE_MEDIA:
            with tempfile.TemporaryDirectory(prefix="cati-") as work:
                pictures = []
                for index, picture in enumerate(embedded_images(path, Path(work))):
                    dest = image(picture, Path(work) / f"model-{index}.png", 140)
                    pictures.append(Segment("Imagen dentro del documento", "doc-image", media=str(dest)))
                yield from section(pictures, 1)
        return
    with tempfile.TemporaryDirectory(prefix="cati-") as work:
        work = Path(work)
        if kind == "image":
            screenshot = path.name.lower().startswith(('screenshot','screen shot','captura','cleanshot','scr-'))
            dest = image(path, work / "image.png",280 if screenshot else 140)
            yield from section([Segment(path.stem, "image", media=str(dest))],0)
            cancelled()
            # OCR needs readable text rather than the smaller vision token budget.
            ocr_dest = image(path,work/'ocr.png',560)
            text = ocr(ocr_dest)
            if text: yield from section(chunks(text,"ocr"),1)
        elif kind == "pdf":
            info = command(["pdfinfo", str(path)])
            count = int(re.search(r"^Pages:\s+(\d+)", info, flags=re.M)[1])
            if count > config["max_pdf_pages"]:
                raise ValueError(f"PDF supera el límite de {config['max_pdf_pages']} páginas")
            photos = {}
            if config["media"]:
                try: photos = pdf_images(path)
                except (subprocess.SubprocessError, OSError, ValueError): photos = {}
            texts = {}
            for page in range(max(1,position[0]), count + 1):
                cancelled()
                if page not in texts:
                    # One pdftotext call per 40 pages instead of one process per page.
                    last = min(count, page + 39)
                    block = command(["pdftotext", "-f", str(page), "-l", str(last), "-layout", str(path), "-"], timeout=120).split("\f")
                    texts = {page + i: t for i, t in enumerate(block[:last - page + 1])}
                text = texts.get(page, "")
                pieces = []
                if len(text.strip()) < 30:
                    prefix = work / "page"
                    command(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "1600", "-png", str(path), str(prefix)])
                    text = ocr(prefix.with_suffix(".png"))
                    if config["media"]:
                        dest = image(prefix.with_suffix(".png"), work / "resized.png",280)
                        pieces.append(Segment(f"Página {page}", "pdf-image", page, media=str(dest)))
                elif page in photos:
                    for old in work.glob("photo-*"): old.unlink()
                    command(["pdfimages", "-f", str(page), "-l", str(page), "-j", "-png", str(path), str(work / "photo")], timeout=60)
                    extracted = sorted(work.glob("photo-*"))
                    for index, local in enumerate(photos[page]):
                        if local < len(extracted):
                            dest = image(extracted[local], work / f"photo-model-{index}.png", 140)
                            pieces.append(Segment(f"Imagen en la página {page}", "doc-image", page, media=str(dest)))
                pieces.extend(chunks(text,"pdf",page))
                yield from section(pieces,page)
        elif kind in {"audio", "video"}:
            length = duration(path)
            if kind == "video" and position[0] == 0:
                for ordinal, second in enumerate(range(position[1] * config['video_stride'], int(length) + 1, config["video_stride"]), position[1]):
                    if second >= length: break
                    cancelled()
                    dest = work / "frame.png"
                    command(["ffmpeg", "-v", "error", "-y", "-ss", str(second), "-i", str(path), "-frames:v", "1", "-vf", "scale=768:768:force_original_aspect_ratio=decrease", str(dest)])
                    prepared = image(dest,work/'frame-model.png',70)
                    yield Segment(f"Fotograma en {second} s", "video-frame", second, media=str(prepared),cursor=(0,ordinal+1))
            if kind == 'video':
                streams = command(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
                if not streams.strip(): return
            phase = 1 if kind == 'video' else 0
            start = position[1] if position[0] == phase else 0
            for ordinal, second in enumerate(range(start * 25, int(length) + 1, 25),start):
                if second >= length: break
                cancelled()
                dest = work / "audio.wav"
                command(["ffmpeg", "-v", "error", "-y", "-ss", str(second), "-i", str(path), "-t", "30", "-vn", "-ac", "1", "-ar", "16000", str(dest)])
                yield Segment(f"Audio en {second}–{min(second + 30, length):.0f} s", "audio", second,
                              min(second + 30, length), media=str(dest),cursor=(phase,ordinal+1))
