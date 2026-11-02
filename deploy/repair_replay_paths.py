#!/usr/bin/env python3
"""Repair replay rel_path values against the files actually on disk.

Strategy per row:
1. normalize backslashes
2. if the file exists, keep it
3. otherwise look for a unique file with the same basename anywhere under replays
4. otherwise try cp866 -> utf-8 unmojibake
Reports how many rows were fixed or remain missing.
"""
import sqlite3
from pathlib import Path

ROOT = Path('/opt/ygobot/data/replays')
DB = '/opt/ygobot/data/bot.sqlite3'


def unmojibake(text):
    try:
        fixed = text.encode('cp866').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return fixed if fixed != text else None


def main():
    db = sqlite3.connect(DB)
    rows = db.execute('SELECT rowid, rel_path FROM replays').fetchall()

    index = {}
    for path in ROOT.rglob('*.yrp'):
        index.setdefault(path.name, []).append(path.relative_to(ROOT).as_posix())

    fixed = 0
    missing = 0
    for rid, original in rows:
        rel = str(original).replace('\\', '/')
        target = ROOT / rel

        if target.is_file():
            if rel != original:
                db.execute('UPDATE replays SET rel_path=? WHERE rowid=?', (rel, rid))
                fixed += 1
            continue

        base = Path(rel).name
        candidates = index.get(base, [])
        if len(candidates) == 1:
            db.execute('UPDATE replays SET rel_path=? WHERE rowid=?', (candidates[0], rid))
            fixed += 1
            continue

        alt = unmojibake(rel)
        if alt:
            alt_target = ROOT / alt
            if alt_target.is_file():
                db.execute('UPDATE replays SET rel_path=? WHERE rowid=?', (alt, rid))
                fixed += 1
                continue
            alt_base = Path(alt).name
            alt_candidates = index.get(alt_base, [])
            if len(alt_candidates) == 1:
                db.execute('UPDATE replays SET rel_path=? WHERE rowid=?', (alt_candidates[0], rid))
                fixed += 1
                continue

        missing += 1

    db.commit()
    print('fixed:', fixed, 'still_missing:', missing, 'total:', len(rows))


if __name__ == '__main__':
    main()
