"""Preview or extend existing 14-day game deadlines; backup deadlines before writing."""
import argparse
import json
import sqlite3
from pathlib import Path


def extend(db, now, backup=None):
    rows = db.execute(
        'SELECT digest,expires_at FROM stat_games '
        'WHERE expires_at=received_at+? AND expires_at>?',
        (14 * 86400, now)).fetchall()
    if backup is None:
        return len(rows)
    # Exclusive creation prevents overwriting the rollback evidence.
    with Path(backup).open('x', encoding='utf-8') as output:
        json.dump(dict(old_deadlines=rows), output)
    db.executemany('UPDATE stat_games SET expires_at=expires_at+? '
                   'WHERE digest=? AND expires_at=?',
                   [(14 * 86400, digest, expiry) for digest, expiry in rows])
    return len(rows)


def main():
    import time
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--backup', type=Path,
                        help='Apply changes, saving old deadlines to this new JSON file')
    args = parser.parse_args()
    mode = 'rw' if args.backup else 'ro'
    with sqlite3.connect(args.database.resolve().as_uri() + '?mode=' + mode, uri=True) as db:
        if args.backup:
            db.execute('BEGIN IMMEDIATE')
        count = extend(db, time.time(), args.backup)
    print(('extended' if args.backup else 'would_extend'), count)


if __name__ == '__main__':
    main()
