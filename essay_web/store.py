import hashlib
import json
import secrets
import shutil
import sqlite3
import time
from contextlib import contextmanager


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))


class Store:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.data
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'content').mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, session TEXT, created REAL, submitted TEXT);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, token TEXT, state TEXT, created REAL, ended REAL,
                    fingerprint TEXT, labels TEXT, model TEXT, cancel INTEGER DEFAULT 0,
                    message TEXT DEFAULT '', total INTEGER, done INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tasks (
                    job TEXT, id TEXT, state TEXT DEFAULT 'pending', result TEXT,
                    PRIMARY KEY(job,id));
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, job TEXT, task TEXT, number INTEGER,
                    started REAL, ended REAL, model TEXT, settings TEXT, labels TEXT,
                    status TEXT, provider_request TEXT, provider_response TEXT,
                    usage TEXT, cost TEXT, reservation TEXT, pricing TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, job TEXT, task TEXT, created REAL, kind TEXT);
                CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, window INTEGER, count INTEGER);
                CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL);
                CREATE INDEX IF NOT EXISTS attempts_started ON attempts(started);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / 'ledger.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA secure_delete=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def directory(self, ident):
        # IDs are server-created hex, never paths supplied by the browser.
        if len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
            raise ValueError('Invalid identifier')
        return self.root / 'content' / ident

    def get_job(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
            return dict(row) if row else None

    def rate_limit(self, key):
        window = int(time.time() // 60)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO rate_limits VALUES (?,?,1) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END, window=excluded.window', (digest(key), window))
            count = db.execute('SELECT count FROM rate_limits WHERE key=?', (digest(key),)).fetchone()[0]
            db.execute('DELETE FROM rate_limits WHERE window<?', (window - 2,))
            return count <= self.settings.requests_per_minute

    def cleanup(self):
        cutoff = time.time() - self.settings.retention_seconds
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            ids = [r[0] for r in db.execute('SELECT id FROM drafts WHERE created<? AND submitted IS NULL', (cutoff,))]
            jobs = [r[0] for r in db.execute("SELECT id FROM jobs WHERE ended<? AND state!='expired'", (cutoff,))]
            # Remove files before marking expired; an interrupted cleanup is retryable.
            for ident in set(ids + jobs):
                directory = self.directory(ident)
                if directory.exists():
                    shutil.rmtree(directory)
            for ident in ids:
                db.execute('DELETE FROM drafts WHERE id=?', (ident,))
            for ident in jobs:
                db.execute('DELETE FROM tasks WHERE job=?', (ident,))
                db.execute('DELETE FROM drafts WHERE id=?', (ident,))
                db.execute("UPDATE jobs SET state='expired', token='', fingerprint='', message='' WHERE id=?", (ident,))
            # Upload/write crashes can leave a directory before its row is committed.
            known = {r[0] for r in db.execute('SELECT id FROM drafts UNION SELECT id FROM jobs')}
            for directory in (self.root / 'content').iterdir():
                if directory.is_dir() and directory.name not in known and directory.stat().st_mtime < cutoff:
                    checked = self.directory(directory.name).resolve()
                    if checked.parent != (self.root / 'content').resolve() or directory.is_symlink():
                        continue
                    shutil.rmtree(checked)
        if ids or jobs:
            with self.connect() as db:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def recover(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE attempts SET status='unknown', ended=?, error='worker_interrupted' WHERE status='sending'", (time.time(),))
            db.execute("UPDATE tasks SET state='unknown' WHERE state='sending'")
            db.execute("UPDATE jobs SET state='interrupted', ended=?, message='Worker restarted. Review uncertain calls before resuming.' WHERE state='running'", (time.time(),))

    def heartbeat(self):
        with self.connect() as db:
            db.execute('INSERT INTO worker VALUES (1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat', (time.time(),))

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not job:
                return None
            db.execute("UPDATE jobs SET state='running', ended=NULL WHERE id=?", (job[0],))
            return job[0]

