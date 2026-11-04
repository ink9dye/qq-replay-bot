"""Bounded durable task ownership and cross-process token mutation serialization."""
from contextlib import contextmanager, closing
import os
import sqlite3
import time
from pathlib import Path


class AuthorizationExpired(PermissionError):
    pass


@contextmanager
def process_lock(path, timeout=3):
    """Use a separate lock file; never hold the fact DB writer during uploads."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0'); handle.flush()
            def acquire():
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire(); break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('token_lifecycle_busy')
                time.sleep(.01)
        try:
            yield
        finally:
            release()


class TaskOwners:
    """Small indexed ledger, independent of the replay fact database."""
    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time
        self._cursor = ''
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS owners('
                            'job TEXT PRIMARY KEY,worker TEXT NOT NULL,generation TEXT NOT NULL,'
                            'state TEXT NOT NULL,expires REAL NOT NULL,updated REAL NOT NULL);'
                            'CREATE INDEX IF NOT EXISTS owners_updated ON owners(updated);'
                            'CREATE INDEX IF NOT EXISTS owners_expires ON owners(state,expires);')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(str(self.path), timeout=2)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def get(self, job):
        with self.db() as db:
            row = db.execute('SELECT * FROM owners WHERE job=?', (job,)).fetchone()
        return dict(row) if row else None

    def remember(self, job, worker, generation, lease):
        old = self.get(job)
        if old and (old['worker'] != worker or old['generation'] != generation or old['state'] != 'active'):
            return False
        now = self.clock()
        with self.db() as db:
            db.execute('INSERT INTO owners VALUES(?,?,?,?,?,?) ON CONFLICT(job) DO UPDATE SET '
                       'expires=excluded.expires,updated=excluded.updated',
                       (job, worker, generation, 'active', now + lease, now))
        return True

    def finish(self, job, worker, generation, reason):
        with self.db() as db:
            db.execute("UPDATE owners SET state=?,updated=? WHERE job=? AND worker=? AND generation=? AND state='active'",
                       (reason, self.clock(), job, worker, generation))

    def outstanding(self, limit=200):
        with self.db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM owners WHERE state='active' AND job>? ORDER BY job LIMIT ?", (self._cursor, limit))]
            if not rows and self._cursor:
                self._cursor = ''
                rows = [dict(r) for r in db.execute(
                    "SELECT * FROM owners WHERE state='active' ORDER BY job LIMIT ?", (limit,))]
            self._cursor = rows[-1]['job'] if rows else ''
            return rows

    def cleanup(self):
        with self.db() as db:
            db.execute("DELETE FROM owners WHERE job IN (SELECT job FROM owners WHERE state!='active' AND updated<? ORDER BY updated LIMIT 200)",
                       (self.clock() - 86400,))
