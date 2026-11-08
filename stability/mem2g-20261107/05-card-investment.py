#!/usr/bin/env python3
"""Skip the 'investment' block for query-API card responses (Android never reads it).

The web card page still gets it:
  * new JS sends full=1 -> keep;
  * old cached JS has no full -> keep because its User-Agent is a browser;
  * query_core uses Python-urllib -> skip the (expensive, unused) block.

Hash-gated against the current production file.
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
BASE = 'df6f8d5017a076d36f619d757679ef0792014d541b3e853e04d03ef2212a10f8'

OLD = """            def run(db):
                from .statistics_investment import query as player_investment
                snapshot_now=float(self.clock())
                result=card_query(db,self.replay_root,code,partition,snapshot_now,filters)
                result['investment']=player_investment(db,self.replay_root,code,snapshot_now,filters.deck_types,filters.series,basis=investment_basis)
"""

NEW = """            full_request=(args.get('full',['0'])[0]=='1'
                          or 'Python-urllib' not in (headers.get('User-Agent') or ''))
            def run(db):
                from .statistics_investment import query as player_investment
                snapshot_now=float(self.clock())
                result=card_query(db,self.replay_root,code,partition,snapshot_now,filters)
                if full_request:
                    result['investment']=player_investment(db,self.replay_root,code,snapshot_now,filters.deck_types,filters.series,basis=investment_basis)
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    args = ap.parse_args()
    backup = ROOT / 'data/config-backups/card-investment-20261108'

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

    raw = PORTAL.read_bytes()
    text = raw.decode('utf8')
    crlf = '\r\n' in text
    norm = text.replace('\r\n', '\n')
    if norm.count(OLD) != 1:
        print('REFUSE: investment anchor not found exactly once', file=sys.stderr)
        return 3
    norm = norm.replace(OLD, NEW)
    out = (norm.replace('\n', '\r\n') if crlf else norm).encode('utf8')

    if args.dry_run:
        print('dry-run ok: %d -> %d bytes' % (len(raw), len(out)))
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
