"""Caché acotada del texto extraído; los lotes reanudados no vuelven a descomprimir Office."""
import hashlib
import os
import sys
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path


class DocumentTextCache:
    def __init__(self, directory=None, memory_bytes=16*1024**2, disk_bytes=128*1024**2):
        base = Path(os.getenv('XDG_CACHE_HOME', Path.home()/'.cache'))
        self.directory = Path(directory) if directory else base/'cati'/'text-v1'
        self.memory_bytes, self.disk_bytes = memory_bytes, disk_bytes
        self.memory = OrderedDict()
        self.used = 0
        self.stored = None  # bytes on disk, known after the first prune
        self.lock = threading.Lock()

    def identity(self, path):
        stat = path.stat()
        value = f'{path.resolve()}:{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}'
        return hashlib.sha256(value.encode()).hexdigest()

    def remember(self, key, text):
        size = sys.getsizeof(text)
        if size > self.memory_bytes: return
        previous = self.memory.pop(key, None)
        if previous is not None: self.used -= sys.getsizeof(previous)
        while self.memory and self.used + size > self.memory_bytes:
            _, old = self.memory.popitem(last=False)
            self.used -= sys.getsizeof(old)
        self.memory[key] = text
        self.used += size

    def get(self, path, extract):
        path = Path(path)
        key = self.identity(path)
        # Serialise cache misses: concurrent jobs for the same file extract it once.
        with self.lock:
            if key in self.memory:
                self.memory.move_to_end(key)
                return self.memory[key]
            target = self.directory/(key+'.txt')
            try:
                if target.stat().st_size > self.disk_bytes: raise ValueError('Caché demasiado grande')
                text = target.read_text(encoding='utf-8')
                target.touch()
            except (OSError, UnicodeError, ValueError):
                text = extract(path)
                if '-----BEGIN ' in text and 'PRIVATE KEY-----' in text:
                    raise ValueError('Archivo con clave privada excluido')
                # Never cache a read across a modification; the indexer retries it.
                if self.identity(path) != key: return text
                encoded = text.encode('utf-8')
                if len(encoded) <= self.disk_bytes:
                    try:
                        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as handle:
                            temporary = Path(handle.name)
                            try:
                                handle.write(encoded)
                                handle.close()
                                temporary.replace(target)
                            finally: temporary.unlink(missing_ok=True)
                        # Listing the folder on every write cost a scan per document: only when over budget.
                        if self.stored is not None: self.stored += len(encoded)
                        if self.stored is None or self.stored > self.disk_bytes: self.prune()
                    except OSError: pass  # Read-only/full cache must not break indexing.
            self.remember(key, text)
            return text

    def prune(self):
        files = []
        for path in self.directory.glob('*.txt'):
            try: stat = path.stat()
            except OSError: continue
            files.append((stat.st_mtime_ns, stat.st_size, path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.disk_bytes: break
            path.unlink(missing_ok=True)
            total -= size
        self.stored = total


DOCUMENT_TEXT = DocumentTextCache()
