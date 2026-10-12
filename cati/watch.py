"""Un descriptor inotify, revisión de respaldo y cola con prioridad para novedades."""
import fcntl
import heapq
import os
import json
import signal
import threading
import time
from collections import defaultdict,deque
from pathlib import Path

from inotify_simple import INotify, flags

from . import catalog
from .config import DATA,load
from .engine import Engine
from .indexer import index,lock_path,reader_options,write_status
from .readers import walk,file_kind,allowed,SKIP_DIRS
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


class Watcher:
    def __init__(self, config=None, db_path=None, engine=None, status=True):
        self.config=config or load()
        self.db_path=db_path
        self.engine=engine or Engine()
        self.status_enabled=status
        self.stop=threading.Event()
        self.rescan=threading.Event()
        self.rescan.set()
        self.condition=threading.Condition()
        self.heap=[]
        self.pending={}
        self.sequence=0
        self.current=''
        self.directories={}
        self.watches={}
        self.watch_failures=set()
        self.retry_after={}
        self.known=0
        self.phase='scanning'
        self.error=None
        self.inotify=INotify(nonblocking=True)
        self.active_config=resolved(self.config)
        self.touched=set()
        self.catalog_size=0
        self.working={}
        self.status_lock=threading.Lock()

    def enqueue(self,path,priority=10,delay=0):
        path=Path(path)
        if self.retry_after.get(path,0)>time.monotonic() and priority>0:return
        with self.condition:
            previous=self.pending.get(path)
            if previous and previous[0]<=priority and priority>0:return
            self.sequence+=1
            token=self.sequence
            due=time.monotonic()+delay
            self.pending[path]=(priority,due,token)
            heapq.heappush(self.heap,(priority,due,token,path))
            self.condition.notify()

    def watch_directory(self,path):
        if path in self.directories or path in self.watch_failures:return
        mask=flags.CLOSE_WRITE|flags.MOVED_TO|flags.MOVED_FROM|flags.CREATE|flags.DELETE|flags.DELETE_SELF|flags.MOVE_SELF|flags.ONLYDIR|flags.DONT_FOLLOW
        try:
            wd=self.inotify.add_watch(str(path),mask)
            self.directories[path]=wd
            self.watches[wd]=path
        except OSError:self.watch_failures.add(path)

    def permitted(self,path):
        if not any(path==Path(p) or Path(p) in path.parents for p in self.active_config['folders']):return False
        if any(path==Path(p) or Path(p) in path.parents for p in self.config['exclude']):return False
        if any(p.startswith('.') or p in SKIP_DIRS for p in path.relative_to(next(Path(p) for p in self.active_config['folders'] if path==Path(p) or Path(p) in path.parents)).parts):return False
        return allowed(path)

    def flush_catalog(self,db):
        # Names are searchable within a second, long before content is analysed.
        if not self.touched:return
        paths,self.touched=self.touched,set()
        try:catalog.touch(db,self.active_config['folders'],paths)
        except Exception as exc:self.error=f'Catálogo: {exc}'

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
                self.rescan.set();continue
            for event in events:
                if event.mask&flags.Q_OVERFLOW:self.rescan.set();continue
                parent=self.watches.get(event.wd)
                if parent is None:continue
                path=parent/event.name if event.name else parent
                if event.mask&flags.IGNORED:
                    self.watches.pop(event.wd,None);self.directories.pop(parent,None);continue
                if event.name and not event.name.startswith('.'):self.touched.add(path)
                if event.mask&flags.ISDIR or not event.name:
                    if not path.name.startswith('.') and event.mask&(flags.CREATE|flags.MOVED_TO|flags.MOVED_FROM|flags.DELETE|flags.DELETE_SELF|flags.MOVE_SELF): self.rescan.set()
                    continue
                if self.permitted(path) and file_kind(path,self.config['code']):
                    self.enqueue(path,0,self.config.get('event_delay',2))

    def scan(self):
        self.phase='scanning'
        self.active_config=resolved(self.config)
        db=connect(self.db_path)
        try:
            self.catalog_size=catalog.sync(db,self.active_config['folders'],self.config['exclude'],self.stop,self.watch_directory)
            candidates=list(walk(self.active_config,stop=self.stop))
            if self.stop.is_set():return
            old={row['path']:dict(row) for row in db.execute('SELECT * FROM files')}
            selected={str(p) for p,_ in candidates}
            # An existing file absent from this snapshot may have arrived after
            # the directory was walked and already been indexed by an event.
            with db:
                for path in old:
                    if path not in selected and not Path(path).exists():db.execute('DELETE FROM files WHERE path=?',(path,))
                    elif not self.permitted(Path(path)) or file_kind(Path(path),self.config['code']) is None:db.execute('DELETE FROM files WHERE path=?',(path,))
            options=reader_options(self.config)
            groups=defaultdict(list)
            for path,kind in candidates:
                try:stat=path.stat()
                except OSError:continue
                row=old.get(str(path))
                same=row and row['mtime']==stat.st_mtime_ns and row['size']==stat.st_size and row['kind']==kind and row['options']==options and not row['error']
                if not same:groups[kind].append((stat.st_mtime_ns,path))
                elif not row['complete']:self.enqueue(path,20)
            # Fair first pass: a photo is scheduled between documents and PDF jobs.
            ordered=[deque(p for _,p in sorted(groups[k],reverse=True)) for k in ['document','pdf','image','audio','video','code'] if groups[k]]
            while any(ordered):
                for queue in ordered:
                    if queue:self.enqueue(queue.popleft(),10)
            self.known=len(candidates)
            self.phase='watching'
            self.error=None
        finally:db.close()

    def scans(self):
        while not self.stop.is_set():
            if self.rescan.wait(timeout=max(10,self.config.get('scan_seconds',300))):self.rescan.clear()
            if self.stop.is_set():return
            try:self.scan()
            except Exception as exc:self.error=f'Exploración: {exc}'
            # Coalesce repeated new-directory notifications without losing them.
            self.stop.wait(2)

    def report(self):
        if not self.status_enabled:return
        with self.condition:queued=len(self.pending)
        current=next(iter(self.working.values()),'')
        with self.status_lock:
            write_status({'running':not self.stop.is_set(),'mode':'watch','phase':self.phase,'workers':len(self.working),
                      'current':current,'known':self.known,'queued':queued,'catalog':self.catalog_size,
                      'watches':len(self.directories),'watch_fallback':len(self.watch_failures),
                      'error':self.error,'updated':time.time()})

    def take(self):
        with self.condition:
            while self.heap:
                priority,due,token,path=self.heap[0]
                if self.pending.get(path)!=(priority,due,token):heapq.heappop(self.heap);continue
                if due>time.monotonic():return None
                heapq.heappop(self.heap);self.pending.pop(path,None);return path
        return None

    def workers(self):
        """Trabajos simultáneos: lectura (OCR, FFmpeg) y GPU se solapan; con batería, uno solo."""
        wanted=self.config.get('workers')
        if wanted:return max(1,int(wanted))
        if on_battery():return 1
        return max(1,min(3,(os.cpu_count() or 2)//2))

    def work(self,name):
        while not self.stop.is_set():
            path=self.take()
            if path is None:
                with self.condition:self.condition.wait(timeout=.5)
                continue
            if not self.permitted(path):continue
            kind=file_kind(path,self.config['code'])
            if not kind or (not self.config['media'] and kind in {'image','audio','video'}):continue
            self.working[name]=str(path)
            try:
                result=index(self.config,stop=self.stop,db_path=self.db_path,engine=self.engine,candidates=[(path,kind)],
                             max_segments=self.config.get('job_segments',16),time_budget=self.config.get('job_seconds',2),
                             report_status=False,locked=True)
                if result['partial']:self.enqueue(path,20)
                if result['errors']:self.retry_after[path]=time.monotonic()+900
                self.error=None
            except Exception as exc:
                self.error=str(exc);self.enqueue(path,20,10)
                self.stop.wait(1)
            finally:self.working.pop(name,None)

    def run(self):
        lock=lock_path(self.db_path).open('a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Ya hay otra indexación en curso')
        threads=[threading.Thread(target=self.listen,daemon=True),threading.Thread(target=self.scans,daemon=True)]
        threads+=[threading.Thread(target=self.work,args=(f'w{i}',),daemon=True) for i in range(self.workers())]
        for thread in threads:thread.start()
        try:
            while not self.stop.is_set():
                self.report()
                self.stop.wait(1)
        finally:
            self.stop.set()
            self.rescan.set()
            with self.condition:self.condition.notify_all()
            for thread in threads:thread.join(timeout=10)
            self.inotify.close()
            self.report()
            lock.close()


def run():
    watcher=Watcher()
    signal.signal(signal.SIGTERM,lambda *_:watcher.stop.set())
    signal.signal(signal.SIGINT,lambda *_:watcher.stop.set())
    watcher.run()
