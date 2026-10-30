"""可重算的单小局事实；此模块不提供前100汇总或改变QQ查询权限。"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from retention_policy import RETENTION_SECONDS


SCHEMA = '''
CREATE TABLE IF NOT EXISTS stat_games (
 digest TEXT PRIMARY KEY, recorded_at REAL NOT NULL, received_at REAL NOT NULL,
 expires_at REAL NOT NULL, report_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stat_files (
 replay_id INTEGER PRIMARY KEY REFERENCES replays(id) ON DELETE CASCADE,
 digest TEXT NOT NULL REFERENCES stat_games(digest) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS stat_files_digest ON stat_files(digest);
CREATE TABLE IF NOT EXISTS stat_builds (
 fingerprint TEXT PRIMARY KEY, main_complete INTEGER NOT NULL,
 extra_complete INTEGER NOT NULL, side_complete INTEGER NOT NULL DEFAULT 0,
 deck_type TEXT, classification_version TEXT
);
CREATE TABLE IF NOT EXISTS stat_build_cards (
 build TEXT NOT NULL REFERENCES stat_builds(fingerprint) ON DELETE CASCADE,
 partition TEXT NOT NULL CHECK(partition IN ('main','side','extra')),
 code INTEGER NOT NULL, copies INTEGER NOT NULL CHECK(copies > 0),
 PRIMARY KEY(build,partition,code)
);
CREATE TABLE IF NOT EXISTS stat_views (
 digest TEXT NOT NULL REFERENCES stat_games(digest) ON DELETE CASCADE,
 player TEXT NOT NULL, player_index INTEGER NOT NULL,
 build TEXT NOT NULL REFERENCES stat_builds(fingerprint),
 outcome TEXT NOT NULL DEFAULT 'unknown', result_source TEXT,
 first_player INTEGER, usage_complete INTEGER NOT NULL DEFAULT 0,
 draw_complete INTEGER NOT NULL DEFAULT 0, report_version INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(digest,player), UNIQUE(digest,player_index)
);
CREATE INDEX IF NOT EXISTS stat_views_player ON stat_views(player);
CREATE TABLE IF NOT EXISTS stat_card_facts (
 digest TEXT NOT NULL, player TEXT NOT NULL,
 partition TEXT NOT NULL CHECK(partition IN ('main','side','extra','other')),
 code INTEGER NOT NULL, copies INTEGER NOT NULL,
 drawn INTEGER, draw_count INTEGER, initial_count INTEGER,
 used INTEGER, use_count INTEGER, used_after_draw INTEGER,
 appeared INTEGER, appearance_count INTEGER,
 PRIMARY KEY(digest,player,partition,code),
 FOREIGN KEY(digest,player) REFERENCES stat_views(digest,player) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stat_exclusions (
 digest TEXT NOT NULL REFERENCES stat_games(digest) ON DELETE CASCADE,
 player TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'default',
 reason TEXT NOT NULL, PRIMARY KEY(digest,player,scope)
);
CREATE TABLE IF NOT EXISTS stat_evidence (
 digest TEXT NOT NULL REFERENCES stat_games(digest) ON DELETE CASCADE,
 kind TEXT NOT NULL, source TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(digest,kind)
);
CREATE TRIGGER IF NOT EXISTS stat_last_file_removed AFTER DELETE ON stat_files
BEGIN
 DELETE FROM stat_games WHERE digest=OLD.digest
 AND NOT EXISTS(SELECT 1 FROM stat_files WHERE digest=OLD.digest);
END;
CREATE TRIGGER IF NOT EXISTS stat_unused_build_removed AFTER DELETE ON stat_views
BEGIN
 DELETE FROM stat_builds WHERE fingerprint=OLD.build
 AND NOT EXISTS(SELECT 1 FROM stat_views WHERE build=OLD.build);
END;
'''


def initialize(db):
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    db.commit()


def build_fingerprint(main, extra):
    # side未在录像中完整公开；未知不能用空side冒充已确认0张。
    value = dict(main=sorted(Counter(main).items()), extra=sorted(Counter(extra).items()),
                 main_complete=bool(main) and all(type(c) is int and c > 0 for c in main),
                 extra_complete=all(type(c) is int and c > 0 for c in extra), side_complete=False)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _number(value):
    return value if type(value) is int and value >= 0 else 0


def _index(items):
    return {item['code']: item for item in items if isinstance(item, dict)
            and type(item.get('code')) is int and item['code'] > 0}


def import_replay(db, replay, path, report=None, now=None):
    """调用方持有Store锁及事务。重复来源只增加文件引用，不增加玩家/卡样本。"""
    from .watch_core import decks_from_replay

    now = time.time() if now is None else float(now)
    path = Path(path)
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != replay['digest']:
        raise ValueError('录像指纹与索引不符')
    source = db.execute('SELECT * FROM replays WHERE id=? AND digest=?',
                        (replay['id'], digest)).fetchone()
    if source is None:
        raise ValueError('录像索引不存在')
    earliest = db.execute('SELECT MIN(saved_at) FROM replays WHERE digest=?', (digest,)).fetchone()[0]
    received = float(earliest)
    expires = received + RETENTION_SECONDS
    existing = db.execute('SELECT * FROM stat_games WHERE digest=?', (digest,)).fetchone()
    if existing:
        received = min(received, existing['received_at'])
        expires = min(expires, existing['expires_at'])
    if now >= expires:
        db.execute('DELETE FROM stat_games WHERE digest=?', (digest,))
        return False
    decks = decks_from_replay(blob)
    if (len(decks) != 2 or len({d[0] for d in decks}) != 2
            or any(not d[0] or d[0] == '[Server]' for d in decks)):
        return False
    names = [d[0] for d in decks]
    if report is not None:
        if (report.get('digest') != digest or [p.get('name') for p in report.get('players', [])] != names
                or any(Counter(p.get('main', [])) != Counter(d[1])
                       or Counter(p.get('extra', [])) != Counter(d[2])
                       for p, d in zip(report['players'], decks))):
            raise ValueError('解析结果与录像身份或构筑不符')
    version = _number((report or {}).get('version'))
    db.execute('INSERT INTO stat_games(digest,recorded_at,received_at,expires_at) VALUES(?,?,?,?) '
               'ON CONFLICT(digest) DO UPDATE SET received_at=MIN(received_at,excluded.received_at),'
               'expires_at=MIN(expires_at,excluded.expires_at)',
               (digest, source['recorded_at'], received, expires))
    db.execute('INSERT OR IGNORE INTO stat_files(replay_id,digest) VALUES(?,?)', (replay['id'], digest))
    for index, (name, main, extra) in enumerate(decks):
        build = build_fingerprint(main, extra)
        db.execute('INSERT OR IGNORE INTO stat_builds(fingerprint,main_complete,extra_complete) VALUES(?,?,?)',
                   (build, int(bool(main) and all(type(c) is int and c > 0 for c in main)),
                    int(all(type(c) is int and c > 0 for c in extra))))
        for partition, codes in [('main', main), ('extra', extra)]:
            db.executemany('INSERT OR IGNORE INTO stat_build_cards VALUES(?,?,?,?)',
                          [(build, partition, code, count) for code, count in Counter(codes).items()
                           if type(code) is int and code > 0])
        db.execute('INSERT OR IGNORE INTO stat_views(digest,player,player_index,build) VALUES(?,?,?,?)',
                   (digest, name, index, build))
        old = dict(db.execute('SELECT * FROM stat_views WHERE digest=? AND player=?', (digest, name)).fetchone())
        if report is not None and version < old['report_version']:
            continue
        player = report['players'][index] if report is not None else {}
        usage_complete = bool(report and report.get('playback') in ('completed', 'responses_exhausted', 'live_complete')
                              and isinstance(player.get('used_cards'), list)
                              and isinstance(player.get('extra_appearances'), list))
        draw_complete = player.get('draw_record_complete') is True and isinstance(player.get('drawn_cards'), list)
        new_version = version > old['report_version']
        replace_use = report is not None and (usage_complete or not old['usage_complete'] or new_version)
        replace_draw = report is not None and (draw_complete or not old['draw_complete'] or new_version)
        used, drawn, appearances = (_index(player.get(key, [])) for key in
                                   ('used_cards', 'drawn_cards', 'extra_appearances'))
        old_cards = {(r['partition'], r['code']): dict(r) for r in db.execute(
            'SELECT * FROM stat_card_facts WHERE digest=? AND player=?', (digest, name))}
        partitions = {'main': Counter(main), 'extra': Counter(extra)}
        partitions['other'] = {c: 0 for c in (used.keys() | drawn.keys() | appearances.keys())
                               if c not in partitions['main'] and c not in partitions['extra']}
        for partition, codes in partitions.items():
            for code, copies in codes.items():
                if type(code) is not int or code <= 0:
                    continue
                before = old_cards.get((partition, code), {})
                fact = {key: before.get(key) for key in ('drawn', 'draw_count', 'initial_count',
                        'used', 'use_count', 'used_after_draw', 'appeared', 'appearance_count')}
                if replace_use:
                    if code in used:
                        fact.update(used=1, use_count=max(_number(used[code].get('events')), before.get('use_count') or 0)
                                    if not usage_complete else _number(used[code].get('events')))
                    elif usage_complete:
                        fact.update(used=0, use_count=0)
                    if partition == 'extra' or (partition == 'other' and code in appearances):
                        a = appearances.get(code, {})
                        count = _number(a.get('appearance_count'))
                        if count or usage_complete:
                            fact.update(appeared=int(count > 0), appearance_count=count if usage_complete
                                        else max(count, before.get('appearance_count') or 0))
                if replace_draw and partition in ('main', 'other'):
                    d = drawn.get(code)
                    if d:
                        count = _number(d.get('draw_count'))
                        fact.update(drawn=1, draw_count=count if draw_complete else max(count, before.get('draw_count') or 0),
                                    initial_count=(d.get('initial_count') if player.get('initial_draws_accounted')
                                                   else before.get('initial_count')))
                        if d.get('used_after_draw') is True:
                            fact['used_after_draw'] = 1
                        elif usage_complete and draw_complete:
                            fact['used_after_draw'] = 0
                    elif draw_complete:
                        fact.update(drawn=0, draw_count=0, initial_count=0, used_after_draw=0)
                values = [fact[k] for k in ('drawn','draw_count','initial_count','used','use_count',
                                            'used_after_draw','appeared','appearance_count')]
                db.execute('INSERT INTO stat_card_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(digest,player,partition,code) DO UPDATE SET copies=excluded.copies,'
                    'drawn=excluded.drawn,draw_count=excluded.draw_count,initial_count=excluded.initial_count,'
                    'used=excluded.used,use_count=excluded.use_count,used_after_draw=excluded.used_after_draw,'
                    'appeared=excluded.appeared,appearance_count=excluded.appearance_count',
                    [digest, name, partition, code, copies] + values)
        outcome, result_source = old['outcome'], old['result_source']
        rank = {'live_server': 3, 'native_replay': 2, 'history_inferred': 1}
        winner = (report or {}).get('winner')
        incoming_source = (report or {}).get('result_source')
        if report and report.get('result_conflict'):
            outcome, result_source = 'unknown', 'conflict'
        elif (report and report.get('_facts_base_only') and report.get('_live_evidence_present') is False
              and result_source == 'live_server'):
            outcome, result_source = 'unknown', 'evidence_unavailable'
        elif type(winner) is int and winner in (0, 1, 2) and rank.get(incoming_source, 0) >= rank.get(result_source, 0) and incoming_source in rank:
            new_outcome = 'draw' if winner == 2 else 'win' if winner == index else 'loss'
            if rank.get(incoming_source, 0) >= 2 and rank.get(result_source, 0) >= 2 and outcome != new_outcome:
                outcome, result_source = 'unknown', 'conflict'
            elif result_source != 'conflict':
                outcome, result_source = new_outcome, incoming_source
        first = (report or {}).get('first_player')
        first_flag = int(first == index) if type(first) is int and first in (0, 1) else old['first_player']
        db.execute('UPDATE stat_views SET outcome=?,result_source=?,first_player=?,usage_complete=?,draw_complete=?,'
                   'report_version=MAX(report_version,?) WHERE digest=? AND player=?',
                   (outcome, result_source, first_flag,
                    int(usage_complete) if replace_use else old['usage_complete'],
                    int(draw_complete) if replace_draw else old['draw_complete'], version, digest, name))
    db.execute('UPDATE stat_games SET report_version=MAX(report_version,?) WHERE digest=?', (version, digest))
    if report is not None and (existing is None or version >= existing['report_version']):
        payload = {key: report.get(key) for key in ('winner', 'first_player', 'initiative_chooser',
                   'result_source', 'result_conflict', 'inference_rule', 'match_evidence', 'version')}
        e = report.get('evidence')
        if isinstance(e, dict) and e.get('digest') == digest:
            payload['capture'] = {key: e.get(key) for key in ('source', 'raw', 'reason', 'received_at',
                'game_no', 'session_id', 'previous_game', 'association', 'captured_from_start')}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        kind = 'analysis:' + hashlib.sha256(encoded.encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO stat_evidence VALUES(?,?,?,?)',
                   (digest, kind, report.get('result_source') or 'unknown', encoded))
    return True


def visible_games(db, replay_root, now):
    """事实核对用，非前100统计API；来源已不存在或到期，立即不可见。"""
    from .share import safe_replay_file
    result = []
    for row in db.execute('SELECT * FROM stat_games WHERE expires_at>? ORDER BY recorded_at,digest', (float(now),)):
        sources = db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f ON r.id=f.replay_id '
                             'WHERE f.digest=?', (row['digest'],)).fetchall()
        if any(safe_replay_file(Path(replay_root), s['rel_path']) for s in sources):
            result.append(dict(row))
    return result


def purge(db, now):
    before = db.execute('SELECT COUNT(*) FROM stat_games').fetchone()[0]
    db.execute('DELETE FROM stat_games WHERE expires_at<=?', (float(now),))
    return before - db.execute('SELECT COUNT(*) FROM stat_games').fetchone()[0]
