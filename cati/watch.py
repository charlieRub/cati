"""Un descriptor inotify, revisión de respaldo y cola con prioridad para novedades."""
import ctypes
import errno
import fcntl
import heapq
import os
import json
import platform
import signal
import threading
import time
from collections import defaultdict,deque
from pathlib import Path

from inotify_simple import INotify, flags

from . import catalog, spelling
from .config import DATA,give_back_memory,load
from .engine import Engine
from .indexer import error_options,fingerprint,index,lock_path,reader_options,write_status
from .readers import walk,file_kind,allowed,release_ocr,known_toolchain,SKIP_DIRS
from .scope import resolved
from .store import connect


def on_battery():
    """True solo si la batería del propio equipo se está descargando (no la de un ratón o teclado)."""
    def read(path,name):
        try:return (path/name).read_text().strip()
        except OSError:return ''
    for supply in Path('/sys/class/power_supply').glob('*'):
        if read(supply,'type')=='Battery' and read(supply,'scope')!='Device' and read(supply,'status')=='Discharging':return True
    return False


# What a scan compares; the analysis cursors and fingerprints stay in the database.
COLUMNS='path,mtime,size,kind,options,error IS NOT NULL AS error,complete'
MASK=flags.CLOSE_WRITE|flags.MOVED_TO|flags.MOVED_FROM|flags.CREATE|flags.DELETE|flags.DELETE_SELF|flags.MOVE_SELF|flags.ONLYDIR|flags.DONT_FOLLOW
CHANGES=flags.CREATE|flags.MOVED_TO|flags.MOVED_FROM|flags.DELETE|flags.DELETE_SELF|flags.MOVE_SELF
# ioprio_set: the CPU nice value does not lower disk priority outside systemd.
IOPRIO_SET={'x86_64':251,'aarch64':30,'riscv64':30,'ppc64le':273,'i686':289}.get(platform.machine())


def background_priority():
    """CPU (nice 19) y disco (clase idle) mínimos en todos los hilos ya creados; los nuevos los heredan."""
    try:tasks=[int(t.name) for t in Path('/proc/self/task').iterdir()]
    except OSError:tasks=[0]
    for task in tasks:
        try:os.setpriority(os.PRIO_PROCESS,task,19)
        except OSError:pass
        if IOPRIO_SET:
            try:ctypes.CDLL(None,use_errno=True).syscall(IOPRIO_SET,1,task,3<<13)
            except (OSError,AttributeError):pass


class Queue(dict):
    """Pendientes por ruta en texto (un Path con sus partes ocupa ~1 KB); se consulta con Path o str."""
    def __contains__(self,key):return dict.__contains__(self,os.fspath(key))
    def __getitem__(self,key):return dict.__getitem__(self,os.fspath(key))
    def get(self,key,default=None):return dict.get(self,os.fspath(key),default)
    def pop(self,key,*default):return dict.pop(self,os.fspath(key),*default)


class Watcher:
    def __init__(self, config=None, db_path=None, engine=None, status=True):
        self.config=config or load()
        self.db_path=db_path
        self.engine=engine or Engine(background=True)
        self.status_enabled=status
        self.stop=threading.Event()
        self.rescan=threading.Event()
        self.rescan.set()
        self.full_scan = True
        self.subtrees = set()
        self.condition=threading.Condition()
        self.heap=[]
        self.pending=Queue()
        self.sequence=0
        self.current=''
        self.directories={}
        self.watches={}
        self.watch_lock=threading.Lock()
        self.visited=set()
        self.watch_failures=set()
        self.retry_after={}
        self.failures={}
        self.known=0
        self.phase='scanning'
        self.error=None
        self.inotify=INotify(nonblocking=True)
        self.later=[]
        self.set_scope(resolved(self.config))
        self.touched=set()
        self.catalog_size=0
        self.working={}
        self.status_lock=threading.Lock()
        self.last_status=(None,0)

    def enqueue(self,path,priority=10,delay=0):
        path=Path(path)
        if self.retry_after.get(path,0)>time.monotonic() and priority>0:return
        key=os.fspath(path)
        with self.condition:
            previous=self.pending.get(key)
            if previous and previous[0]<=priority and priority>0:return
            self.sequence+=1
            token=self.sequence
            now=time.monotonic()
            due=now+delay
            self.pending[key]=(priority,due,token)
            # Delayed jobs wait apart: a debounced event must not hold back jobs that are already due.
            if due>now:heapq.heappush(self.later,(due,priority,token,key))
            else:heapq.heappush(self.heap,(priority,due,token,key))
            # A file rewritten every second leaves a superseded entry per event: rebuild instead of growing.
            if len(self.heap)+len(self.later)>2*len(self.pending)+4096:
                self.heap=[(p,d,t,k) for k,(p,d,t) in self.pending.items() if d<=now]
                self.later=[(d,p,t,k) for k,(p,d,t) in self.pending.items() if d>now]
                heapq.heapify(self.heap);heapq.heapify(self.later)
            self.condition.notify()

    def watch_directory(self,path):
        # The kernel answers with the folder's current descriptor: one per inode, whatever its name was.
        if path in self.watch_failures:return
        try:wd=self.inotify.add_watch(str(path),MASK)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT,errno.ENOTDIR):self.watch_failures.add(path)
            return
        self.visited.add(path)
        drop=None
        with self.watch_lock:
            old=self.watches.get(wd)
            if old==path:self.directories[path]=wd;return
            # A moved folder keeps its watch: forget its previous name.
            if old is not None and self.directories.get(old)==wd:del self.directories[old]
            previous=self.directories.get(path)
            if previous is not None and previous!=wd and self.watches.get(previous)==path:
                del self.watches[previous];drop=previous  # the inode that used to live here went elsewhere
            self.directories[path]=wd;self.watches[wd]=path
        if drop is not None:
            try:self.inotify.rm_watch(drop)
            except OSError:pass

    def unwatch_unvisited(self,scopes=None):
        """Suelta las vigilancias que esta revisión no ha recorrido: carpetas borradas, excluidas o instalaciones de herramientas."""
        prefixes=None if scopes is None else tuple(str(p).rstrip('/')+'/' for p in scopes)
        bounds=None if scopes is None else {str(p) for p in scopes}
        drop=[]
        with self.watch_lock:
            for path,wd in list(self.directories.items()):
                if path in self.visited:continue
                if prefixes is not None:
                    text=str(path)
                    if text not in bounds and not text.startswith(prefixes):continue
                del self.directories[path]
                if self.watches.get(wd)==path:del self.watches[wd];drop.append(wd)
        for wd in drop:
            try:self.inotify.rm_watch(wd)
            except OSError:pass

    def set_scope(self,active):
        self.active_config=active
        # Plain strings: permitted() runs for every event and every indexed row of a full scan.
        self.roots=tuple(str(Path(p)) for p in active['folders'])
        self.exclusions=None

    def permitted(self,path,directory=False):
        text=str(path)
        root=next((r for r in self.roots if text==r or text.startswith(r.rstrip('/')+'/')),None)
        if root is None:return False
        if self.exclusions is None or self.exclusions[0]!=self.config['exclude']:
            self.exclusions=(list(self.config['exclude']),tuple(str(Path(p)) for p in self.config['exclude']))
        if any(text==e or text.startswith(e+'/') for e in self.exclusions[1]):return False
        parts=text[len(root.rstrip('/'))+1:].split('/') if text!=root else []
        if any(p.startswith('.') or p in SKIP_DIRS or p.endswith(('.noindex','.app')) for p in parts):return False
        # Toolchain installs (an Android SDK) are recognised by content; each folder's answer is cached.
        folder=root.rstrip('/')
        for name in (parts if directory else parts[:-1]):
            folder+='/'+name
            if known_toolchain(folder):return False
        return not parts or allowed(Path(parts[-1]))

    def flush_catalog(self,db):
        # Names are searchable within a second, long before content is analysed.
        if not self.touched:return
        paths,self.touched=self.touched,set()
        try:catalog.touch(db,self.active_config['folders'],[p for p in paths if self.permitted(p)])
        except Exception as exc:
            self.error=f'Catálogo: {exc}'
            self.touched|=paths  # a busy database must not lose these names; retried on the next pass

    def listen(self):
        db=connect(self.db_path)
        try:self._listen(db)
        finally:db.close()

    def _listen(self,db):
        while not self.stop.is_set():
            self.flush_catalog(db)
            try: events=self.inotify.read(timeout=500)
            except OSError:
                if self.stop.is_set():return
                self.full_scan=True;self.rescan.set()
                self.stop.wait(1);continue  # a broken descriptor must not spin a core
            for event in events:
                if event.mask&flags.Q_OVERFLOW:self.full_scan=True;self.rescan.set();continue
                parent=self.watches.get(event.wd)
                if parent is None:continue
                path=parent/event.name if event.name else parent
                if event.mask&flags.IGNORED:
                    # Only this descriptor's entries: the same path may already be watched again under a new one.
                    with self.watch_lock:
                        if self.watches.get(event.wd)==parent:del self.watches[event.wd]
                        if self.directories.get(parent)==event.wd:del self.directories[parent]
                    continue
                if event.name and not event.name.startswith('.'):self.touched.add(path)
                if event.mask&flags.ISDIR or not event.name:
                    if event.mask&CHANGES and self.permitted(path,directory=bool(event.mask&(flags.CREATE|flags.MOVED_TO))):
                        with self.condition:
                            self.subtrees.add(path)
                        self.rescan.set()
                    continue
                if self.permitted(path) and file_kind(path,self.config['code']):
                    self.enqueue(path,0,self.config.get('event_delay',2))

    def scan(self, subtrees=None):
        self.phase='scanning'
        self.set_scope(resolved(self.config))
        if subtrees is None:
            known_toolchain.cache_clear()  # a folder can become an SDK install (or stop being one)
            now=time.monotonic()
            self.retry_after={p:t for p,t in self.retry_after.items() if t>now}
            with self.condition:self.failures={p:n for p,n in self.failures.items() if p in self.pending}
        db=connect(self.db_path)
        try:
            seen=[]
            unreadable=[]
            self.watch_failures.clear()  # retry transient failures on the next reconciliation
            self.visited=set()
            if subtrees is not None:
                # A folder that disappeared is still checked: its rows and watches must go.
                subtrees = [p for p in subtrees if self.permitted(p,directory=p.is_dir())]
                catalog.touch(db,self.active_config['folders'],subtrees)
            catalog.sync(db,self.active_config['folders'],self.config['exclude'],self.stop,self.watch_directory,seen,
                         subtrees=subtrees,unreadable=unreadable)
            self.catalog_size=db.execute('SELECT count(*) FROM catalog').fetchone()[0]
            if self.stop.is_set():return
            # Moved folders were re-pointed while walking; what was not visited is no longer watched.
            self.unwatch_unvisited(subtrees)
            self.visited=set()
            stats={}
            if self.config['code'] or self.config.get('project_screenshots_only') or self.config.get('skip_datasets'):
                # Project and dataset rules need the dedicated walk.
                candidates=list(walk(self.active_config if subtrees is None else self.active_config | {'folders':[str(p) for p in subtrees]},stop=self.stop))
            else:
                # One pass over the disk: the catalog walk already applied the same visibility rules.
                media=self.config['media']
                candidates=[]
                for path,size,mtime in seen:
                    kind=file_kind(path)
                    if kind and (media or kind not in {'image','audio','video'}):
                        candidates.append((path,kind));stats[path]=(mtime,size)
            if self.stop.is_set():return
            if subtrees is None:
                old={row['path']:row for row in db.execute(f'SELECT {COLUMNS} FROM files')}
            else:
                old={}
                for path in subtrees:
                    key=str(path)
                    old.update((row['path'],row) for row in db.execute(
                        f'SELECT {COLUMNS} FROM files WHERE path=? OR (path>=? AND path<?)',(key,key+'/',key+'0')))
            selected={str(p) for p,_ in candidates}
            # A folder that could not be listed (I/O error, a stalled network mount) keeps its rows.
            kept=tuple(str(p)+'/' for p in unreadable)
            # An existing file absent from this snapshot may have arrived after
            # the directory was walked and already been indexed by an event.
            with db:
                for path in old:
                    if path in selected or (kept and path.startswith(kept)):continue
                    if not os.path.exists(path) or not self.permitted(path) or file_kind(Path(path),self.config['code']) is None:
                        db.execute('DELETE FROM files WHERE path=?',(path,))
            options={kind:reader_options(self.config,kind) for kind in ('document','pdf','image','audio','video','code')}
            groups=defaultdict(list)
            for path,kind in candidates:
                if path in stats:mtime,size=stats[path]
                else:
                    try:info=path.stat()
                    except OSError:continue
                    mtime,size=info.st_mtime_ns,info.st_size
                row=old.get(str(path))
                if row and row['error'] and row['mtime']==mtime and row['size']==size and row['kind']==kind:
                    try:
                        if row['options']==error_options(options[kind],path.stat()):continue
                    except OSError:continue
                same=row and row['mtime']==mtime and row['size']==size and row['kind']==kind and row['options']==options[kind] and not row['error']
                if not same:groups[kind].append((mtime,path))
                elif not row['complete']:self.enqueue(path,20)
            # Fair first pass: a photo is scheduled between documents and PDF jobs.
            ordered=[deque(p for _,p in sorted(groups[k],reverse=True)) for k in ['document','pdf','image','audio','video','code'] if groups[k]]
            while any(ordered):
                for queue in ordered:
                    if queue:self.enqueue(queue.popleft(),10)
            self.known=db.execute('SELECT count(*) FROM files').fetchone()[0] if subtrees is not None else len(candidates)
            self.phase='watching'
            self.error=None
        finally:
            db.close()
            give_back_memory()  # a full scan's row snapshot is gone

    def backfill(self):
        """Huella de archivos ya analizados antes de existir la deduplicación (una vez, por lotes)."""
        db=connect(self.db_path)
        try:
            while not self.stop.is_set():
                rows=db.execute('SELECT id,path,size FROM files WHERE fingerprint IS NULL AND complete=1 AND error IS NULL LIMIT 200').fetchall()
                if not rows:return
                updates=[]
                for row in rows:
                    try:updates.append((fingerprint(row['path'],row['size']),row['id']))
                    except OSError:updates.append(('missing',row['id']))
                with db:db.executemany('UPDATE files SET fingerprint=? WHERE id=?',updates)
        finally:db.close()

    def scans(self):
        next_full = 0
        while not self.stop.is_set():
            self.rescan.wait(timeout=max(0, next_full - time.monotonic()))
            self.rescan.clear()
            if self.stop.is_set():return
            # Batch a burst of directory events, but never delay names by the
            # full reconciliation interval. Periodic scans recover missed events.
            if self.stop.wait(.5):return
            with self.condition:
                dirty, self.subtrees = self.subtrees, set()
            full = self.full_scan or time.monotonic() >= next_full
            self.full_scan = False
            scopes = []
            for path in sorted(dirty, key=lambda p:len(p.parts)):
                if not any(parent == path or parent in path.parents for parent in scopes):scopes.append(path)
            if not full and not scopes:continue
            try:
                self.scan(None if full else scopes)
                if full:
                    next_full = time.monotonic() + max(10,self.config.get('scan_seconds',1800))
                    self.backfill()
                if not self.stop.is_set():spelling.save_if_stale(self.db_path)
            except Exception as exc:
                self.error=f'Exploración: {exc}'
                self.full_scan=True
                self.stop.wait(60);self.rescan.set()

    def report(self,force=False):
        if not self.status_enabled:return
        # Desktop schedulers can boost the interpreter after it starts. Restore
        # this service's background priority only when it has actually changed.
        try:
            if os.getpriority(os.PRIO_PROCESS,os.getpid()) != 19:background_priority()
        except OSError:pass
        with self.condition:queued=len(self.pending)
        current=next(iter(self.working.values()),'')
        state={'running':not self.stop.is_set(),'mode':'watch','phase':self.phase,'workers':len(self.working),
               'current':current,'known':self.known,'queued':queued,'catalog':self.catalog_size,
               'watches':len(self.directories),'watch_fallback':len(self.watch_failures),'error':self.error}
        # Unchanged state is rewritten only as a heartbeat (readers treat 20 s of silence as stopped).
        previous,written=self.last_status
        if not force and state==previous and time.monotonic()-written<10:return
        with self.status_lock:
            write_status(state|{'updated':time.time()})
            self.last_status=(state,time.monotonic())

    def take(self):
        with self.condition:
            now=time.monotonic()
            while self.later and self.later[0][0]<=now:
                due,priority,token,path=heapq.heappop(self.later)
                if self.pending.get(path)==(priority,due,token):heapq.heappush(self.heap,(priority,due,token,path))
            while self.heap:
                priority,due,token,path=heapq.heappop(self.heap)
                if self.pending.get(path)==(priority,due,token):del self.pending[path];return Path(path)
        return None

    def workers(self):
        """Trabajos simultáneos: lectura (OCR, FFmpeg) y GPU se solapan; con batería, uno solo."""
        wanted=self.config.get('workers')
        if wanted:return max(1,int(wanted))
        if on_battery():return 1
        return 1

    def work(self,name):
        while not self.stop.is_set():
            path=self.take()
            if path is None:
                with self.condition:self.condition.wait(timeout=.5)
                continue
            kind=file_kind(path,self.config['code'])
            if not self.permitted(path) or not kind or (not self.config['media'] and kind in {'image','audio','video'}):
                self.failures.pop(path,None);self.retry_after.pop(path,None);continue
            self.working[name]=str(path)
            try:
                result=index(self.config,stop=self.stop,db_path=self.db_path,engine=self.engine,candidates=[(path,kind)],
                             max_segments=self.config.get('job_segments',16),time_budget=self.config.get('job_seconds',2),
                             report_status=False,locked=True)
                if result['partial']:self.enqueue(path,20)
                if result.get('changed'):self.enqueue(path,20,30)  # still being written: try again once it settles
                if result['errors']:self.retry_after[path]=time.monotonic()+900
                else:
                    self.failures.pop(path,None)
                    self.retry_after.pop(path,None)
                self.error=None
            except Exception as exc:
                # A failing engine must not trigger extraction/inference every ten seconds.
                self.error=str(exc)
                failures=min(7,self.failures.get(path,0)+1)
                self.failures[path]=failures
                delay=min(900,15*2**(failures-1))
                with self.condition:self.pending.pop(path,None)
                self.enqueue(path,20,delay)
                self.retry_after[path]=time.monotonic()+delay
                self.stop.wait(1)
            finally:self.working.pop(name,None)
            self.stop.wait(max(0,self.config.get('job_delay',.25)))

    def run(self):
        # Give this background process a distinct name instead of the generic Python
        # interpreter, and lower already-created native library threads as well.
        try:ctypes.CDLL(None).prctl(15,b'cati-index',0,0,0)
        except (OSError,AttributeError):pass
        background_priority()
        lock=lock_path(self.db_path).open('a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Ya hay otra indexación en curso')
        threads=[threading.Thread(target=self.listen,daemon=True),threading.Thread(target=self.scans,daemon=True)]
        threads+=[threading.Thread(target=self.work,args=(f'w{i}',),daemon=True) for i in range(self.workers())]
        for thread in threads:thread.start()
        try:
            idle=0
            while not self.stop.is_set():
                self.report()
                self.stop.wait(1)
                idle=idle+1 if not self.working and not self.pending else 0
                if idle==60:release_ocr()  # nothing queued for a minute: give the OCR memory back
        finally:
            self.stop.set()
            self.rescan.set()
            with self.condition:self.condition.notify_all()
            for thread in threads:thread.join(timeout=10)
            self.inotify.close()
            self.report(force=True)
            lock.close()


def run():
    watcher=Watcher()
    signal.signal(signal.SIGTERM,lambda *_:watcher.stop.set())
    signal.signal(signal.SIGINT,lambda *_:watcher.stop.set())
    watcher.run()
