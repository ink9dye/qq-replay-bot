"""Validate an audited identity manifest and publish a small independent ledger.

Default is read-only preview. This never changes capture completeness or facts.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.statistics_match_identity import capture_metadata
from bot.statistics_identity_confirmations import NAME


def validate(db, data, plan):
    rows = plan.get('records', [])
    if (plan.get('version') != 1 or plan.get('identity_only') is not True
            or plan.get('captured_from_start_modified') is not False
            or not 0 < len(rows) <= 1000):
        raise ValueError('invalid_plan')
    indexed = {r['digest']: r for r in rows}
    if len(indexed) != len(rows):
        raise ValueError('duplicate_digest')
    old = capture_metadata(db, indexed)
    slots = set()
    for row in rows:
        digest, first, number = row['digest'], row['first_digest'], row['number']
        if (len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                or type(number) is not int or not 1 <= number <= 20
                or row['session'] != 'verified-log:' + first or not row.get('evidence')):
            raise ValueError('invalid_identity')
        anchor = indexed.get(first)
        if not anchor or anchor['number'] != 1 or anchor['players'] != row['players']:
            raise ValueError('missing_or_conflicting_anchor')
        slot = (row['session'], number)
        if slot in slots:
            raise ValueError('conflicting_slot')
        slots.add(slot)
        actual = sorted({r[0] for r in db.execute('SELECT player FROM stat_views WHERE digest=?', (digest,))})
        if len(actual) != 2 or actual != row['players']:
            raise ValueError('players_changed:' + digest)
        native = old.get(digest)
        expected = row.get('previous_metadata')
        if (list(native) if native else None) != expected:
            raise ValueError('native_metadata_changed:' + digest)
        if native and (native[2] or (native[1] > 0 and native[1] != number)):
            raise ValueError('native_identity_conflict:' + digest)
        exists = False
        for (rel,) in db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f '
                                'ON f.replay_id=r.id WHERE f.digest=?', (digest,)):
            path = (data / 'replays' / rel).resolve()
            if (data / 'replays').resolve() not in path.parents:
                raise ValueError('invalid_source_path')
            if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
                if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    exists = True
                    break
        if not exists:
            raise ValueError('source_missing_or_hash_changed:' + digest)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    database = args.db.resolve()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        rows = validate(db, database.parent, plan)
    result = dict(applied=False, records=len(rows), first_games=sum(r['number'] == 1 for r in rows),
                  captured_from_start_modified=False, facts_modified=False)
    if args.apply:
        target = database.parent / NAME
        # Exclusive first publication; never overwrite other chats' confirmations.
        if target.exists():
            raise ValueError('ledger_already_exists')
        backup = database.parent / 'config-backups' / ('first-identity-' + str(time.time_ns()))
        backup.mkdir(parents=True, exist_ok=False)
        (backup / 'plan.json').write_bytes(args.plan.read_bytes())
        temporary = backup / NAME
        with closing(sqlite3.connect(str(temporary))) as ledger, ledger:
            ledger.execute('CREATE TABLE confirmations(digest TEXT PRIMARY KEY,session TEXT NOT NULL,'
                           'number INTEGER NOT NULL,first_digest TEXT NOT NULL,players TEXT NOT NULL,'
                           'proof TEXT NOT NULL,UNIQUE(session,number))')
            ledger.executemany('INSERT INTO confirmations VALUES(?,?,?,?,?,?)', [
                (r['digest'], r['session'], r['number'], r['first_digest'],
                 json.dumps(r['players'], ensure_ascii=False), json.dumps(r['evidence'], ensure_ascii=False))
                for r in rows])
        owner = database.stat()
        if hasattr(os, 'chown'):
            os.chown(temporary, owner.st_uid, owner.st_gid)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        # Only cache invalidation in the main DB. No replay/evidence/view edits.
        with closing(sqlite3.connect(str(database), timeout=2)) as db, db:
            db.execute('UPDATE stat_classification_state SET revision=revision+1 WHERE singleton=1')
        result.update(applied=True, backup=str(backup), ledger_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    print(json.dumps(result))


if __name__ == '__main__':
    main()
