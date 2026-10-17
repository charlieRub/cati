"""Búsquedas guardadas: una consulta con sus filtros que se vuelve a ejecutar cada vez («carpeta inteligente»)."""
import json
import os
import threading
import time
import uuid

from .config import DATA

PATH = DATA / 'saved.json'


def load():
    try: items = json.loads(PATH.read_text())
    except (OSError, ValueError): return []
    return [item for item in items if isinstance(item, dict) and 'id' in item] if isinstance(items, list) else []


def write(items):
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = PATH.with_name(f'.{PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp')   # two writers never share it
    try:
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle:   # private from the start
            handle.write(json.dumps(items, ensure_ascii=False, indent=1))
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, PATH)
    finally:
        temporary.unlink(missing_ok=True)


def add(name, query, filters=None):
    items = load()
    entry = {'id': uuid.uuid4().hex[:8], 'name': name.strip() or query.strip() or 'Búsqueda', 'query': query.strip(),
             'filters': {k: v for k, v in (filters or {}).items() if v}, 'created': time.time()}
    items.append(entry)
    write(items)
    return entry


def remove(identifier):
    write([item for item in load() if item['id'] != identifier])


def suggest_name(query, filters):
    parts = [query.strip()] if query.strip() else []
    if filters.get('folder'): parts.append('en ' + os.path.basename(filters['folder'].rstrip('/')))
    if filters.get('kind'): parts.append(filters['kind'])
    if filters.get('period'): parts.append({'today': 'hoy', 'week': 'esta semana', 'month': 'este mes', 'year': 'este año'}.get(filters['period'], ''))
    if filters.get('size'): parts.append({'big': 'grandes', 'huge': 'enormes', 'small': 'pequeños'}.get(filters['size'], ''))
    text = ' '.join(p for p in parts if p).strip()
    return text[:1].upper() + text[1:] if text else 'Búsqueda'
