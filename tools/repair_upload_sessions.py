"""Preview or apply an explicit, bounded log-derived prefix repair manifest.

Only erroneous prefix associations are removed. Upload jobs provide grouping,
never invented first-game numbers. The original fact/recording rows stay intact.
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.watch_upload_sessions import record


def checked_rows(db, plan):
    if plan.get('version') != 1 or not 0 < len(plan.get('rows', [])) <= 1000:
        raise ValueError('invalid_or_unbounded_plan')
    found = []
    seen = set()
    for item in plan['rows']:
        replay_id = item['replay_id']
        if replay_id in seen:
            raise ValueError('duplicate_replay')
        seen.add(replay_id)
        row = db.execute('SELECT r.id,r.digest,r.rel_path,r.recorded_at,s.session_id,s.game_no '
                         'FROM replays r JOIN replay_sessions s ON s.replay_id=r.id WHERE r.id=?',
                         (replay_id,)).fetchone()
        expected = (replay_id, item['digest'], item['rel_path'], item['recorded_at'],
                    item['before_session'], item['before_game_no'])
        if not row or tuple(row) != expected or not row[4].startswith('prefix:'):
            raise ValueError('before_identity_changed:%s' % replay_id)
        players = sorted({r[0] for r in db.execute('SELECT player FROM stat_views WHERE digest=?', (row[1],))})
        if len(players) != 2 or players != sorted(item['players']):
            raise ValueError('player_pair_changed:%s' % replay_id)
        if not isinstance(item['job'], str) or not item['job'] or len(item['job']) > 128:
            raise ValueError('invalid_job')
        for e in db.execute("SELECT json_extract(payload_json,'$.capture') FROM stat_evidence WHERE digest=?", (row[1],)):
            capture = json.loads(e[0]) if e[0] else None
            if isinstance(capture, dict) and ('game_no' in capture or 'session_id' in capture):
                raise ValueError('explicit_capture_exists:%s' % replay_id)
        found.append(dict(item))
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8-sig'))
    database = args.db.resolve()
    # Default preview never opens a writable connection.
    db = sqlite3.connect(str(database) if args.apply else database.as_uri()+'?mode=ro',
                         uri=not args.apply, timeout=3)
    try:
        db.execute('BEGIN IMMEDIATE' if args.apply else 'BEGIN')
        rows = checked_rows(db, plan)
        if args.apply:
            backup = database.parent / 'config-backups' / ('session-repair-'+str(time.time_ns())+'.json')
            backup.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive create; exact affected rows only, no full DB or WAL copy.
            with backup.open('x', encoding='utf-8') as handle:
                json.dump(dict(version=1, rows=rows), handle, ensure_ascii=False, indent=2)
            for row in rows:
                record(database.parent, row['digest'], row['job'], row['players'], row['recorded_at'])
                db.execute('DELETE FROM replay_sessions WHERE replay_id=? AND session_id=? AND game_no=?',
                           (row['replay_id'], row['before_session'], row['before_game_no']))
            db.execute('UPDATE stat_classification_state SET revision=revision+1 WHERE singleton=1')
            db.commit()
        else:
            db.rollback()
        print(json.dumps(dict(applied=args.apply, rows=len(rows),
                              jobs=len({r['job'] for r in rows}),
                              first_games_invented=0,
                              backup=str(backup) if args.apply else None), ensure_ascii=False))
    finally:
        db.close()


if __name__ == '__main__':
    main()
