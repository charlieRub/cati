"""Interpreta una frase en español o inglés: tipo, extensión, fechas, lugar y tema."""
import calendar
import re
import subprocess
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))


STOP = set(normalize(w) for w in '''
a al algo alguna algun algún alguno algunos aqui aquí buscar busca buscame búscame buscando busco con como cómo de del el ella en
es esta este eso esa hay la las lo los me mi mis para por que qué quiero se si sobre su sus te tengo un una unas unos y o u
donde dónde dame dime enseñame enséñame muestrame muéstrame mostrar ver necesito encuentra encuentrame encuéntrame encontrar
habla hablan hable hablen trata tratan trate traten sale salen aparece aparecen contiene contienen contenga contengan tiene
tienen tenga tengan relacionado relacionada relacionados relacionadas acerca cosa cosas archivo archivos fichero ficheros
todo todos toda todas mio mia mios mias mío mía míos mías tu tus ese esos esas aquel cual cuál cuales donde guardado guardada
guardados guardadas hice hizo era eran fue sea sean ser estar esta están estan creo creado creada creados creadas
the a an of in on for to about find where is are my i want please show me some any with that which file files all
'''.split())

KIND_WORDS = {
    'image': 'foto fotos fotografia fotografias imagen imagenes photo photos picture pictures image images captura capturas '
             'screenshot screenshots pantallazo pantallazos',
    'documents': 'documento documentos document documents doc docs texto textos',
    'video': 'video videos clip clips pelicula peliculas movie movies',
    'audio': 'audio audios sonido sonidos voz podcast podcasts cancion canciones song songs musica music',
    'folder': 'carpeta carpetas directorio directorios folder folders directory directories',
    'sheet': 'calculo excel spreadsheet spreadsheets',
    'slides': 'presentacion presentaciones diapositivas powerpoint slides',
    'code': 'codigo',
    'archive': 'comprimido comprimidos',
}
KIND_WORDS = {k: set(v.split()) for k, v in KIND_WORDS.items()}
KIND_CATEGORIES = {'image': {'image'}, 'documents': {'document', 'pdf'}, 'video': {'video'}, 'audio': {'audio'},
                   'folder': {'folder'}, 'sheet': {'sheet'}, 'slides': {'slides'}, 'code': {'code'}, 'archive': {'archive'}}

EXT_WORDS = {
    'html': {'.html', '.htm'}, 'htm': {'.html', '.htm'}, 'web': {'.html', '.htm'}, 'pdf': {'.pdf'}, 'pdfs': {'.pdf'},
    'docx': {'.docx'}, 'word': {'.docx', '.doc', '.odt'}, 'odt': {'.odt'}, 'rtf': {'.rtf'},
    'md': {'.md', '.markdown'}, 'markdown': {'.md', '.markdown'}, 'txt': {'.txt', '.text'},
    'csv': {'.csv'}, 'xlsx': {'.xlsx'}, 'xls': {'.xls'}, 'ods': {'.ods'}, 'pptx': {'.pptx'}, 'ppt': {'.ppt'}, 'odp': {'.odp'},
    'png': {'.png'}, 'jpg': {'.jpg', '.jpeg'}, 'jpeg': {'.jpg', '.jpeg'}, 'gif': {'.gif'}, 'gifs': {'.gif'}, 'webp': {'.webp'},
    'svg': {'.svg'}, 'heic': {'.heic'}, 'tiff': {'.tif', '.tiff'}, 'mp3': {'.mp3'}, 'mp4': {'.mp4'}, 'mkv': {'.mkv'},
    'mov': {'.mov'}, 'wav': {'.wav'}, 'flac': {'.flac'}, 'ogg': {'.ogg'}, 'json': {'.json'}, 'epub': {'.epub'},
    'iso': {'.iso'}, 'rar': {'.rar'}, '7z': {'.7z'}, 'py': {'.py'}, 'ipynb': {'.ipynb'}, 'sql': {'.sql'},
}
EXT_CATEGORY_HINT = {'excel': {'.xlsx', '.xls', '.ods', '.csv'}, 'powerpoint': {'.pptx', '.ppt', '.odp'}}

MONTHS = {normalize(n): i for i, n in enumerate(
    'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre'.split(), 1)}
MONTHS |= {n.lower(): i for i, n in enumerate(calendar.month_name) if n and n not in {'May', 'March'}}
UNITS = {'dia': 1, 'dias': 1, 'day': 1, 'days': 1, 'semana': 7, 'semanas': 7, 'week': 7, 'weeks': 7,
         'mes': 30, 'meses': 30, 'month': 30, 'months': 30, 'ano': 365, 'anos': 365, 'year': 365, 'years': 365}
SIZE = {'kb': 1024, 'mb': 1024 ** 2, 'gb': 1024 ** 3}
LABEL = {'folder': 'Carpetas', 'document': 'Documentos', 'pdf': 'PDF', 'image': 'Imágenes', 'video': 'Vídeo',
         'audio': 'Audio', 'sheet': 'Hojas de cálculo', 'slides': 'Presentaciones', 'code': 'Código', 'archive': 'Comprimidos'}


def together(items):
    """«A», «A y B», «A, B y C»."""
    items = list(items)
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' y ' + items[-1]


def xdg(key, fallback):
    try:
        value = subprocess.check_output(['xdg-user-dir', key], text=True, timeout=2).strip()
        if value and Path(value) != Path.home(): return Path(value)
    except (OSError, subprocess.SubprocessError): pass
    return Path.home() / fallback


_places = None


def places():
    global _places
    if _places is None:
        home = Path.home()
        aliases = {'DOWNLOAD': ('Downloads', 'descargas downloads download descarga'),
                   'DOCUMENTS': ('Documents', 'documentos documents'),
                   'DESKTOP': ('Desktop', 'escritorio desktop'),
                   'PICTURES': ('Pictures', 'imagenes pictures fotos'),
                   'MUSIC': ('Music', 'musica music'), 'VIDEOS': ('Videos', 'videos')}
        _places = {'home': home}
        for key, (fallback, words) in aliases.items():
            folder = xdg(key, fallback)
            for word in words.split(): _places[word] = folder
    return _places


def stem(word):
    """Raíz para coincidir plurales por prefijo: imágenes→imagen, pirámides→piramid."""
    if any(c.isdigit() for c in word) or len(word) <= 4: return word
    if word.endswith('es') and len(word) > 5 and word[-3] not in 'aeiou': return word[:-2]
    if word.endswith('s'): return word[:-1]
    return word


@dataclass
class Intent:
    raw: str
    categories: set | None = None
    exts: set | None = None
    after: int | None = None          # ns, inclusive
    before: int | None = None         # ns, exclusive
    soft_after: int | None = None     # a year named in the phrase favours, never excludes
    soft_before: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    location: Path | None = None
    location_word: str | None = None
    recent: bool = False
    terms: list = field(default_factory=list)
    semantic: str = ''
    labels: list = field(default_factory=list)
    kind_word: str | None = None      # legacy single intent for result metadata

    @property
    def filtered(self):
        return any([self.categories, self.exts, self.after, self.before, self.min_size, self.max_size, self.location])


def day(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def ns(dt):
    return int(dt.timestamp() * 1e9)


def parse(query, now=None):
    now = now or datetime.now()
    intent = Intent(raw=query)
    # Per-character normalization keeps a map back to the original, accented phrase.
    pieces, owner = [], []
    for position, char in enumerate(query):
        for piece in normalize(char):
            pieces.append(piece); owner.append(position)
    text = ' ' + ''.join(pieces) + ' '
    original = text

    def cut(pattern, handler):
        nonlocal text
        def replace(match):
            if handler(match) is False: return match.group(0)
            return ' ' * len(match.group(0))
        text = re.sub(pattern, replace, text)

    def window(start, end, label):
        intent.after, intent.before = ns(start), ns(end) if end else None
        intent.labels.append(label)

    today = day(now)
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:hoy|today)\b', lambda m: window(today, None, 'hoy'))
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:anteayer|antes de ayer)\b', lambda m: window(today - timedelta(2), today - timedelta(1), 'anteayer'))
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:ayer|yesterday)\b', lambda m: window(today - timedelta(1), today, 'ayer'))
    monday = today - timedelta(today.weekday())
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:semana pasada|last week)\b', lambda m: window(monday - timedelta(7), monday, 'semana pasada'))
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:esta semana|this week)\b', lambda m: window(monday, None, 'esta semana'))
    first = today.replace(day=1)
    previous = (first - timedelta(1)).replace(day=1)
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:mes pasado|last month)\b', lambda m: window(previous, first, 'mes pasado'))
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:este mes|this month)\b', lambda m: window(first, None, 'este mes'))
    january = today.replace(month=1, day=1)
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:ano pasado|last year)\b', lambda m: window(january.replace(year=january.year - 1), january, 'año pasado'))
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:este ano|this year)\b', lambda m: window(january, None, 'este año'))

    def last(match):
        amount = int(match.group(1)) if match.group(1) else 1
        days = amount * UNITS[match.group(2)]
        window(today - timedelta(days - 1), None, f'últimos {amount} {match.group(2)}')
    cut(r'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:ultim[oa]s?|past|last)\s+(\d+)?\s*(dias?|semanas?|mes(?:es)?|anos?|days?|weeks?|months?|years?)\b', last)

    def month(match):
        number = MONTHS[match.group(1)]
        year = int(match.group(2)) if match.group(2) else (now.year if number <= now.month else now.year - 1)
        start = datetime(year, number, 1)
        end = datetime(year + (number == 12), number % 12 + 1, 1)
        window(start, end, f'{match.group(1)} {year}')
    names = '|'.join(MONTHS)
    cut(rf'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el\s+)?mes\s+de\s+)?({names})(?:\s+(?:de |del )?((?:19|20)\d\d))?\b', month)

    def year(match):
        value = int(match.group(1))
        intent.soft_after, intent.soft_before = ns(datetime(value, 1, 1)), ns(datetime(value + 1, 1, 1))
        return False  # the number stays a search term: «facturas 2024» also matches names
    cut(r'\b(?:de|del|en|from|in)\s+((?:19|20)\d\d)\b', year)

    def size(match, attr):
        value = float(match.group(1).replace(',', '.')) * SIZE[match.group(2)]
        setattr(intent, attr, int(value))
        intent.labels.append(('>' if attr == 'min_size' else '<') + f' {match.group(1)} {match.group(2).upper()}')
    cut(r'\b(?:de )?(?:mas de|mayores? (?:de|que)|over|larger than|bigger than)\s+(\d+(?:[.,]\d+)?)\s*(kb|mb|gb)\b', lambda m: size(m, 'min_size'))
    cut(r'\b(?:de )?(?:menos de|menores? (?:de|que)|under|smaller than)\s+(\d+(?:[.,]\d+)?)\s*(kb|mb|gb)\b', lambda m: size(m, 'max_size'))

    def recent(match):
        intent.recent = True
        intent.labels.append('recientes')
    cut(r'\b(?:mas )?(?:recientes?|recientemente|ultimos|ultimas|nuevos|nuevas|latest|recent|newest)\b', recent)

    def place(match):
        word = match.group(2)
        folder = places().get(word)
        if folder is None and not match.group(1): return False
        intent.location = folder
        intent.location_word = word
        intent.labels.append(f'en {folder.name if folder else word}')
    cut(r'\b(?:en|in|inside|dentro de|de)\s+(?:(?:mis?|my|el|la|los|las|the)\s+)?(carpeta|folder|directorio)\s+(?:(?:de|del|llamada|called)\s+)?([\w]+)\b', place)
    cut(r'\b(?:en|in|inside|dentro de|de)\s+(?:(?:mis?|my|el|la|los|las|the)\s+)?()([\w]+)\b(?=\s*$|\s+(?:de|del|con|que|sobre|en|y)\b)', place)

    for token in re.findall(r'(?<![\w.])\.([a-z0-9]{1,5})\b', text):
        intent.exts = (intent.exts or set()) | {'.' + token}
    cut(r'(?<![\w.])\.[a-z0-9]{1,5}\b', lambda m: None)

    content = []
    kinds = []
    for match in re.finditer(r'\w+', text):
        word = match.group(0)
        if word in EXT_WORDS:
            intent.exts = (intent.exts or set()) | EXT_WORDS[word]
            if word not in {'pdf', 'pdfs'}:  # «PDF sobre momias» keeps that word for the model
                text = text[:match.start()] + ' ' * len(word) + text[match.end():]
                continue
        if word in EXT_CATEGORY_HINT:
            intent.exts = (intent.exts or set()) | EXT_CATEGORY_HINT[word]
        kind = next((k for k, values in KIND_WORDS.items() if word in values), None)
        if kind:
            kinds.append(kind)
            if kind == 'folder': text = text[:match.start()] + ' ' * len(word) + text[match.end():]
            continue
        content.append(word)
    if kinds:
        intent.kind_word = kinds[0]
        if 'folder' in kinds:
            intent.categories = {'folder'}
        else:
            intent.categories = set().union(*(KIND_CATEGORIES[k] for k in kinds))
    if intent.exts:
        # «documento html»: the extension is stricter than the generic word.
        if intent.categories and intent.categories != {'folder'}:
            intent.categories = None
        names = sorted({'HTML' if e in {'.htm', '.html'} else 'JPG' if e in {'.jpg', '.jpeg'} else e[1:].upper() for e in intent.exts})
        intent.labels.insert(0, together(names))
        if intent.exts == {'.pdf'}: intent.kind_word = intent.kind_word or 'pdf'
    if intent.categories:
        intent.labels.insert(0, together([LABEL[c] for c in sorted(intent.categories)]))
    intent.terms = list(dict.fromkeys(w for w in content if w not in STOP and w not in EXT_WORDS
                                      and (len(w) >= 2 or w.isdigit())))
    blanked = {owner[i - 1] for i in range(1, len(original) - 1) if text[i] == ' ' and original[i] != ' '}
    semantic = ' '.join(''.join(c for i, c in enumerate(query) if i not in blanked).split())
    intent.semantic = semantic if intent.terms or kinds else ''
    return intent

