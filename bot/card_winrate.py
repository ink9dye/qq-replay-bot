# -*- coding: utf-8 -*-
"""QQ「单卡胜率」：双方当前前100、主卡实际使用后的总体胜率与对手卡组 Top 5。

复用 top100.eligible_views 的资格与 card_statistics 的事实/对阵算法；本模块
只做取卡名、只读连接、30 秒双方前100人口快照与结果小缓存，不新增统计口径。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

CACHE_SECONDS = 30.0
CACHE_MAX = 16
_BASIS_SECONDS = 30.0
_CACHE = {}
_BASIS = {}
_LOCK = threading.Lock()
MATCHUP_MIN_GAMES = 10
MATCHUP_FALLBACK_GAMES = 5
MATCHUP_LIMIT = 5


def resolve_card(database, query):
    """卡名或卡号 -> (code, name)。卡名沿用「使用量」的本地库匹配。"""
    text = str(query or '').strip()
    if text.isdigit() and 0 < int(text) <= 4294967295:
        code = int(text)
        try:
            from .statistics_card_names import index
            from .card_search import card_name
            info = index(Path(database).resolve().parent).get(str(code))
            name = card_name(info) if info else ('#%d' % code)
        except Exception:
            name = '#%d' % code
        return code, name
    from .card_usage_pie import resolve_card as resolve_name
    path = Path(database).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        return resolve_name(path.parent, text, db)


def _stamp(db, now):
    """名册/分类/有效对局/排除状态变化时让 30 秒人口快照失效。"""
    roster = db.execute('SELECT version,updated_at,last_error FROM stat_roster_state '
                        'WHERE singleton=1').fetchone()
    revision = db.execute('SELECT revision FROM stat_classification_state '
                          'WHERE singleton=1').fetchone()
    games = db.execute('SELECT COUNT(*),MAX(received_at) FROM stat_games WHERE expires_at>?',
                       (float(now),)).fetchone()
    exclusions = db.execute('SELECT COUNT(*) FROM stat_exclusions').fetchone()
    return (tuple(roster) if roster else None,
            tuple(revision) if revision else None,
            tuple(games), tuple(exclusions))


def _matched_basis(db, replay_root, now):
    """双方都在当前前100、且对手唯一的玩家视角；只算一次，30 秒内复用。"""
    from .top100 import eligible_views
    from .card_statistics import _opponent_rows
    basis = eligible_views(db, Path(replay_root), float(now), False, 'default',
                           annotate_series=False)
    top100 = {row[0] for row in db.execute('SELECT player FROM stat_current_top100')}
    opponents = _opponent_rows(db, {row['digest'] for row in basis['views']})
    selected = []
    for row in basis['views']:
        if row['player'] not in top100:
            continue
        other = [item for item in opponents.get(row['digest'], ())
                 if item['player'] != row['player']]
        if len(other) != 1 or other[0]['player'] not in top100:
            continue
        selected.append(dict(row))
    return selected


def _basis(db, replay_root, now):
    key = (str(Path(db.execute('PRAGMA database_list').fetchone()[2]).resolve()),
           _stamp(db, now))
    now_mono = time.monotonic()
    with _LOCK:
        hit = _BASIS.get(key)
        if hit is not None and 0 <= now_mono - hit[0] < _BASIS_SECONDS:
            return hit[1]
    selected = _matched_basis(db, replay_root, now)
    with _LOCK:
        _BASIS[key] = (time.monotonic(), selected)
        for old in [k for k in _BASIS if k[0] == key[0] and k != key]:
            _BASIS.pop(old, None)
        if len(_BASIS) > 4:
            _BASIS.pop(min(_BASIS, key=lambda item: _BASIS[item][0]), None)
    return selected


def _compute(database, replay_root, now, code):
    from .card_identity import identity, facts as identity_facts
    from .card_statistics import matchups, _opponent_rows
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        selected = [dict(row) for row in _basis(db, replay_root, now)]
        info = identity(db, int(code))
        codes = info['codes']
        builds = sorted({row['build'] for row in selected})
        copies = {}
        marks = ','.join('?' for _ in codes)
        for start in range(0, len(builds), 400):
            chunk = builds[start:start + 400]
            slots = ','.join('?' for _ in chunk)
            for row in db.execute(
                    f'SELECT build,SUM(copies) copies FROM stat_build_cards '
                    f'WHERE code IN ({marks}) AND partition=? AND build IN ({slots}) '
                    f'GROUP BY build', [*codes, 'main', *chunk]):
                copies[row['build']] = row['copies']
        db.execute('DROP TABLE IF EXISTS temp._card_winrate_selected')
        db.execute('CREATE TEMP TABLE _card_winrate_selected(digest TEXT NOT NULL, '
                   'player TEXT NOT NULL, PRIMARY KEY(digest,player))')
        db.executemany('INSERT OR IGNORE INTO _card_winrate_selected(digest,player) VALUES(?,?)',
                       [(row['digest'], row['player']) for row in selected])
        facts = identity_facts(db, codes, 'main', '_card_winrate_selected')
        for row in selected:
            row['copies'] = copies.get(row['build'], 0)
        opponents = _opponent_rows(db, {row['digest'] for row in selected})
        result = matchups(db, selected, facts, 'main', MATCHUP_LIMIT,
                          MATCHUP_MIN_GAMES, MATCHUP_FALLBACK_GAMES, opponents=opponents)
        # 按每个小局的玩家视角分桶；复用同一份事实和对手，不重查人口或录像。
        order = {}
        for label, value in (('first', 1), ('second', 0), ('unknown', None)):
            subset = [row for row in selected if row.get('first_player') == value]
            order[label] = matchups(db, subset, facts, 'main', limit=0,
                                    opponents=opponents)['base']
    return dict(card=info['canonical_card'], matchups=result, order=order)


def collect(database, replay_root, now, card_name):
    code, name = resolve_card(database, card_name)
    key = (str(Path(database).resolve()), code)
    current = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and 0 <= current - hit[0] < CACHE_SECONDS:
            return dict(hit[1], name=name)
    payload = _compute(database, replay_root, now, code)
    payload['name'] = name
    payload['code'] = code
    with _LOCK:
        _CACHE[key] = (time.monotonic(), payload)
        if len(_CACHE) > CACHE_MAX:
            for old in sorted(_CACHE, key=lambda item: _CACHE[item][0])[:len(_CACHE) - CACHE_MAX]:
                _CACHE.pop(old, None)
    return payload


def message(payload):
    """纯文字回复：总体/先攻/后攻使用后胜率 + 对手卡组 Top 5。"""
    code = int(payload['code'])
    name = payload.get('name') or ('#%d' % code)
    matchups = payload.get('matchups') or {}
    base = matchups.get('base') or {}
    lines = ['前100单卡胜率：%s（%08d）' % (name, code)]
    if base.get('games'):
        lines.append('总体使用后胜率 %.1f%%（%d胜/%d负/%d平，共%d局；双方当前前100）' % (
            base['rate'], base['wins'], base['losses'], base['draws'], base['games']))
    else:
        lines.append('总体使用后胜率：暂无有效样本（双方当前前100）')
    order = payload.get('order') or {}
    for key, label in (('first', '先攻'), ('second', '后攻')):
        bucket = order.get(key) or {}
        if bucket.get('games'):
            mark = '（样本少）' if bucket['games'] < MATCHUP_MIN_GAMES else ''
            lines.append('%s使用后胜率 %.1f%%（%d胜/%d负/%d平，共%d局）%s' % (
                label, bucket['rate'], bucket['wins'], bucket['losses'],
                bucket['draws'], bucket['games'], mark))
        else:
            lines.append('%s使用后胜率：暂无有效样本' % label)
    unknown = (order.get('unknown') or {}).get('games', 0)
    if unknown:
        lines.append('先后攻未知 %d局：仅计入总体。' % unknown)
    rows = matchups.get('rows') or []
    if rows:
        lines.append('对前百卡组最高的%d个（使用后）：' % len(rows))
        for index, row in enumerate(rows, 1):
            mark = '（样本少）' if row.get('low_sample') else ''
            lines.append('%d. %s %.1f%%（%d胜/%d负/%d平，%d局）%s' % (
                index, row['deck_type'], row['rate'], row['wins'], row['losses'],
                row['draws'], row['games'], mark))
    else:
        lines.append('对前百卡组：暂无达到%d局的对手卡组。' % MATCHUP_FALLBACK_GAMES)
    lines.append('口径：双方当前前100；主卡实际使用且胜负明确；平局计入分母；'
                 '先后攻按该小局；分组不足10局标样本少；'
                 '每行至少%d局，不足时用%d局以上补足并标注。' % (
                     MATCHUP_MIN_GAMES, MATCHUP_FALLBACK_GAMES))
    return '\n'.join(lines)
