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


# «que creé» / «que modifiqué»: which date the phrase talks about. Default: either of them.
CREATED = set('''cree creaste creo_ creado creada creados creadas crearon hice hiciste hizo hecho hecha hechos hechas
descargue descargaste descargado descargada descargados descargadas baje bajaste bajado bajada bajados bajadas recibi
recibido recibida recibidos recibidas created made downloaded received'''.split()) - {'creo_'}
MODIFIED = set('''modifique modificaste modificado modificada modificados modificadas edite editaste editado editada editados
editadas cambie cambiaste cambiado cambiada cambiados cambiadas actualice actualizado actualizada actualizados actualizadas
toque tocado tocada tocados tocadas retoque retocado retocada modified edited changed updated touched'''.split())

STOP = set(normalize(w) for w in '''
a al algo alguna algun algún alguno algunos aqui aquí buscar busca buscame búscame buscando busco con como cómo de del el ella en
es esta este eso esa hay la las lo los me mi mis para por que qué quiero se si sobre su sus te tengo un una unas unos y o u
donde dónde dame dime enseñame enséñame muestrame muéstrame mostrar ver necesito encuentra encuentrame encuéntrame encontrar
habla hablan hable hablen trata tratan trate traten sale salen aparece aparecen contiene contienen contenga contengan tiene
tienen tenga tengan relacionado relacionada relacionados relacionadas acerca cosa cosas archivo archivos fichero ficheros
todo todos toda todas mio mia mios mias mío mía míos mías tu tus ese esos esas aquel cual cuál cuales donde guardado guardada
guardados guardadas hice hizo era eran fue sea sean ser estar esta están estan creo creado creada creados creadas
dia día fecha no ni sean sea excepto salvo
the a an of in on for to about find where is are my i want please show me some any with that which file files all
''' .split()) | CREATED | MODIFIED

KIND_WORDS = {
    'image': 'foto fotos fotografia fotografias imagen imagenes photo photos picture pictures image images captura capturas '
             'screenshot screenshots pantallazo pantallazos',
    'documents': 'documento documentos document documents doc docs texto textos',
    'video': 'video videos clip clips pelicula peliculas movie movies grabacion grabaciones',
    'audio': 'audio audios sonido sonidos voz podcast podcasts cancion canciones song songs musica music',
    'folder': 'carpeta carpetas directorio directorios folder folders directory directories',
    'sheet': 'calculo excel excels spreadsheet spreadsheets hoja hojas',
    'slides': 'presentacion presentaciones diapositivas powerpoint powerpoints slides',
    'code': 'codigo',
    'archive': 'comprimido comprimidos',
}
KIND_WORDS = {k: set(v.split()) for k, v in KIND_WORDS.items()}
KIND_CATEGORIES = {'image': {'image'}, 'documents': {'document', 'pdf'}, 'video': {'video'}, 'audio': {'audio'},
                   'folder': {'folder'}, 'sheet': {'sheet'}, 'slides': {'slides'}, 'code': {'code'}, 'archive': {'archive'}}

# A bare word that names a format. Words that are also ordinary words («iso», «raw», «log») only
# count written as an extension: «.iso», «extensión iso», «formato raw».
EXT_WORDS = {
    'html': {'.html', '.htm'}, 'htm': {'.html', '.htm'}, 'web': {'.html', '.htm'}, 'pdf': {'.pdf'},
    'docx': {'.docx'}, 'word': {'.docx', '.doc', '.odt'}, 'odt': {'.odt'}, 'rtf': {'.rtf'},
    'md': {'.md', '.markdown'}, 'markdown': {'.md', '.markdown'}, 'txt': {'.txt', '.text'},
    'csv': {'.csv'}, 'tsv': {'.tsv'}, 'xlsx': {'.xlsx'}, 'xls': {'.xls'}, 'ods': {'.ods'}, 'pptx': {'.pptx'}, 'ppt': {'.ppt'},
    'odp': {'.odp'}, 'png': {'.png'}, 'jpg': {'.jpg', '.jpeg'}, 'jpeg': {'.jpg', '.jpeg'}, 'gif': {'.gif'}, 'webp': {'.webp'},
    'svg': {'.svg'}, 'heic': {'.heic', '.heif'}, 'tiff': {'.tif', '.tiff'}, 'tif': {'.tif', '.tiff'}, 'bmp': {'.bmp'},
    'avif': {'.avif'}, 'psd': {'.psd'}, 'xcf': {'.xcf'}, 'eps': {'.eps'}, 'dng': {'.dng'}, 'cr2': {'.cr2'}, 'nef': {'.nef'},
    'mp3': {'.mp3'}, 'mp4': {'.mp4'}, 'mkv': {'.mkv'}, 'mov': {'.mov'}, 'avi': {'.avi'}, 'webm': {'.webm'}, 'm4v': {'.m4v'},
    'wav': {'.wav'}, 'flac': {'.flac'}, 'ogg': {'.ogg'}, 'opus': {'.opus'}, 'm4a': {'.m4a'}, 'aac': {'.aac'},
    'json': {'.json'}, 'xml': {'.xml'}, 'yaml': {'.yaml', '.yml'}, 'yml': {'.yaml', '.yml'}, 'toml': {'.toml'},
    'epub': {'.epub'}, 'mobi': {'.mobi'}, 'zip': {'.zip'}, 'rar': {'.rar'}, '7z': {'.7z'}, 'tgz': {'.tgz'},
    'apk': {'.apk'}, 'deb': {'.deb'}, 'rpm': {'.rpm'}, 'appimage': {'.appimage'}, 'exe': {'.exe'}, 'msi': {'.msi'},
    'dmg': {'.dmg'}, 'torrent': {'.torrent'}, 'srt': {'.srt'}, 'vtt': {'.vtt'}, 'ics': {'.ics'}, 'vcf': {'.vcf'},
    'eml': {'.eml'}, 'kml': {'.kml'}, 'kmz': {'.kmz'}, 'gpx': {'.gpx'}, 'stl': {'.stl'}, 'blend': {'.blend'},
    'dwg': {'.dwg'}, 'dxf': {'.dxf'}, 'gcode': {'.gcode'}, 'ttf': {'.ttf'}, 'otf': {'.otf'}, 'woff': {'.woff', '.woff2'},
    'py': {'.py'}, 'ipynb': {'.ipynb'}, 'sql': {'.sql'}, 'sh': {'.sh'}, 'js': {'.js'}, 'ts': {'.ts'}, 'tsx': {'.tsx'},
    'jsx': {'.jsx'}, 'java': {'.java'}, 'kt': {'.kt'}, 'rs': {'.rs'}, 'cpp': {'.cpp'}, 'css': {'.css'}, 'scss': {'.scss'},
    'sqlite': {'.sqlite', '.sqlite3', '.db'}, 'parquet': {'.parquet'}, 'feather': {'.feather'}, 'tex': {'.tex'},
}
EXPLICIT_ONLY = {'iso', 'raw', 'log', 'obj', 'ini', 'cfg', 'db', 'gz', 'tar', 'c', 'h', 'go', 'ai', 'key', 'pages', 'numbers', 'ink'}
EXT_CATEGORY_HINT = {'excel': {'.xlsx', '.xls', '.ods', '.csv'}, 'excels': {'.xlsx', '.xls', '.ods', '.csv'},
                     'powerpoint': {'.pptx', '.ppt', '.odp'}, 'powerpoints': {'.pptx', '.ppt', '.odp'}}

MONTHS = {normalize(n): i for i, n in enumerate(
    'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre'.split(), 1)}
MONTHS['setiembre'] = 9
MONTHS |= {n.lower(): i for i, n in enumerate(calendar.month_name) if n and n not in {'May', 'March'}}
MONTH_NAMES = 'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre'.split()
WEEKDAYS = {'lunes': 0, 'martes': 1, 'miercoles': 2, 'jueves': 3, 'viernes': 4, 'sabado': 5, 'domingo': 6,
            'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6}
NUMBERS = {'un': 1, 'una': 1, 'uno': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8,
           'nueve': 9, 'diez': 10, 'once': 11, 'doce': 12, 'quince': 15, 'veinte': 20, 'treinta': 30,
           'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'ten': 10}
UNITS = {'dia': 1, 'dias': 1, 'day': 1, 'days': 1, 'semana': 7, 'semanas': 7, 'week': 7, 'weeks': 7,
         'mes': 30, 'meses': 30, 'month': 30, 'months': 30, 'ano': 365, 'anos': 365, 'year': 365, 'years': 365}
SIZE = {'kb': 1024, 'mb': 1024 ** 2, 'gb': 1024 ** 3}
LABEL = {'folder': 'Carpetas', 'document': 'Documentos', 'pdf': 'PDF', 'image': 'Imágenes', 'video': 'Vídeo',
         'audio': 'Audio', 'sheet': 'Hojas de cálculo', 'slides': 'Presentaciones', 'code': 'Código', 'archive': 'Comprimidos'}

# Words the phrase parser understands: the spelling corrector treats them as known and prefers them.
VOCABULARY = (set().union(*KIND_WORDS.values()) | set(EXT_WORDS) | set(MONTHS) | set(WEEKDAYS) | set(NUMBERS)
              | set(UNITS) | CREATED | MODIFIED | set('''hoy ayer anteayer anoche semana pasada pasado mes ano este esta
              ultimos ultimas ultimo ultima recientes reciente nuevos nuevas hace entre desde hasta antes despues partir
              manana tarde noche fin mas menos mayores menores mayor menor sin excepto salvo extension formato tipo
              descargas documentos escritorio imagenes musica videos carpeta'''.split()))


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
    soft_after: int | None = None     # a month or year next to a topic favours, never excludes
    soft_before: int | None = None
    date_field: str = 'any'           # 'created' when the phrase says so; otherwise the modification date
    date_words: set = field(default_factory=set)   # «agosto» in «rentabilidad agosto.xlsx» still counts
    hints: set = field(default_factory=set)        # words the parser consumed that may also be in a name
    min_size: int | None = None
    max_size: int | None = None
    location: Path | None = None
    location_word: str | None = None
    excluded_exts: set | None = None
    excluded_categories: set | None = None
    excluded_terms: list = field(default_factory=list)   # «que no sean de egipto»
    recent: bool = False
    terms: list = field(default_factory=list)
    semantic: str = ''
    labels: list = field(default_factory=list)
    kind_word: str | None = None      # legacy single intent for result metadata
    corrected: str | None = None      # the phrase after fixing typos, when it changed
    original: str | None = None

    @property
    def filtered(self):
        return any([self.categories, self.exts, self.after, self.before, self.min_size, self.max_size, self.location,
                    self.excluded_exts, self.excluded_categories])


def day(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def ns(dt):
    return int(dt.timestamp() * 1e9)


def spanish(dt, year=True):
    return f'{dt.day} {MONTH_NAMES[dt.month - 1][:3]}' + (f' {dt.year}' if year else '')


MONTH_RE = '|'.join(sorted(MONTHS, key=len, reverse=True))
WEEKDAY_RE = '|'.join(WEEKDAYS)
NUMBER_RE = r'\d+|' + '|'.join(sorted(NUMBERS, key=len, reverse=True))
# One calendar expression: «3 de marzo de 2024», «3/3/24», «2024-03-03», «marzo de 2024», «2024».
DATE_RE = (rf'(?:\d{{1,2}}\s+(?:de\s+)?(?:{MONTH_RE})(?:\s+(?:de|del)?\s*(?:19|20)\d\d)?'
           rf'|(?:{MONTH_RE})\s+\d{{1,2}}(?:\s*,?\s*(?:19|20)\d\d)?'
           r'|\d{1,2}/\d{1,2}(?:/(?:\d{4}|\d{2}))?|\d{1,2}[.-]\d{1,2}[.-](?:\d{4}|\d{2})|(?:19|20)\d\d-\d{1,2}-\d{1,2}'
           rf'|(?:{MONTH_RE})(?:\s+(?:de|del)?\s*(?:19|20)\d\d)?|(?:19|20)\d\d)')


def number(word):
    return int(word) if word.isdigit() else NUMBERS[word]


def calendar_span(text, now):
    """(inicio, fin, etiqueta, palabra) de una expresión de DATE_RE; el año que falta es el último ya vivido."""
    text = ' '.join(text.split())

    def guess(month, day_=1):
        year = now.year
        if (month, day_) > (now.month, now.day): year -= 1
        return year

    def one_day(year, month, day_):
        try: start = datetime(year, month, day_)
        except ValueError: return None
        return start, start + timedelta(1), spanish(start), None

    m = re.fullmatch(rf'(\d{{1,2}})\s+(?:de\s+)?({MONTH_RE})(?:\s+(?:de|del)?\s*((?:19|20)\d\d))?', text) or \
        re.fullmatch(rf'({MONTH_RE})\s+(\d{{1,2}})(?:\s*,?\s*((?:19|20)\d\d))?', text)
    if m:
        a, b, year = m.groups()
        day_, month = (int(a), MONTHS[b]) if a.isdigit() else (int(b), MONTHS[a])
        return one_day(int(year) if year else guess(month, day_), month, day_)
    m = re.fullmatch(r'((?:19|20)\d\d)-(\d{1,2})-(\d{1,2})', text)
    if m: return one_day(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r'(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{4}|\d{2}))?', text)
    if m:
        day_, month = int(m[1]), int(m[2])
        if m[3]: year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        else: year = guess(month, day_) if 1 <= month <= 12 else now.year
        return one_day(year, month, day_)
    m = re.fullmatch(rf'({MONTH_RE})(?:\s+(?:de|del)?\s*((?:19|20)\d\d))?', text)
    if m:
        month = MONTHS[m[1]]
        year = int(m[2]) if m[2] else guess(month)
        start = datetime(year, month, 1)
        return start, datetime(year + (month == 12), month % 12 + 1, 1), f'{MONTH_NAMES[month - 1]} {year}', m[1]
    m = re.fullmatch(r'((?:19|20)\d\d)', text)
    if m:
        year = int(m[1])
        return datetime(year, 1, 1), datetime(year + 1, 1, 1), str(year), m[1]
    return None


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
    words = set(re.findall(r'\w+', text))
    if words & CREATED: intent.date_field = 'created'
    elif words & MODIFIED: intent.date_field = 'modified'
    verb = {'created': 'creado ', 'modified': 'modificado '}.get(intent.date_field, '')

    def cut(pattern, handler):
        nonlocal text
        def replace(match):
            if handler(match) is False: return match.group(0)
            return ' ' * len(match.group(0))
        text = re.sub(pattern, replace, text)

    single_day = []  # a one-day window can be narrowed by «a las 14» or «por la tarde»

    def window(start, end, label, hard=True, word=None):
        if word: intent.date_words.add(word); intent.hints.add(word)
        if hard:
            intent.after, intent.before = ns(start), ns(end) if end else None
            single_day[:] = [start] if end and end - start == timedelta(1) else []
            label = verb + label
        else:
            intent.soft_after, intent.soft_before = ns(start), ns(end)
        intent.labels.append(label)

    today = day(now)
    lead = r'\b(?:(?:de|del|en|desde|durante|on|in|from)\s+)?(?:(?:el|la|los|las|the)\s+)?'
    article = r'(?:(?:el|la|los|las|the|dia)\s+)*'
    span = lambda m, i=1: calendar_span(m.group(i), now)

    # Ranges first, so «entre marzo y junio» is not read as two separate months.
    def between(m):
        a, b = span(m, 1), span(m, 2)
        if not a or not b: return False
        year = re.search(r'(?:19|20)\d\d', m.group(2))
        if year and not re.search(r'(?:19|20)\d\d|/\d+/', m.group(1)):
            # «entre marzo y junio de 2025»: the year said once applies to both ends.
            a = calendar_span(m.group(1) + ' ' + year.group(0), now) or a
        window(a[0], b[1], f'{a[2]} – {b[2]}')
    cut(rf'\bentre\s+{article}({DATE_RE})\s+y\s+{article}({DATE_RE})\b', between)
    cut(rf'\bbetween\s+({DATE_RE})\s+and\s+({DATE_RE})\b', between)

    def since(m):
        value = span(m, 2)
        if not value: return False
        start = value[1] if m.group(1) in {'despues de', 'posterior a', 'posteriores a', 'after'} else value[0]
        intent.after = ns(start)
        intent.labels.append(f'{verb}desde {value[2]}')
    cut(rf'\b(desde|a partir de|despues de|posterior(?:es)? a|since|after)\s+{article}({DATE_RE})\b', since)

    def until(m):
        value = span(m, 2)
        if not value: return False
        intent.before = ns(value[1] if m.group(1) in {'hasta', 'until'} else value[0])
        intent.labels.append(verb + (f'antes de {value[2]}' if m.group(1) not in {'hasta', 'until'} else f'hasta {value[2]}'))
    cut(rf'\b(antes de|anterior(?:es)? a|hasta|before|until)\s+{article}({DATE_RE})\b', until)

    cut(lead + r'(?:hoy|today)\b', lambda m: window(today, None, 'hoy'))
    cut(lead + r'(?:anteayer|antes de ayer)\b', lambda m: window(today - timedelta(2), today - timedelta(1), 'anteayer'))
    cut(r'\b(?:de\s+)?anoche\b|\blast night\b', lambda m: window(today - timedelta(hours=6), today + timedelta(hours=6), 'anoche'))
    cut(lead + r'(?:ayer|yesterday)\b', lambda m: window(today - timedelta(1), today, 'ayer'))
    monday = today - timedelta(today.weekday())

    def weekend(m):
        saturday = today - timedelta((today.weekday() - 5) % 7)
        if saturday > today or (m.group(1) and today.weekday() >= 5): saturday -= timedelta(7)
        window(saturday, saturday + timedelta(2), 'fin de semana')
    cut(lead + r'(?:este\s+|ultimo\s+)?fin de semana(\s+pasado)?\b|\b(?:last\s+)?weekend\b', weekend)
    cut(lead + r'(?:semana pasada|last week)\b', lambda m: window(monday - timedelta(7), monday, 'semana pasada'))
    cut(lead + r'(?:esta semana|this week)\b', lambda m: window(monday, None, 'esta semana'))
    first = today.replace(day=1)
    previous = (first - timedelta(1)).replace(day=1)
    cut(lead + r'(?:mes pasado|last month)\b', lambda m: window(previous, first, 'mes pasado'))
    cut(lead + r'(?:este mes|this month)\b', lambda m: window(first, None, 'este mes'))
    january = today.replace(month=1, day=1)
    cut(lead + r'(?:ano pasado|last year)\b', lambda m: window(january.replace(year=january.year - 1), january, 'año pasado'))
    cut(lead + r'(?:este ano|this year)\b', lambda m: window(january, None, 'este año'))

    def part_of_today(m):
        name = m.group(1) or m.group(2)
        hours = {'manana': (6, 13), 'morning': (6, 13), 'tarde': (13, 21), 'afternoon': (13, 21), 'noche': (19, 24),
                 'evening': (19, 24), 'tonight': (19, 24)}[name]
        window(today + timedelta(hours=hours[0]), today + timedelta(hours=hours[1]), f'esta {name}')
    cut(r'\b(?:de\s+)?(?:esta|this)\s+(manana|tarde|noche|morning|afternoon|evening)\b|\b(tonight)\b', part_of_today)

    def last(match):
        amount = number(match.group(1)) if match.group(1) else 1
        days = amount * UNITS[match.group(2)]
        window(today - timedelta(days - 1), None, f'últimos {amount} {match.group(2)}')
    cut(rf'\b(?:(?:de|del|en|desde)\s+)?(?:(?:el|la|los|las)\s+)?(?:ultim[oa]s?|past|last)\s+({NUMBER_RE})?\s*'
        r'(dias?|semanas?|mes(?:es)?|anos?|days?|weeks?|months?|years?)\b', last)

    def ago(match):
        amount = number(match.group(1) or match.group(3))
        unit = match.group(2) or match.group(4)
        if unit.startswith(('hora', 'hour')):
            end = now - timedelta(hours=amount - 1)
            window(end - timedelta(hours=2), end, f'hace {amount} h')
            return
        unit = {'day': 'dia', 'week': 'semana', 'month': 'mes', 'year': 'ano'}.get(unit.rstrip('s'), unit)
        slack = {1: 0, 7: 3, 30: 10, 365: 45}[UNITS[unit]]  # «hace un mes» rarely means that exact day
        center = today - timedelta(amount * UNITS[unit])
        window(center - timedelta(slack), center + timedelta(slack + 1), f'hace {amount} {unit}')
    cut(rf'\bhace\s+({NUMBER_RE})\s+(horas?|dias?|semanas?|mes(?:es)?|anos?)\b'
        rf'|\b({NUMBER_RE})\s+(hours?|days?|weeks?|months?|years?)\s+ago\b', ago)

    def recently(match):
        intent.recent = True
        window(now - timedelta(hours=6) if 'rato' in match.group(0) else today - timedelta(6), None, 'hace poco')
    cut(r'\bhace\s+(?:un\s+)?(?:poco|rato|ratito)\b|\brecently\b', recently)

    def weekday(match):
        name = match.group(2)
        back = (today.weekday() - WEEKDAYS[name]) % 7
        if back == 0 and not match.group(1): back = 7  # «el lunes» said on a Monday is the previous one
        start = today - timedelta(back)
        window(start, start + timedelta(1), f'{name} {spanish(start, False)}', word=name)
    cut(rf'\b(?:(?:de|del|el|on|last)\s+)?(?:(este|this)\s+)?(?:pasado\s+)?({WEEKDAY_RE})(?:\s+pasado)?\b', weekday)

    def exact(match):
        value = calendar_span(match.group(1), now)
        if not value: return False
        start, end, label, word = value
        explicit = intent.date_field != 'any'
        if word and not re.fullmatch(r'\d+', word):
            # A month next to a topic («rentabilidad de agosto») favours; alone («fotos de agosto») filters.
            window(start, end, label, hard=explicit, word=word)
            if not explicit: intent.labels.pop()
            pending.append((start, end, label, word))
            return
        if word:  # a bare year stays a word too: «facturas 2024» also matches names
            if not re.search(r'\b(?:ano|year|de|del|en|from|in)\s+$', text[:match.start(1)]): return False
            pending.append((datetime(int(word), 1, 1), datetime(int(word) + 1, 1, 1), label, word))
            intent.soft_after, intent.soft_before = ns(pending[-1][0]), ns(pending[-1][1])
            intent.date_words.add(word)
            return False
        window(start, end, label)
    pending = []
    cut(rf'\b(?:(?:de|del|en|el|on|in|from|durante|ano|year)\s+)*(?:(?:el|la|dia)\s+)*({DATE_RE})\b', exact)

    def hour(match):
        if not single_day: return
        if match.group(1):
            start = single_day[0] + timedelta(hours=int(match.group(1)), minutes=int(match.group(2) or 0))
            length = timedelta(minutes=10) if match.group(2) else timedelta(hours=1)
            if start.hour >= 24 or int(match.group(1)) > 23: return
            intent.after, intent.before = ns(start), ns(start + length)
            intent.labels[-1] += f' {start:%H:%M}'
        else:
            hours = {'manana': (6, 13), 'tarde': (13, 21), 'noche': (19, 24)}[match.group(3)]
            intent.after, intent.before = ns(single_day[0] + timedelta(hours=hours[0])), ns(single_day[0] + timedelta(hours=hours[1]))
            intent.labels[-1] += f' por la {match.group(3)}'
    cut(r'\b(?:a|sobre)\s+las?\s+(\d{1,2})(?:[:.h](\d\d))?\s*(?:h|horas)?\b|\bpor\s+la\s+(manana|tarde|noche)\b', hour)

    def size(match, attr):
        value = float(match.group(1).replace(',', '.')) * SIZE[match.group(2)]
        setattr(intent, attr, int(value))
        intent.labels.append(('>' if attr == 'min_size' else '<') + f' {match.group(1)} {match.group(2).upper()}')
    cut(r'\b(?:de )?(?:mas de|mayores? (?:de|que)|over|larger than|bigger than)\s+(\d+(?:[.,]\d+)?)\s*(kb|mb|gb)\b', lambda m: size(m, 'min_size'))
    cut(r'\b(?:de )?(?:menos de|menores? (?:de|que)|under|smaller than)\s+(\d+(?:[.,]\d+)?)\s*(kb|mb|gb)\b', lambda m: size(m, 'max_size'))

    def about(match):
        value = float(match.group(1).replace(',', '.')) * SIZE[match.group(2)]
        intent.min_size, intent.max_size = int(value * .75), int(value * 1.3)
        intent.labels.append(f'≈ {match.group(1)} {match.group(2).upper()}')
    cut(r'\b(?:de\s+)?(?:unos?\s+|aprox(?:imadamente)?\s+|about\s+|around\s+|~\s*)?(\d+(?:[.,]\d+)?)\s*(kb|mb|gb)\b', about)

    def recent(match):
        intent.recent = True
        intent.hints.add(match.group(0).split()[-1])
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

    kinds_said = []

    def extension(word, explicit=False):
        word = word.lstrip('.')
        if word in EXT_WORDS and (explicit or word not in EXPLICIT_ONLY): return EXT_WORDS[word]
        if word.endswith('s') and word[:-1] in EXT_WORDS and len(word) > 3 and word[:-1] not in EXPLICIT_ONLY:
            return EXT_WORDS[word[:-1]]  # «pdfs», «pngs», «mp3s»
        if explicit and re.fullmatch(r'[a-z0-9]{1,10}', word): return {'.' + word}
        return None

    def kind_of_word(word):
        return next((k for k, values in KIND_WORDS.items() if word in values), None)

    def exclude(match):
        word = match.group(1)
        exts = extension(word, explicit=match.group(0).count('.') > 0)
        kind = kind_of_word(word)
        if exts:
            intent.excluded_exts = (intent.excluded_exts or set()) | exts
            intent.labels.append(f'sin {word.upper()}')
        elif kind:
            intent.excluded_categories = (intent.excluded_categories or set()) | KIND_CATEGORIES[kind]
            intent.labels.append(f'sin {word}')
        elif match.group(0).lstrip().startswith(('sin ', 'no ', 'not ', 'ni ', 'without ')) and word in STOP:
            return False
        elif len(word) >= 3 and word not in STOP:
            intent.excluded_terms.append(word)
            intent.labels.append(f'sin «{word}»')
        else: return False
    cut(r'\b(?:sin|excepto|menos|salvo|que no sean?|ni|except|without|not|no)\s+(?:(?:los|las|el|la|ningun|ninguna|en|de|del)\s+)?\.?(\w+)\b', exclude)
    # «captura de pantalla» names one kind of image, not a screen.
    cut(r'\b(?:capturas?|pantallazos?)\s+de\s+pantalla\b', lambda m: (intent.hints.update({'captura', 'screenshot'}), kinds_said.append('image')) and None)

    def explicit(match):
        exts = extension(match.group(1), explicit=True)
        if not exts: return False
        intent.exts = (intent.exts or set()) | exts
        intent.hints.add(match.group(1))
    cut(r'\b(?:con\s+)?(?:extension(?:es)?|ext|terminad[oa]s?\s+en|acabad[oa]s?\s+en)\s*[:=]?\s*(?:de\s+)?\.?([a-z0-9]{1,10})\b', explicit)
    cut(r'\b(?:formato|tipo|format|type)\s+(?:de\s+)?\.?([a-z0-9]{1,10})\b',
        lambda m: explicit(m) if m.group(1) in EXT_WORDS or m.group(1) in EXPLICIT_ONLY else False)
    cut(r'(?<![\w.])\*?\.([a-z0-9]{1,10})\b', explicit)

    content = []
    kinds = list(kinds_said)
    for match in re.finditer(r'\w+', text):
        word = match.group(0)
        exts = extension(word)
        if exts:
            intent.exts = (intent.exts or set()) | exts
            intent.hints.add(word)
            if word not in {'pdf', 'pdfs'}:  # «PDF sobre momias» keeps that word for the model
                text = text[:match.start()] + ' ' * len(word) + text[match.end():]
                continue
        if word in EXT_CATEGORY_HINT:
            intent.exts = (intent.exts or set()) | EXT_CATEGORY_HINT[word]
        kind = kind_of_word(word)
        if kind:
            kinds.append(kind)
            intent.hints.add(word)
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
    intent.terms = list(dict.fromkeys(w for w in content if w not in STOP and not extension(w)
                                      and (len(w) >= 2 or w.isdigit())))
    if pending and intent.after is None and intent.before is None:
        start, end, label, word = pending[-1]
        topic = [t for t in intent.terms if t != word]
        if not topic or intent.date_field != 'any':
            # Nothing else to look for («fotos de 2023») or a date verb: the period filters.
            intent.after, intent.before = ns(start), ns(end)
            intent.soft_after = intent.soft_before = None
            if word in intent.terms: intent.terms.remove(word)
            label = verb + label
        else:
            intent.soft_after, intent.soft_before = ns(start), ns(end)
            label = f'≈ {label}'
        intent.labels.append(label)
    blanked = {owner[i - 1] for i in range(1, len(original) - 1) if text[i] == ' ' and original[i] != ' '}
    semantic = ' '.join(''.join(c for i, c in enumerate(query) if i not in blanked).split())
    intent.semantic = semantic if intent.terms or kinds else ''
    return intent
