#!/usr/bin/env python3
"""Apply the mem2g roster patch to bot/statistics_portal.py.

Hash-gated: refuses if the live file is not the reviewed base (another
conversation changed it). Keeps the file's CRLF line endings. Writes a
timestamped backup before touching anything.

Usage:
  python3 03-apply.py [--root /opt/ygobot] [--dry-run]
  python3 03-apply.py --rollback [--root /opt/ygobot]
"""
import argparse
import hashlib
import py_compile
import shutil
import sys
import time
from pathlib import Path

BASE_SHA = 'a4e2552b7e1c08c02fe536813d282fbb456e703e6771a369c49c138e8d9decff'

CONST_ANCHOR = "QUERY_CACHE_MAX_BYTES = 8 * 1024 * 1024\n"
CONST_ADD = '''PRECOMPUTED_ROSTER_SECONDS = 900.0


def _trim_heap():
    """Return freed arenas to the OS after a heavy build (glibc only)."""
    try:
        import ctypes
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except (OSError, AttributeError):
        pass
'''

OLD_ROSTER = '''    def _roster_reply(self, force=False, background=False):
        """榜首查询的唯一入口：route 与后台保活共用，保证缓存键一致。"""
        from .statistics_dashboard import query
'''

NEW_ROSTER = '''    def _precomputed_roster(self):
        """Serve the roster built by tools/roster_precompute.py.

        Keeps the 30-90 s build and its 250-300 MB intermediate out of this
        process, so _query_compute_lock is not held and query 503s drop.
        """
        try:
            path = self.database.parent / 'cache' / 'roster.json'
            item = json.loads(path.read_text(encoding='utf8'))
            saved = float(item['saved'])
            if not 0.0 <= float(self.clock()) - saved <= PRECOMPUTED_ROSTER_SECONDS:
                return None
            if int(item['status']) != 200:
                return None
            body = str(item['body'])
            if not body or len(body) > 4 * 1024 * 1024:
                return None
            headers = dict(item.get('headers') or {})
            headers['X-Statistics-State'] = 'precomputed'
            headers['X-Statistics-Saved-At'] = str(int(saved))
            return 200, str(item['kind']), body.encode('utf8'), headers
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _roster_reply(self, force=False, background=False):
        """榜首查询的唯一入口：route 与后台保活共用，保证缓存键一致。"""
        if not force:
            precomputed = self._precomputed_roster()
            if precomputed is not None:
                return precomputed
        from .statistics_dashboard import query
'''

OLD_RESULT = "        result=self._query_reply(callback)\n"
NEW_RESULT = ("        result=self._query_reply(callback)\n"
              "        if key and key[0] in ('roster','matrix','card-report','recordings'):\n"
              "            _trim_heap()\n")


def build(text: str) -> str:
    """Return the patched text, preserving the input's line endings."""
    crlf = '\r\n' in text
    norm = text.replace('\r\n', '\n')
    if norm.count(CONST_ANCHOR) != 1:
        raise SystemExit('REFUSE: constant anchor not found exactly once')
    norm = norm.replace(CONST_ANCHOR, CONST_ANCHOR + CONST_ADD)
    if norm.count(OLD_ROSTER) != 1:
        raise SystemExit('REFUSE: _roster_reply anchor not found exactly once')
    norm = norm.replace(OLD_ROSTER, NEW_ROSTER)
    if norm.count(OLD_RESULT) != 1:
        raise SystemExit('REFUSE: result anchor not found exactly once')
    norm = norm.replace(OLD_RESULT, NEW_RESULT)
    return norm.replace('\n', '\r\n') if crlf else norm


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='/opt/ygobot')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    root = Path(args.root)
    target = root / 'bot' / 'statistics_portal.py'
    backup_dir = root / 'data' / 'config-backups' / 'mem2g-20261107'

    if args.rollback:
        backups = sorted(backup_dir.glob('statistics_portal.py.*'))
        if not backups:
            print(f'no backup in {backup_dir}', file=sys.stderr)
            return 2
        latest = backups[-1]
        shutil.copy2(latest, target)
        py_compile.compile(str(target), doraise=True)
        print(f'restored {latest} -> {target}')
        print('NOTE: restart ygo-web to load it.')
        return 0

    raw = target.read_bytes()
    have = hashlib.sha256(raw).hexdigest()
    if have != BASE_SHA:
        print(f'REFUSE: {target} sha256={have}', file=sys.stderr)
        print(f'expected {BASE_SHA}; another change landed, rebase first.', file=sys.stderr)
        return 3

    text = raw.decode('utf8')
    out = build(text)
    if out == text:
        print('no textual change produced', file=sys.stderr)
        return 4
    if args.dry_run:
        print(f'dry-run ok: {len(raw)} -> {len(out.encode("utf8"))} bytes')
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = backup_dir / f'statistics_portal.py.{stamp}'
    shutil.copy2(target, backup)
    target.write_bytes(out.encode('utf8'))
    py_compile.compile(str(target), doraise=True)
    print(f'backup: {backup}')
    print(f'new sha256: {hashlib.sha256(target.read_bytes()).hexdigest()}')
    print('NOTE: restart ygo-web to load the patch (quiet window / user consent).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
