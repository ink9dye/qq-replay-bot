"""Bounded disk-only raw report intermediates; eligibility is always recalculated."""
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

MAX_ENTRY = 4 * 1024 * 1024
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 24
MAX_RECORDS = 16000
MAX_AGE = 300
MIN_FREE = 2 * 1024 * 1024 * 1024


def _identity(db):
    database = db.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        return None
    # Selected rows are already qualified using current ranks, exclusions, time,
    # identity, and naming. The cache contains raw facts only, never that decision.
    keys = [tuple(r) for r in db.execute('SELECT digest,player FROM _report_selected ORDER BY digest,player')]
    versions = [tuple(r) for r in db.execute(
        'SELECT g.digest,COALESCE(r.revision,0) FROM stat_games g LEFT JOIN stat_fact_revisions r ON r.digest=g.digest '
        'WHERE g.digest IN (SELECT digest FROM _report_selected) ORDER BY g.digest')]
    if len(versions) != len({row[0] for row in keys}):
        return None  # old or not yet versioned facts are never persisted
    builds = [r[0] for r in db.execute('SELECT build FROM _report_builds ORDER BY build')]
    raw = json.dumps([1,keys,versions,builds],separators=(',',':')).encode()
    return Path(database).parent/'query-cache/card-report.sqlite3', hashlib.sha256(raw).hexdigest()


def _get(path, key, now, max_age=MAX_AGE):
    if not path.is_file():return None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as cache:
        row=cache.execute('SELECT payload FROM entries WHERE key=? AND created>?',(key,now-max_age)).fetchone()
    if row and len(row[0])<=MAX_ENTRY:
        value=json.loads(row[0])
        if isinstance(value,dict) and ((isinstance(value.get('copies'),list) and isinstance(value.get('facts'),list)) or 'response' in value):
            return value
    return None


def _put(path, key, value, now):
    # Fail open to normal SQL when the disk or available memory is constrained.
    parent=path.parent.parent
    if shutil.disk_usage(parent).free<MIN_FREE:return
    if os.name!='nt':
        available=next((int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')),0)
        if available<400*1024:return
    if len(value.get('copies',[]))+len(value.get('facts',[]))>MAX_RECORDS:return
    payload=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    if len(payload)>MAX_ENTRY:return
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(str(path),timeout=.1)) as cache, cache:
        os.chmod(path,0o600)
        cache.execute('PRAGMA max_page_count=24576')  # <=96MiB file; no VACUUM
        cache.execute('PRAGMA journal_size_limit=4194304')
        cache.execute('CREATE TABLE IF NOT EXISTS entries(key TEXT PRIMARY KEY,created REAL NOT NULL,payload BLOB NOT NULL)')
        cache.execute('CREATE INDEX IF NOT EXISTS entries_created ON entries(created)')
        cache.execute('DELETE FROM entries WHERE created<=?',(now-MAX_AGE,))
        cache.execute('INSERT OR REPLACE INTO entries VALUES(?,?,?)',(key,now,payload))
        sizes=cache.execute('SELECT key,length(payload) FROM entries ORDER BY created DESC').fetchall()
        total=0
        for index,(entry,size) in enumerate(sizes):
            total+=size
            if index>=MAX_ENTRIES or total>MAX_BYTES:
                cache.execute('DELETE FROM entries WHERE key=?',(entry,))


def raw_tables(db, loader):
    identity=None
    try:
        identity=_identity(db)
        if identity:
            value=_get(*identity,time.time())
            if value is not None:return value
    except (sqlite3.Error,OSError,ValueError,TypeError):
        identity=None
    value=loader()
    if identity:
        try:_put(*identity,value,time.time())
        except (sqlite3.Error,OSError,ValueError,TypeError):pass
    return value


def response_identity(database, key):
    """Exact DB/WAL/ref stamps; ten-second rank-profile/time bucket."""
    if not key or key[0]!='card-report':return None
    database=Path(database)
    paths=[database,Path(str(database)+'-wal')]+[database.parent/name for name in (
        'series-naming.json','card-series-reference.json','card-equivalence.json',
        'first-game-confirmations.sqlite3','watch-upload-sessions.sqlite3')]
    state=[]
    for path in paths:
        try:s=path.stat();state.append((path.name,s.st_mtime_ns,s.st_size))
        except FileNotFoundError:state.append((path.name,None,None))
    from urllib.parse import parse_qs
    normalized=parse_qs(key[1],keep_blank_values=True)
    encoded=json.dumps([2,str(database.resolve()),state,int(time.time()//10),normalized],sort_keys=True).encode()
    return database.parent/'query-cache/card-report.sqlite3','response:'+hashlib.sha256(encoded).hexdigest()


def response_get(identity, ttl):
    if identity is None:return None
    try:
        data=_get(*identity,time.time(),min(10,ttl))
        if data and 'response' in data:
            status,kind,body,headers=data['response']
            if status==200 and isinstance(body,str) and isinstance(headers,dict):
                return status,kind,body.encode('utf8'),headers
    except (sqlite3.Error,OSError,ValueError,TypeError):pass
    return None


def response_put(identity, result):
    if identity is None or result[0]!=200 or len(result[2])>MAX_ENTRY//2:return
    try:_put(*identity,dict(response=[result[0],result[1],result[2].decode('utf8'),result[3]]),time.time())
    except (sqlite3.Error,OSError,ValueError,TypeError):pass
