"""Duplicados exactos y qué ocupa el espacio, a partir del catálogo y comprobando el disco."""
import hashlib
import os
import stat
from collections import defaultdict
from pathlib import Path

from .store import connect

PARTIAL = 64 * 1024


def digest(path, full=False, size=None):
    """Huella de un archivo: extremos (rápida) o contenido completo."""
    hasher = hashlib.blake2b(digest_size=16)
    size = os.path.getsize(path) if size is None else size
    with open(path, 'rb') as handle:
        if full or size <= 3 * PARTIAL:
            while block := handle.read(1 << 20): hasher.update(block)
        else:
            hasher.update(handle.read(PARTIAL))
            handle.seek(size // 2); hasher.update(handle.read(PARTIAL))
            handle.seek(size - PARTIAL); hasher.update(handle.read(PARTIAL))
    return hasher.digest()


def duplicates(min_size=4096, folder=None, db_path=None, stop=None, progress=None):
    """Grupos de archivos con idéntico contenido: [{'size', 'paths'}] ordenados por espacio recuperable.

    Mismo tamaño → huella parcial → comparación completa, así que casi nunca se lee un archivo entero
    sin necesidad (y los pequeños, cuya huella parcial ya es completa, se leen una sola vez). Los enlaces
    duros (mismo inodo) no cuentan como copias: no ocupan espacio extra. Solo archivos normales (nunca
    enlaces simbólicos), y un archivo que cambia mientras se compara se descarta."""
    db = connect(db_path)
    try:
        sql, values = 'SELECT path,size FROM catalog WHERE is_dir=0 AND size>=?', [min_size]
        if folder:
            prefix = str(folder).rstrip('/') + '/'
            sql += ' AND path>=? AND path<?'
            values += [prefix, prefix[:-1] + '0']
        by_size = defaultdict(list)
        for row in db.execute(sql, values): by_size[row['size']].append(row['path'])
    finally:
        db.close()
    candidates = [(size, paths) for size, paths in by_size.items() if len(paths) > 1]
    groups, done = [], 0
    for size, paths in sorted(candidates, key=lambda c: -c[0] * (len(c[1]) - 1)):
        if stop and stop(): break
        done += 1
        if progress and done % 20 == 0: progress(done, len(candidates))
        found, seen = {}, set()
        for path in paths:
            try: info = os.lstat(path)
            except OSError: continue
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) in seen or info.st_size != size: continue
            seen.add((info.st_dev, info.st_ino))
            found[path] = info
        if len(found) < 2: continue
        partial = defaultdict(list)
        for path in found:
            try: partial[digest(path, size=size)].append(path)
            except OSError: pass
        whole = size <= 3 * PARTIAL   # the partial print already covered every byte
        for same in partial.values():
            if len(same) < 2: continue
            full = defaultdict(list)
            if whole: full[None] = same
            else:
                for path in same:
                    try: full[digest(path, True, size)].append(path)
                    except OSError: pass
            for identical in full.values():
                identical = [p for p in identical if unchanged(p, found[p])]
                if len(identical) > 1:
                    groups.append({'size': size, 'paths': sorted(identical, key=lambda p: (found[p].st_mtime_ns, len(p), p))})
    groups.sort(key=lambda g: -g['size'] * (len(g['paths']) - 1))
    return groups


def unchanged(path, before):
    """¿Sigue siendo el mismo archivo, sin tocar, que se midió antes de leerlo?"""
    try: now = os.lstat(path)
    except OSError: return False
    return (now.st_ino, now.st_size, now.st_mtime_ns, now.st_ctime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)


def still_identical(paths):
    """Comprobación justo antes de borrar copias: ¿siguen siendo todas byte a byte iguales a la primera?"""
    try:
        sizes = {os.path.getsize(p) for p in paths}
        return len(sizes) == 1 and len({digest(p, True) for p in paths}) == 1
    except OSError: return False


def reclaimable(groups):
    return sum(g['size'] * (len(g['paths']) - 1) for g in groups)


def spare_copies(groups):
    """Qué sobra de cada grupo: se conserva el más antiguo (el original) y se marca el resto."""
    return [path for group in groups for path in group['paths'][1:]]


def usage(folder, db_path=None, hidden=False):
    """Lo que ocupa cada elemento directo de `folder`: ([(ruta, es_carpeta, bytes, archivos)], total).

    Usa el catálogo (instantáneo) y cae al disco para carpetas que el índice no recorre; en el disco no sigue
    enlaces simbólicos ni cuenta dos veces un archivo con varios enlaces duros."""
    folder = Path(folder)
    prefix = str(folder).rstrip('/') + '/'
    sizes, counts, kinds = defaultdict(int), defaultdict(int), {}
    db = connect(db_path)
    try:
        for row in db.execute('SELECT path,size FROM catalog WHERE is_dir=0 AND path>=? AND path<?', (prefix, prefix[:-1] + '0')):
            child, _, rest = row['path'][len(prefix):].partition('/')
            if child.startswith('.') and not hidden: continue
            sizes[child] += row['size']; counts[child] += 1; kinds[child] = bool(rest)
    finally:
        db.close()
    if not sizes:
        linked = set()

        def measure(info):
            if info.st_nlink > 1:
                if (info.st_dev, info.st_ino) in linked: return 0
                linked.add((info.st_dev, info.st_ino))
            return info.st_size
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith('.') and not hidden: continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        kinds[entry.name] = True; sizes[entry.name] += 0
                        stack = [entry.path]
                        while stack:
                            try: inner = os.scandir(stack.pop())
                            except OSError: continue
                            with inner:
                                for item in inner:
                                    try:
                                        if item.is_dir(follow_symlinks=False): stack.append(item.path); continue
                                        sizes[entry.name] += measure(item.stat(follow_symlinks=False)); counts[entry.name] += 1
                                    except OSError: pass
                    else:
                        sizes[entry.name] = measure(entry.stat(follow_symlinks=False)); counts[entry.name] = 1; kinds[entry.name] = False
                except OSError: pass
    rows = [(prefix + name, kinds[name] or os.path.isdir(prefix + name), sizes[name], counts[name]) for name in sizes]
    rows.sort(key=lambda r: -r[2])
    return rows, sum(r[2] for r in rows)
