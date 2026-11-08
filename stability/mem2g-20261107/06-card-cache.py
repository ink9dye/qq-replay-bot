#!/usr/bin/env python3
"""Cache card-metrics replies in the portal process (TTL 600 s, keyed by query).

The endpoint takes 28-48 s cold, while query_core's HTTP timeout is 8 s, so the
app currently gets metrics=null on every cold call. With this cache every
distinct query string is computed once; later calls (including query_core's
5-minute refresh) return instantly.

Portal-only change: no collector restart, no Android change.
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
PORTAL = ROOT / 'bot/statistics_portal.py'
BASE = 'ffa0ce7d9c3fce5d7796fdc0d899cfc5c8a4240740ff5b88b770624a61738e18'

INIT_ANCHOR = "        self._query_cache={}\n        self._query_cache_lock=threading.Lock()\n"
INIT_ADD = ("        self._card_metrics_cache={}\n"
            "        self._card_metrics_lock=threading.Lock()\n")

BUSY_ANCHOR = "    def _query_busy_reply(self):\n"
METHODS = """    def _card_metrics_cache_get(self,key,ttl=CARD_METRICS_CACHE_SECONDS):
        now=time.monotonic()
        with self._card_metrics_lock:
            item=self._card_metrics_cache.get(key)
            if item is not None and 0.0<=now-item[0]<=ttl:
                return item[1]
            if item is not None:
                self._card_metrics_cache.pop(key,None)
        return None

    def _card_metrics_cache_put(self,key,reply,limit=32,max_bytes=12*1024*1024):
        body=reply[2] if len(reply)>2 else b''
        size=len(body) if isinstance(body,(bytes,bytearray)) else 0
        if size>max_bytes//4:return
        with self._card_metrics_lock:
            self._card_metrics_cache[key]=(time.monotonic(),reply,size)
            total=sum(item[2] for item in self._card_metrics_cache.values())
            while len(self._card_metrics_cache)>limit or (total>max_bytes and len(self._card_metrics_cache)>1):
                oldest=min(self._card_metrics_cache,key=lambda k:self._card_metrics_cache[k][0])
                total-=self._card_metrics_cache.pop(oldest)[2]

"""

RETURN_ANCHOR = "            return self._query_reply(lambda db:scoped_query(ranges,lambda:run(db)))\n"
RETURN_NEW = """            cached=self._card_metrics_cache_get(parsed.query)
            if cached is not None:
                return cached
            reply=self._query_reply(lambda db:scoped_query(ranges,lambda:run(db)))
            if reply[0]==200:
                self._card_metrics_cache_put(parsed.query,reply)
            _trim_heap()
            return reply
"""

CONST_ANCHOR = "CARD_SAMPLES_LIMIT = 200\n"
CONST_ADD = "CARD_METRICS_CACHE_SECONDS = 600.0\n"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(raw):
    text = raw.decode('utf8')
    crlf = '\r\n' in text
    norm = text.replace('\r\n', '\n')
    for anchor, label in ((INIT_ANCHOR, 'init'), (BUSY_ANCHOR, 'busy'),
                          (RETURN_ANCHOR, 'return'), (CONST_ANCHOR, 'const')):
        if norm.count(anchor) != 1:
            raise SystemExit('REFUSE: %s anchor count %d' % (label, norm.count(anchor)))
    norm = norm.replace(CONST_ANCHOR, CONST_ANCHOR + CONST_ADD)
    norm = norm.replace(INIT_ANCHOR, INIT_ANCHOR + INIT_ADD)
    norm = norm.replace(BUSY_ANCHOR, METHODS + BUSY_ANCHOR)
    norm = norm.replace(RETURN_ANCHOR, RETURN_NEW)
    return (norm.replace('\n', '\r\n') if crlf else norm).encode('utf8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    args = ap.parse_args()
    backup = ROOT / 'data/config-backups/card-metrics-cache-20261108'

    if args.rollback:
        found = sorted(backup.glob('statistics_portal.py.*'))
        if not found:
            print('no backup', file=sys.stderr)
            return 2
        shutil.copy2(found[-1], PORTAL)
        user = pwd.getpwnam('ygo')
        os.chown(PORTAL, user.pw_uid, user.pw_gid)
        subprocess.run(['sudo', '-u', 'ygo', 'python3', '-m', 'py_compile', str(PORTAL)], check=True)
        print('restored', PORTAL, 'from', found[-1], '; restart ygo-web')
        return 0

    if sha(PORTAL) != BASE:
        print('REFUSE portal sha', sha(PORTAL), file=sys.stderr)
        return 3

    out = build(PORTAL.read_bytes())
    if args.dry_run:
        print('dry-run ok: %d -> %d bytes' % (PORTAL.stat().st_size, len(out)))
        return 0

    backup.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(PORTAL, backup / ('statistics_portal.py.' + stamp))
    PORTAL.write_bytes(out)
    user = pwd.getpwnam('ygo')
    os.chown(PORTAL, user.pw_uid, user.pw_gid)
    subprocess.run(['sudo', '-u', 'ygo', 'python3', '-m', 'py_compile', str(PORTAL)], check=True)
    print('new sha', sha(PORTAL))
    print('backup', backup)
    print('NOTE: restart ygo-web')
    return 0


if __name__ == '__main__':
    sys.exit(main())
