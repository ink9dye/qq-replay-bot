"""Bounded, monotonic replay sidecar updates. No engine replay or new worker."""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


def decode(body):
    value = json.loads(body)
    if (not isinstance(value, dict) or not isinstance(value.get('digest'), str)
            or len(value['digest']) != 64
            or any(c not in '0123456789abcdef' for c in value['digest'])
            or not isinstance(value.get('players'), list) or len(value['players']) != 2
            or any(not isinstance(p, str) or not p or len(p) > 128 for p in value['players'])):
        raise ValueError('invalid_replay_evidence')
    if 'winner' in value and value['winner'] is not None and (
            type(value['winner']) is not int or value['winner'] not in (0, 1, 2)):
        raise ValueError('invalid_winner')
    if value.get('game_no') is not None and (type(value['game_no']) is not int or not 0 <= value['game_no'] <= 20):
        raise ValueError('invalid_game_number')
    if value.get('session_id') is not None and (not isinstance(value['session_id'], str) or len(value['session_id']) > 128):
        raise ValueError('invalid_session')
    if value.get('captured_from_start') is not None and type(value['captured_from_start']) is not bool:
        raise ValueError('invalid_capture_flag')
    return value


def valid_replay(path, evidence, maximum):
    if not path.is_file() or path.stat().st_size > maximum:
        return False
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != evidence['digest']:
        raise ValueError('evidence_digest_mismatch')
    from .watch_core import decks_from_replay
    if [p[0] for p in decks_from_replay(blob)] != evidence['players']:
        raise ValueError('evidence_player_order_mismatch')
    return True


def locate(app, desired, evidence):
    if desired.is_file():
        valid_replay(desired, evidence, app.max_upload)
        return desired
    database = app.replay_root.parent/'bot.sqlite3'
    if database.is_file():
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=2) as db:
            for (relative,) in db.execute('SELECT rel_path FROM replays WHERE digest=? LIMIT 8', (evidence['digest'],)):
                candidate = (app.replay_root/relative).resolve()
                if candidate.is_relative_to(app.replay_root.resolve()) and candidate.is_file():
                    valid_replay(candidate, evidence, app.max_upload)
                    return candidate
    return desired


def merge(previous, incoming):
    if not previous:
        return incoming
    if previous['digest'] != incoming['digest'] or previous['players'] != incoming['players']:
        raise ValueError('evidence_identity_conflict')
    for key in ('winner', 'session_id', 'game_no'):
        old, new = previous.get(key), incoming.get(key)
        if key == 'game_no' and old == 0:
            continue
        if old not in (None, '') and new not in (None, '') and old != new:
            raise ValueError('evidence_conflict_'+key)
    result = dict(previous)
    for key, value in incoming.items():
        if value is None or (key == 'events' and not value):
            continue
        if key == 'captured_from_start':
            value = value or previous.get(key) is True
        result[key] = value
    # Incomplete retries cannot replace an already complete event journal.
    if previous.get('captured_from_start') and not incoming.get('captured_from_start'):
        for key in ('events', 'draw_capture_version'):
            if key in previous:
                result[key] = previous[key]
    return result


def write(path, value):
    content = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    if path.is_file() and path.read_bytes() == content:
        return False
    temp = path.with_name(path.name+'.evidence-tmp')
    temp.write_bytes(content)
    os.replace(temp, path)
    return True


def register_and_refresh(app, path, job_id, players, recorded, value):
    if not valid_replay(path, value, app.max_upload):
        return 'awaiting_replay'
    from .watch_upload_sessions import record
    record(app.replay_root.parent, value['digest'], job_id, players, recorded,
           {k: value.get(k) for k in ('session_id', 'game_no', 'captured_from_start')})
    database = app.replay_root.parent/'bot.sqlite3'
    if not database.is_file():
        return 'facts_pending'
    from .statistics_import import load_report
    from .statistics_facts import import_replay
    # Parse only this small replay/report before acquiring any write transaction.
    report = load_report(path)
    db = sqlite3.connect(database, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute('SELECT * FROM replays WHERE digest=? AND rel_path=? LIMIT 1',
                         (value['digest'], path.relative_to(app.replay_root).as_posix())).fetchone()
        if row is None:
            return 'facts_pending'
        with db:
            import_replay(db, dict(row), path, report, time.time())
        return 'facts_updated'
    finally:
        db.close()
