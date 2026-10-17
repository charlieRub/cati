"""Qué abres y con qué frecuencia: alimenta «Recientes», «Más usados» y un pequeño empuje al ordenar."""
import time
from pathlib import Path

from .store import opened


def record(path, db_path=None):
    with opened(db_path) as db:
        db.execute('INSERT INTO usage(path,opens,last) VALUES(?,1,?) ON CONFLICT(path) DO UPDATE SET opens=opens+1,last=excluded.last',
                   (str(path), time.time_ns()))


def moved(old, new, db_path=None):
    """Conserva el historial cuando un archivo (o una carpeta entera) cambia de sitio."""
    old, new = str(old), str(new)
    with opened(db_path) as db:
        rows = db.execute('SELECT path,opens,last FROM usage WHERE path=? OR (path>=? AND path<?)', (old, old + '/', old + '0')).fetchall()
        for row in rows:
            target = new + row['path'][len(old):]
            db.execute('DELETE FROM usage WHERE path=?', (row['path'],))
            db.execute('INSERT INTO usage(path,opens,last) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET opens=opens+excluded.opens,last=max(last,excluded.last)',
                       (target, row['opens'], row['last']))


def forget(path, db_path=None):
    with opened(db_path) as db:
        db.execute('DELETE FROM usage WHERE path=? OR (path>=? AND path<?)', (str(path), str(path) + '/', str(path) + '0'))


def rows(order, limit, db_path=None):
    """Los `limit` más recientes o más abiertos que siguen existiendo."""
    column = 'last DESC' if order == 'recent' else 'opens DESC, last DESC'
    found = []
    with opened(db_path) as db:
        for row in db.execute(f'SELECT path,opens,last FROM usage ORDER BY {column} LIMIT ?', (limit * 3,)):
            if Path(row['path']).exists(): found.append((row['path'], row['opens'], row['last']))
            if len(found) >= limit: break
    return found


_cache = {'at': 0.0, 'counts': {}}


def counts(db):
    """Aperturas por ruta, cacheadas unos segundos: se consulta en cada búsqueda."""
    if time.monotonic() - _cache['at'] > 5:
        _cache['counts'] = {row[0]: row[1] for row in db.execute('SELECT path,opens FROM usage')}
        _cache['at'] = time.monotonic()
    return _cache['counts']
