"""Indexación atómica y reanudable por archivo; trabajos acotados para no bloquear la cola."""
import contextlib
import fcntl
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .config import DATA, load
from .engine import Engine, EngineError
from .readers import walk, extract, cache_thumbnail
from .store import connect, next_segment_id


def reader_options(config, kind=None):
    options = {k:config.get(k) for k in ['media','video_stride','max_file_mb','max_text_mb','max_pdf_pages']} | {'readers':3}
    # Documents and PDFs gained embedded images and more formats; other kinds keep their analysis.
    if kind in {'document', 'pdf'}: options['documents'] = 2
    return hashlib.sha256(json.dumps(options,sort_keys=True).encode()).hexdigest()


def fingerprint(path, size):
    """Huella del contenido: completa hasta 8 MB; en archivos mayores, inicio, centro y final."""
    digest = hashlib.blake2b(str(size).encode(), digest_size=20)
    with open(path, 'rb') as handle:
        if size <= 8 * 1024 * 1024:
            while block := handle.read(1024 * 1024): digest.update(block)
        else:
            for offset in (0, size // 2, size - 1024 * 1024):
                handle.seek(offset); digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def error_options(options, stat):
    """Negative cache invalidates on content/permission changes or reader options.
    Its version can be bumped without invalidating successfully indexed files."""
    return f'{options}:failed-v1:{stat.st_ctime_ns}'


def reuse(db, path, stat, kind, options, print_):
    """Copia el análisis de un archivo idéntico ya completado; devuelve True si lo hizo."""
    twin = db.execute('''SELECT id,cursor FROM files WHERE fingerprint=? AND size=? AND kind=? AND options=?
                         AND complete=1 AND error IS NULL AND path<>? LIMIT 1''',
                      (print_, stat.st_size, kind, options, str(path))).fetchone()
    if twin is None: return False
    with db:
        db.execute('''INSERT INTO files(path,mtime,size,kind,error,options,complete,cursor,fingerprint) VALUES(?,?,?,?,NULL,?,1,?,?)
           ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,size=excluded.size,kind=excluded.kind,error=NULL,
           options=excluded.options,complete=1,cursor=excluded.cursor,fingerprint=excluded.fingerprint''',
                   (str(path), stat.st_mtime_ns, stat.st_size, kind, options, twin['cursor'], print_))
        file_id = db.execute('SELECT id FROM files WHERE path=?', (str(path),)).fetchone()[0]
        db.execute('DELETE FROM segments WHERE file=?', (file_id,))
        for text, segment_kind, loc, end, vec, sets_scale in db.execute(
                'SELECT text,kind,loc,end,vec,sets_scale FROM segments WHERE file=? ORDER BY id', (twin['id'],)).fetchall():
            row = db.execute('INSERT INTO segments(id,file,text,kind,loc,end,vec,sets_scale) VALUES(?,?,?,?,?,?,?,?)',
                             (next_segment_id(db), file_id, text, segment_kind, loc, end, vec, sets_scale))
            db.execute('INSERT INTO words(rowid,path,text) VALUES(?,?,?)', (row.lastrowid, str(path), text))
    return True


def lock_path(db_path=None):
    """Un cerrojo por índice: las pruebas con su propia base no chocan con el servicio."""
    return Path(db_path).with_suffix('.lock') if db_path else DATA/'index.lock'


class Changed(Exception):
    """El archivo cambió mientras se leía: se reintenta más tarde, no se guarda como fallo."""


def errors_log(db_path=None):
    """Registro de fallos junto a su índice: las pruebas con su propia base no escriben en el del usuario."""
    return Path(db_path).with_name(Path(db_path).stem + '-errors.log') if db_path else DATA/'index-errors.log'


def log_error(path, exc, db_path=None, limit=1 << 20):
    log = errors_log(db_path)
    try:
        if log.stat().st_size > limit: log.replace(log.with_name(log.name + '.1'))  # keep one previous file
    except OSError: pass
    try:
        with log.open('a') as out: out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {path}: {exc}\n")
    except OSError: pass  # a full or read-only disk must not stop indexing


def write_status(state):
    temporary = DATA / 'status.tmp'
    temporary.write_text(json.dumps(state,ensure_ascii=False))
    temporary.replace(DATA/'status.json')


def index(config=None, stop=None, notify=None, db_path=None, engine=None,
          candidates=None, max_segments=None, time_budget=None, report_status=True, locked=False):
    config = config or load()
    all_options = {}
    supplied = candidates is not None
    # The watcher holds the lock for its whole life and runs several jobs at once.
    with (contextlib.nullcontext() if locked else lock_path(db_path).open('a')) as lock:
        if not locked:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Ya hay una indexación en curso')
        db = connect(db_path)
        engine = engine or Engine()
        state = {'running':True,'done':0,'unchanged':0,'partial':0,'errors':0,'current':'','started':time.time()}
        reported = 0.0
        def report(final=False):
            nonlocal reported
            # Thousands of unchanged files pass per second: status is rewritten at most 4 times a second.
            if not final and time.monotonic()-reported < .25: return
            reported = time.monotonic()
            if report_status: write_status(state)
            if notify: notify(state.copy())
        report()
        try:
            if stop and stop.is_set(): return state
            if candidates is None:
                candidates = list(walk(config))
            candidates = list(candidates)
            if stop and stop.is_set(): return state
            if not supplied:
                selected = {str(p) for p,_ in candidates}
                with db:
                    for row in db.execute('SELECT id,path FROM files').fetchall():
                        if row['path'] not in selected: db.execute('DELETE FROM files WHERE id=?',(row['id'],))
            state['total'] = len(candidates)
            for path,kind in candidates:
                path = Path(path)
                if stop and stop.is_set(): break
                state['current'] = str(path)
                report()
                staging = iterator = None
                stat = None
                options = all_options.setdefault(kind, reader_options(config, kind))
                try:
                    if not path.is_file() or path.is_symlink():
                        with db: db.execute('DELETE FROM files WHERE path=?',(str(path),))
                        continue
                    stat = path.stat()
                    old = db.execute('SELECT * FROM files WHERE path=?',(str(path),)).fetchone()
                    same = bool(old and not old['error'] and old['kind']==kind and old['options']==options
                                and old['mtime']==stat.st_mtime_ns and old['size']==stat.st_size)
                    if same and old['complete']:
                        state['unchanged'] += 1
                        continue
                    print_ = old['fingerprint'] if same else None
                    if not same:
                        print_ = fingerprint(path, stat.st_size)
                        if reuse(db, path, stat, kind, options, print_):
                            state['done'] += 1; state['reused'] = state.get('reused', 0) + 1
                            continue
                    resume = json.loads(old['cursor']) if same else None
                    if stat.st_size > config['max_file_mb']*1024**2:
                        raise ValueError(f"Supera {config['max_file_mb']} MB")
                    if kind in {'document','code'} and stat.st_size>config.get('max_text_mb',32)*1024**2:
                        raise ValueError(f"Documento supera {config.get('max_text_mb',32)} MB; aumenta max_text_mb para incluirlo")
                    staging = sqlite3.connect('')
                    staging.execute('CREATE TABLE staged(text,kind,loc,end,vec,sets_scale)')
                    count,complete,last_cursor = 0,False,resume or [0,0]
                    began = time.monotonic()
                    iterator = iter(extract(path,kind,config,stop,cursor=resume))
                    batch = []
                    def flush():
                        nonlocal count,last_cursor
                        if not batch: return
                        vectors = engine.text([b.text for b in batch],[path.stem]*len(batch))
                        for segment,vector in zip(batch,vectors): store(segment,vector)
                        batch.clear()
                    def store(segment,vector):
                        nonlocal count,last_cursor
                        last_cursor = segment.cursor or [0,count+1]
                        scale = not ((kind=='pdf' and (segment.loc or 0)>30) or (kind=='document' and last_cursor[1]>30))
                        staging.execute('INSERT INTO staged VALUES(?,?,?,?,?,?)',(segment.text,segment.kind,segment.loc,segment.end,vector.astype('<f4').tobytes(),int(scale)))
                        count += 1
                        if count%128==0: staging.commit()
                    while (max_segments is None or count+len(batch)<max_segments) and (count+len(batch)==0 or time_budget is None or time.monotonic()-began<time_budget):
                        if stop and stop.is_set(): raise InterruptedError()
                        try: segment = next(iterator)
                        except StopIteration: complete = True; break
                        if segment.media:
                            # Media files are temporary; embed before the reader advances.
                            flush()
                            modality = 'audio' if segment.kind=='audio' else 'image'
                            vector = engine.media(segment.media,modality)
                            if segment.kind in {'image','video-frame'}: cache_thumbnail(segment.media,path,segment.loc)
                            store(segment,vector)
                        else:
                            batch.append(segment)
                            if len(batch)>=8: flush()
                    flush()
                    iterator.close()
                    if not count and complete and not same:
                        vector = engine.text([path.stem],[path.stem])[0]
                        staging.execute('INSERT INTO staged VALUES(?,?,?,?,?,?)',(path.stem,'name',None,None,vector.astype('<f4').tobytes(),1))
                    after = path.stat()
                    if (after.st_mtime_ns,after.st_size)!=(stat.st_mtime_ns,stat.st_size):
                        raise Changed('El archivo cambió mientras se leía; se reintentará')
                    with db:
                        db.execute('''INSERT INTO files(path,mtime,size,kind,error,options,complete,cursor,fingerprint) VALUES(?,?,?,?,NULL,?,?,?,?)
                           ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,size=excluded.size,kind=excluded.kind,error=NULL,
                           options=excluded.options,complete=excluded.complete,cursor=excluded.cursor,fingerprint=excluded.fingerprint''',
                           (str(path),stat.st_mtime_ns,stat.st_size,kind,options,int(complete),json.dumps(last_cursor),print_))
                        file_id = db.execute('SELECT id FROM files WHERE path=?',(str(path),)).fetchone()[0]
                        if not same: db.execute('DELETE FROM segments WHERE file=?',(file_id,))
                        for text,segment_kind,loc,end,vec,sets_scale in staging.execute('SELECT * FROM staged'):
                            row=db.execute('INSERT INTO segments(id,file,text,kind,loc,end,vec,sets_scale) VALUES(?,?,?,?,?,?,?,?)',
                                           (next_segment_id(db),file_id,text,segment_kind,loc,end,vec,sets_scale))
                            db.execute('INSERT INTO words(rowid,path,text) VALUES(?,?,?)',(row.lastrowid,str(path),text))
                    state['done' if complete else 'partial'] += 1
                except InterruptedError: break
                except EngineError: raise
                except Changed:
                    state['changed'] = state.get('changed', 0) + 1  # the previous analysis stays until it settles
                except Exception as exc:
                    try: now = path.stat()
                    except OSError: now = None
                    if now is None:
                        # Deleted while being read: not a failure of the file.
                        with db: db.execute('DELETE FROM files WHERE path=?',(str(path),))
                        state['changed'] = state.get('changed', 0) + 1
                    elif stat and (now.st_mtime_ns,now.st_size)!=(stat.st_mtime_ns,stat.st_size):
                        # A half-written file («image file is truncated», bad xref): it is read again once it settles.
                        state['changed'] = state.get('changed', 0) + 1
                    else:
                        state['errors'] += 1
                        with db:
                            db.execute('DELETE FROM files WHERE path=?',(str(path),))
                            db.execute('INSERT INTO files(path,mtime,size,kind,error,options) VALUES(?,?,?,?,?,?)',
                                       (str(path),now.st_mtime_ns,now.st_size,kind,str(exc)[:500],error_options(options,now)))
                        log_error(path, exc, db_path)
                finally:
                    if iterator: iterator.close()  # removes the reader's temporary media now, even after a failure
                    if staging: staging.close()
                report()
        except Exception as exc:
            state['error']=str(exc)
            raise
        finally:
            db.close()
            state.update(running=False,finished=time.time(),current='')
            report(final=True)
    return state
