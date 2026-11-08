#!/usr/bin/env python3
"""Speed up card stats without changing output.

1) top100.eligible_views: cache the per-digest "replay file exists" result and
   the digest->rel_path map for 60 s (the check does ~8.4k filesystem stats per
   call; eligibility already runs on a 30 s snapshot).
2) card_statistics.query: only fetch opponent rows when an opponent filter is
   actually present (the app's default request has none).

Hash-gated; backs up both files; py_compile as ygo.
"""
import argparse
import hashlib
import os
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/opt/ygobot')
TOP = ROOT / 'bot/top100.py'
CARD = ROOT / 'bot/card_statistics.py'
BASE_TOP = '1288a8884dad1f4b962f76fc7d3783a5bf5195a3bc655b8de712211fb4a263d4'
BASE_CARD = '5d9dfb25db9855be9c2e6327d2c1288a685f9031ee7656c26ecbc59382859569'

TOP_IMPORT_ANCHOR = 'from . import statistics_quality\n'
TOP_IMPORT_ADD = """from . import statistics_quality
import threading

_ALIVE_CACHE = {}
_ALIVE_FILES = {'at': 0.0, 'paths': None}
_ALIVE_TTL = 60.0
_ALIVE_LOCK = threading.Lock()
"""

TOP_OLD = """    if check_files:
        file_paths={}
        for file_row in db.execute('SELECT f.digest,r.rel_path FROM stat_files f JOIN replays r ON r.id=f.replay_id'):
            file_paths.setdefault(file_row['digest'],[]).append(file_row['rel_path'])
        alive={}
        root_resolved=replay_root.resolve();dir_cache={}
        for row in rows:
            digest=row['digest']
            if digest not in alive:
                alive[digest]=any(safe_replay_file(replay_root,rel_path,root_resolved,dir_cache)
                                  for rel_path in file_paths.get(digest,()))
    else:
        alive={row['digest']: True for row in rows}
"""

TOP_NEW = """    if check_files:
        now_mono=time.monotonic()
        with _ALIVE_LOCK:
            cached_paths=_ALIVE_FILES['paths'] if now_mono-_ALIVE_FILES['at']<=_ALIVE_TTL else None
            cached_alive=dict(_ALIVE_CACHE)
        if cached_paths is None:
            cached_paths={}
            for file_row in db.execute('SELECT f.digest,r.rel_path FROM stat_files f JOIN replays r ON r.id=f.replay_id'):
                cached_paths.setdefault(file_row['digest'],[]).append(file_row['rel_path'])
            with _ALIVE_LOCK:
                _ALIVE_FILES.update(at=now_mono,paths=cached_paths)
        file_paths=cached_paths
        alive={}
        root_resolved=replay_root.resolve();dir_cache={}
        fresh={}
        for row in rows:
            digest=row['digest']
            if digest in alive:continue
            hit=cached_alive.get(digest)
            if hit is not None and 0.0<=now_mono-hit[0]<=_ALIVE_TTL:
                alive[digest]=hit[1];continue
            ok=any(safe_replay_file(replay_root,rel_path,root_resolved,dir_cache)
                   for rel_path in file_paths.get(digest,()))
            alive[digest]=ok;fresh[digest]=(now_mono,ok)
        if fresh:
            with _ALIVE_LOCK:
                _ALIVE_CACHE.update(fresh)
                if len(_ALIVE_CACHE)>40000:
                    _ALIVE_CACHE.clear();_ALIVE_CACHE.update(fresh)
    else:
        alive={row['digest']: True for row in rows}
"""

CARD_OLD = """    others = {}
    digests = sorted({row['digest'] for row in candidates})
    for start in range(0, len(digests), 400):
        chunk = digests[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        for r in db.execute('SELECT v.digest,v.player,v.build,b.deck_type,b.classification_source,b.series_status,b.main_complete,b.extra_complete '
                            'FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build '
                            'WHERE v.digest IN (' + marks + ')', chunk):
            others[(r['digest'], r['player'])] = dict(r)
    if f.opponent_series:
        annotate(db, list(others.values()), series_catalog)
    opponents={}
    for row in others.values():opponents.setdefault(row['digest'],[]).append(row)
"""

CARD_NEW = """    others = {}
    if f.opponent_types or f.opponent_series:
        digests = sorted({row['digest'] for row in candidates})
        for start in range(0, len(digests), 400):
            chunk = digests[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            for r in db.execute('SELECT v.digest,v.player,v.build,b.deck_type,b.classification_source,b.series_status,b.main_complete,b.extra_complete '
                                'FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build '
                                'WHERE v.digest IN (' + marks + ')', chunk):
                others[(r['digest'], r['player'])] = dict(r)
        if f.opponent_series:
            annotate(db, list(others.values()), series_catalog)
    opponents={}
    for row in others.values():opponents.setdefault(row['digest'],[]).append(row)
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_text(raw, old, new, label):
    text = raw.decode('utf8')
    crlf = '\r\n' in text
    norm = text.replace('\r\n', '\n')
    if norm.count(old) != 1:
        raise SystemExit('REFUSE: %s anchor count %d' % (label, norm.count(old)))
    norm = norm.replace(old, new)
    return (norm.replace('\n', '\r\n') if crlf else norm).encode('utf8')


def build_top(raw):
    text = raw.decode('utf8')
    crlf = '\r\n' in text
    norm = text.replace('\r\n', '\n')
    if norm.count(TOP_IMPORT_ANCHOR) != 1:
        raise SystemExit('REFUSE: top import anchor')
    norm = norm.replace(TOP_IMPORT_ANCHOR, TOP_IMPORT_ADD)
    if norm.count(TOP_OLD) != 1:
        raise SystemExit('REFUSE: top block anchor')
    norm = norm.replace(TOP_OLD, TOP_NEW)
    return (norm.replace('\n', '\r\n') if crlf else norm).encode('utf8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    args = ap.parse_args()
    backup = ROOT / 'data/config-backups/card-speed-20261108'

    if args.rollback:
        for name, target in (('top100.py', TOP), ('card_statistics.py', CARD)):
            found = sorted(backup.glob(name + '.*'))
            if not found:
                print('no backup for', name, file=sys.stderr)
                return 2
            shutil.copy2(found[-1], target)
            user = pwd.getpwnam('ygo')
            os.chown(target, user.pw_uid, user.pw_gid)
            subprocess.run(['sudo', '-u', 'ygo', 'python3', '-m', 'py_compile', str(target)], check=True)
            print('restored', target)
        print('rollback done; restart ygo-web')
        return 0

    if sha(TOP) != BASE_TOP:
        print('REFUSE top100 sha', sha(TOP), file=sys.stderr)
        return 3
    if sha(CARD) != BASE_CARD:
        print('REFUSE card_statistics sha', sha(CARD), file=sys.stderr)
        return 3

    new_top = build_top(TOP.read_bytes())
    new_card = patch_text(CARD.read_bytes(), CARD_OLD, CARD_NEW, 'card others')
    if args.dry_run:
        print('dry-run ok: top %d -> %d, card %d -> %d' % (
            TOP.stat().st_size, len(new_top), CARD.stat().st_size, len(new_card)))
        return 0

    backup.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(TOP, backup / ('top100.py.' + stamp))
    shutil.copy2(CARD, backup / ('card_statistics.py.' + stamp))
    TOP.write_bytes(new_top)
    CARD.write_bytes(new_card)
    user = pwd.getpwnam('ygo')
    for target in (TOP, CARD):
        os.chown(target, user.pw_uid, user.pw_gid)
        subprocess.run(['sudo', '-u', 'ygo', 'python3', '-m', 'py_compile', str(target)], check=True)
    print('top sha', sha(TOP))
    print('card sha', sha(CARD))
    print('backup', backup)
    print('NOTE: restart ygo-web')
    return 0


if __name__ == '__main__':
    sys.exit(main())
