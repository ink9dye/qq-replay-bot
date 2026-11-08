#!/usr/bin/env python3
"""Cap card-metrics 'samples' to 200 by default; full=1 keeps everything.

Server-side only: the Android client ignores 'samples', and the web card page
will pass full=1. Hash-gated against the current production files.
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
JS = ROOT / 'bot/statistics_card_metrics.js'
BASE_PORTAL = 'a1c18377797ed6a7498134b0201c665a7b0851cb5ccb97d07bd98aedb7c3b794'
BASE_JS = '5d0c3ad39ab1e5bad61cb8f4c9d435de9c90736cde1c5add81d58d1d4216fe89'

CONST_ANCHOR = 'QUERY_CACHE_MAX_BYTES = 8 * 1024 * 1024\n'
CONST_ADD = 'CARD_SAMPLES_LIMIT = 200\n'

SORT_ANCHOR = ("result['generated_at']=snapshot_now;"
               "result['samples'].sort(key=lambda s:s['recorded_at'],reverse=True)\n")
SORT_NEW = (SORT_ANCHOR +
            "                if args.get('full',['0'])[0]!='1' and len(result['samples'])>CARD_SAMPLES_LIMIT:\n"
            "                    result['samples']=result['samples'][:CARD_SAMPLES_LIMIT]\n"
            "                    result['samples_truncated']=True\n")

JS_ANCHOR = "new URLSearchParams({code,partition:"
JS_NEW = "new URLSearchParams({code,full:'1',partition:"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_portal(raw):
    text = raw.decode('utf8')
    crlf = '\r\n' in text
    norm = text.replace('\r\n', '\n')
    if norm.count(CONST_ANCHOR) != 1:
        raise SystemExit('REFUSE: const anchor')
    norm = norm.replace(CONST_ANCHOR, CONST_ANCHOR + CONST_ADD)
    if norm.count(SORT_ANCHOR) != 1:
        raise SystemExit('REFUSE: sort anchor')
    norm = norm.replace(SORT_ANCHOR, SORT_NEW)
    return (norm.replace('\n', '\r\n') if crlf else norm).encode('utf8')


def build_js(raw):
    text = raw.decode('utf8')
    if text.count(JS_ANCHOR) != 1:
        raise SystemExit('REFUSE: js anchor')
    return text.replace(JS_ANCHOR, JS_NEW).encode('utf8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    args = ap.parse_args()
    backup = ROOT / 'data/config-backups/card-samples-20261108'

    if args.rollback:
        for name, target in (('statistics_portal.py', PORTAL), ('statistics_card_metrics.js', JS)):
            found = sorted(backup.glob(name + '.*'))
            if not found:
                print('no backup for', name, file=sys.stderr)
                return 2
            shutil.copy2(found[-1], target)
            user = pwd.getpwnam('ygo')
            os.chown(target, user.pw_uid, user.pw_gid)
            print('restored', target, 'from', found[-1])
        subprocess.run(['sudo', '-u', 'ygo', 'python3', '-m', 'py_compile', str(PORTAL)], check=True)
        print('rollback done; restart ygo-web')
        return 0

    if sha(PORTAL) != BASE_PORTAL:
        print('REFUSE portal sha', sha(PORTAL), file=sys.stderr)
        return 3
    if sha(JS) != BASE_JS:
        print('REFUSE js sha', sha(JS), file=sys.stderr)
        return 3

    new_portal = build_portal(PORTAL.read_bytes())
    new_js = build_js(JS.read_bytes())
    if args.dry_run:
        print('dry-run ok: portal %d -> %d, js %d -> %d' % (
            PORTAL.stat().st_size, len(new_portal), JS.stat().st_size, len(new_js)))
        return 0

    backup.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(PORTAL, backup / ('statistics_portal.py.' + stamp))
    shutil.copy2(JS, backup / ('statistics_card_metrics.js.' + stamp))
    PORTAL.write_bytes(new_portal)
    JS.write_bytes(new_js)
    user = pwd.getpwnam('ygo')
    os.chown(PORTAL, user.pw_uid, user.pw_gid)
    os.chown(JS, user.pw_uid, user.pw_gid)
    # compile as ygo so __pycache__ stays writable by the service user
    subprocess.run(['sudo', '-u', 'ygo', 'python3', '-m', 'py_compile', str(PORTAL)], check=True)
    print('portal sha', sha(PORTAL))
    print('js sha', sha(JS))
    print('backup', backup)
    print('NOTE: restart ygo-web to load the portal change; JS needs Ctrl+F5 (max-age 3600).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
