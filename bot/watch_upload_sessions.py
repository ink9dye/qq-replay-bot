"""Small disk-backed upload provenance; an upload job never proves game number one."""
import json
import os
import sqlite3
import time
from pathlib import Path

NAME = 'watch-upload-sessions.sqlite3'
SCHEMA = '''CREATE TABLE IF NOT EXISTS uploads (
 digest TEXT NOT NULL, job TEXT NOT NULL, players TEXT NOT NULL,
 recorded REAL NOT NULL, received REAL NOT NULL, capture TEXT,
 PRIMARY KEY(digest,job));
CREATE INDEX IF NOT EXISTS uploads_received ON uploads(received);'''


def record(data_dir, digest, job, players, recorded, capture=None):
    if not job or len(str(job)) > 128 or len(digest) != 64:
        raise ValueError('invalid_upload_identity')
    path = Path(data_dir) / NAME
    db = sqlite3.connect(str(path), timeout=2)
    try:
        db.executescript(SCHEMA)
        with db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT capture FROM uploads WHERE digest=? AND job=?',
                                  (digest, str(job))).fetchone()
            if capture and previous and previous[0]:
                old = json.loads(previous[0])
                if old.get('conflict') or (old.get('session_id'), old.get('game_no')) != (
                        capture.get('session_id'), capture.get('game_no')):
                    capture = {'conflict': True}
                else:
                    capture = dict(capture, captured_from_start=(
                        old.get('captured_from_start') is True or capture.get('captured_from_start') is True))
            db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(digest,job) DO UPDATE SET '
                       'capture=COALESCE(excluded.capture,uploads.capture)',
                       (digest, str(job), json.dumps(sorted(set(players)), ensure_ascii=False),
                        float(recorded), time.time(), json.dumps(capture) if capture else None))
            # Indexed, bounded cleanup; no full fact DB scan or in-memory history.
            db.execute('DELETE FROM uploads WHERE rowid IN (SELECT rowid FROM uploads '
                       'WHERE received<? ORDER BY received LIMIT 200)', (time.time()-15*86400,))
    finally:
        db.close()
        # A root-run historical repair must not leave the live ygo writer locked
        # out of a newly created ledger.
        facts = Path(data_dir) / 'bot.sqlite3'
        if hasattr(os, 'geteuid') and os.geteuid() == 0 and facts.is_file():
            owner = facts.stat()
            os.chown(path, owner.st_uid, owner.st_gid)


def metadata(facts_db, digests):
    path = facts_db.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        return {}
    ledger = Path(path).parent / NAME
    if not ledger.is_file():
        return {}
    db = sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
    found = {}
    try:
        values = list(set(digests))
        for start in range(0, len(values), 400):
            chunk = values[start:start+400]
            marks = ','.join('?' for _ in chunk)
            for digest, job, raw in db.execute(
                    'SELECT digest,job,capture FROM uploads WHERE digest IN (%s)' % marks, chunk):
                item = ('upload-job:'+job, 0, False)
                try:
                    capture = json.loads(raw) if raw else {}
                    number = capture.get('game_no')
                    session = capture.get('session_id')
                    if (type(number) is int and 1 <= number <= 20
                            and isinstance(session, str) and session):
                        item = (session, number, capture.get('captured_from_start') is True)
                except (ValueError, AttributeError):
                    pass
                found.setdefault(digest, set()).add(item)
    finally:
        db.close()
    # Conflicting tasks/explicit identities never get guessed into one match.
    return {d: next(iter(items)) if len(items) == 1 else ('unverified:'+d, 0, False)
            for d, items in found.items()}
