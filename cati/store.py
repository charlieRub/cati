import sqlite3
from contextlib import contextmanager
from .config import DATA, FINGERPRINT


def add_column(db, table, column, definition):
    """Idempotente aunque otra conexión migre a la vez."""
    if column in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}: return
    try:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    except sqlite3.OperationalError as exc:
        if 'duplicate column' not in str(exc): raise


def connect(path=None, threads=False):
    path = path or DATA / "index.sqlite3"
    db = sqlite3.connect(path, timeout=30, check_same_thread=not threads)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    if db.execute('PRAGMA user_version').fetchone()[0] < 2:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
              mtime INTEGER NOT NULL, size INTEGER NOT NULL, kind TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS segments (id INTEGER PRIMARY KEY, file INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
              text TEXT NOT NULL, kind TEXT NOT NULL, loc REAL, end REAL, vec BLOB NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(path, text, tokenize='unicode61 remove_diacritics 2');
            CREATE TRIGGER IF NOT EXISTS segments_delete AFTER DELETE ON segments BEGIN
              DELETE FROM words WHERE rowid=old.id;
            END;
        """)
        add_column(db, 'files', 'options', "TEXT NOT NULL DEFAULT ''")
        add_column(db, 'files', 'complete', 'INTEGER NOT NULL DEFAULT 1')
        add_column(db, 'files', 'cursor', "TEXT NOT NULL DEFAULT '[0,0]'")
        add_column(db, 'segments', 'sets_scale', 'INTEGER NOT NULL DEFAULT 1')
        db.execute('PRAGMA user_version=2')
    if db.execute('PRAGMA user_version').fetchone()[0] < 3:
        # Every file and folder by name, including types whose content is not read.
        db.executescript("""
            CREATE TABLE IF NOT EXISTS catalog (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL, folders TEXT NOT NULL, ext TEXT NOT NULL, is_dir INTEGER NOT NULL,
              size INTEGER NOT NULL, mtime INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS catalog_mtime ON catalog(mtime);
            CREATE INDEX IF NOT EXISTS catalog_ext ON catalog(ext);
            CREATE VIRTUAL TABLE IF NOT EXISTS catalog_names USING fts5(name, folders, content='catalog',
              content_rowid='id', tokenize='trigram remove_diacritics 1');
            CREATE TRIGGER IF NOT EXISTS catalog_insert AFTER INSERT ON catalog BEGIN
              INSERT INTO catalog_names(rowid,name,folders) VALUES(new.id,new.name,new.folders);
            END;
            CREATE TRIGGER IF NOT EXISTS catalog_delete AFTER DELETE ON catalog BEGIN
              INSERT INTO catalog_names(catalog_names,rowid,name,folders) VALUES('delete',old.id,old.name,old.folders);
            END;
            CREATE TRIGGER IF NOT EXISTS catalog_update AFTER UPDATE OF name,folders ON catalog BEGIN
              INSERT INTO catalog_names(catalog_names,rowid,name,folders) VALUES('delete',old.id,old.name,old.folders);
              INSERT INTO catalog_names(rowid,name,folders) VALUES(new.id,new.name,new.folders);
            END;
            PRAGMA user_version=3;
        """)
    if db.execute('PRAGMA user_version').fetchone()[0] < 4:
        # Content fingerprint: an exact copy reuses the analysis of the original.
        add_column(db, 'files', 'fingerprint', 'TEXT')
        db.executescript('CREATE INDEX IF NOT EXISTS files_fingerprint ON files(fingerprint); PRAGMA user_version=4;')
    row = db.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
    if row and row[0] != FINGERPRINT:
        db.close()
        raise RuntimeError("Cambió el modelo o los prompts. Reconstruye el índice antes de buscar.")
    if not row:
        db.execute("INSERT INTO meta VALUES('fingerprint', ?)", (FINGERPRINT,))
    db.commit()
    path.chmod(0o600)
    return db


@contextmanager
def opened(path=None):
    db=connect(path)
    try:
        with db:yield db
    finally:db.close()
