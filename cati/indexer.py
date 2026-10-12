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
from .store import connect


def reader_options(config):
    options = {k:config.get(k) for k in ['media','video_stride','max_file_mb','max_text_mb','max_pdf_pages']}
    return hashlib.sha256(json.dumps(options | {'readers':3},sort_keys=True).encode()).hexdigest()


def lock_path(db_path=None):
    """Un cerrojo por índice: las pruebas con su propia base no chocan con el servicio."""
    return Path(db_path).with_suffix('.lock') if db_path else DATA/'index.lock'


def write_status(state):
    temporary = DATA / 'status.tmp'
    temporary.write_text(json.dumps(state,ensure_ascii=False))
    temporary.replace(DATA/'status.json')


def index(config=None, stop=None, notify=None, db_path=None, engine=None,
          candidates=None, max_segments=None, time_budget=None, report_status=True, locked=False):
    config = config or load()
    options = reader_options(config)
    supplied = candidates is not None
    # The watcher holds the lock for its whole life and runs several jobs at once.
    with (contextlib.nullcontext() if locked else lock_path(db_path).open('a')) as lock:
        if not locked:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Ya hay una indexación en curso')
        db = connect(db_path)
        engine = engine or Engine()
        state = {'running':True,'done':0,'unchanged':0,'partial':0,'errors':0,'current':'','started':time.time()}
        def report():
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
                staging = None
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
                        raise ValueError('El archivo cambió mientras se leía; se reintentará')
                    with db:
                        db.execute('''INSERT INTO files(path,mtime,size,kind,error,options,complete,cursor) VALUES(?,?,?,?,NULL,?,?,?)
                           ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,size=excluded.size,kind=excluded.kind,error=NULL,
                           options=excluded.options,complete=excluded.complete,cursor=excluded.cursor''',
                           (str(path),stat.st_mtime_ns,stat.st_size,kind,options,int(complete),json.dumps(last_cursor)))
                        file_id = db.execute('SELECT id FROM files WHERE path=?',(str(path),)).fetchone()[0]
                        if not same: db.execute('DELETE FROM segments WHERE file=?',(file_id,))
                        for text,segment_kind,loc,end,vec,sets_scale in staging.execute('SELECT * FROM staged'):
                            row=db.execute('INSERT INTO segments(file,text,kind,loc,end,vec,sets_scale) VALUES(?,?,?,?,?,?,?)',
                                           (file_id,text,segment_kind,loc,end,vec,sets_scale))
                            db.execute('INSERT INTO words(rowid,path,text) VALUES(?,?,?)',(row.lastrowid,str(path),text))
                    state['done' if complete else 'partial'] += 1
                except InterruptedError: break
                except EngineError: raise
                except Exception as exc:
                    state['errors'] += 1
                    with db:
                        db.execute('DELETE FROM files WHERE path=?',(str(path),))
                        try:
                            stat=path.stat()
                            db.execute('INSERT INTO files(path,mtime,size,kind,error) VALUES(?,?,?,?,?)',
                                       (str(path),stat.st_mtime_ns,stat.st_size,kind,str(exc)[:500]))
                        except OSError: pass
                    with (DATA/'index-errors.log').open('a') as log: log.write(f'{path}: {exc}\n')
                finally:
                    if staging: staging.close()
                report()
        except Exception as exc:
            state['error']=str(exc)
            raise
        finally:
            db.close()
            state.update(running=False,finished=time.time(),current='')
            report()
    return state
