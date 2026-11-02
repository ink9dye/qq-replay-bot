# -*- coding: utf-8 -*-
"""额外卡组统计：携带率、出场率、出场胜率（观战事实，不混主卡抽到口径）。"""
from __future__ import annotations

from .card_identity import reference
from .card_search import card_name
from .statistics_card_names import index as card_index
from .statistics_card_report import Filters, _selected_rows, phase_matches

MAX_ROWS = 500
SORTS = ('carry', 'players', 'appearance', 'win', 'code')


def query(db, replay_root, now, filters=None, phases=(), phase_none=False, limit=MAX_ROWS, sort='carry'):
    basis, selected, exclusions = _selected_rows(db, replay_root, now, filters)
    if phase_none:
        selected = []
    elif phases:
        selected = [row for row in selected if phase_matches(row, phases)]
    games = len(selected)
    players = {row['player'] for row in selected}
    rows_by_build = {}
    for row in selected:
        rows_by_build.setdefault(row['build'], []).append(row)

    builds = sorted(rows_by_build)
    groups = {}
    for start in range(0, len(builds), 400):
        chunk = builds[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        for card in db.execute(
                'SELECT build,code FROM stat_build_cards '
                f'WHERE partition=\'extra\' AND copies>0 AND build IN ({marks})', chunk):
            groups.setdefault(card['build'], set()).add(int(card['code']))

    mapping, _, _ = reference(db)
    canonical = lambda code: mapping.get(code, (code,))[0]
    code_min = {}
    build_codes = {}
    for build, codes in groups.items():
        normalized = set()
        for code in codes:
            key = canonical(code)
            normalized.add(key)
            previous = code_min.get(key)
            if previous is None or code < previous:
                code_min[key] = code
        build_codes[build] = normalized

    carried = {}
    for row in selected:
        key = (row['digest'], row['player'])
        for code in build_codes.get(row['build'], ()):
            carried.setdefault(code, set()).add(key)

    # 只取本次 selected 的额外事实；appeared=出场，used 仅作辅助。
    db.execute('DROP TABLE IF EXISTS temp._extra_selected')
    db.execute('CREATE TEMP TABLE _extra_selected(digest TEXT, player TEXT, PRIMARY KEY(digest,player))')
    db.executemany('INSERT OR IGNORE INTO _extra_selected(digest,player) VALUES(?,?)',
                   [(row['digest'], row['player']) for row in selected])
    appeared = {}
    used = {}
    for fact in db.execute(
            'SELECT f.code,f.digest,f.player,MAX(f.appeared) appeared,MAX(f.used) used '
            'FROM stat_card_facts f JOIN _extra_selected s '
            'ON s.digest=f.digest AND s.player=f.player '
            'WHERE f.partition=\'extra\' GROUP BY f.code,f.digest,f.player'):
        key = (fact['digest'], fact['player'])
        group = canonical(int(fact['code']))
        if group not in carried or key not in carried[group]:
            continue
        if fact['appeared'] == 1:
            appeared.setdefault(group, set()).add(key)
        if fact['used'] == 1:
            used.setdefault(group, set()).add(key)

    rows = []
    names = card_index(replay_root.parent)
    by_key = {(row['digest'], row['player']): row for row in selected}
    for group, carry_keys in carried.items():
        carry_count = len(carry_keys)
        appear_keys = appeared.get(group, set())
        outcomes = {'win': 0, 'loss': 0, 'draw': 0, 'unknown': 0}
        for key in appear_keys:
            result = str((by_key.get(key) or {}).get('outcome') or 'unknown')
            outcomes[result if result in outcomes else 'unknown'] += 1
        known = outcomes['win'] + outcomes['loss']
        code = code_min[group]
        info = names.get(str(code))
        rows.append(dict(
            code=code,
            name=card_name(info) if info else '卡名暂缺',
            carry_players=len({key[1] for key in carry_keys}),
            carry_games=carry_count,
            carry_rate=100 * carry_count / games if games else None,
            appear_games=len(appear_keys),
            appearance_rate=100 * len(appear_keys) / carry_count if carry_count else None,
            used_games=len(used.get(group, set())),
            wins=outcomes['win'], losses=outcomes['loss'], draws=outcomes['draw'], unknown=outcomes['unknown'],
            win_rate=100 * outcomes['win'] / known if known else None,
        ))
    if sort == 'players':
        rows.sort(key=lambda row: (-row['carry_players'], -(row['carry_rate'] or -1), row['code']))
    elif sort == 'appearance':
        rows.sort(key=lambda row: (-(row['appearance_rate'] if row['appearance_rate'] is not None else -1), -row['carry_games'], row['code']))
    elif sort == 'win':
        rows.sort(key=lambda row: (-(row['win_rate'] if row['win_rate'] is not None else -1), -row['carry_games'], row['code']))
    elif sort == 'code':
        rows.sort(key=lambda row: row['code'])
    else:
        rows.sort(key=lambda row: (-(row['carry_rate'] if row['carry_rate'] is not None else -1), -row['carry_games'], row['code']))
    total = len(rows)
    shown = rows[:limit]
    return dict(
        version=1, generated_at=now, unit='extra_deck_presence',
        filters=dict(
            series=list(filters.series) if filters else [],
            opponent_series=list(filters.opponent_series) if filters else [],
            phase=list(phases),
            sort=sort,
        ),
        roster=basis['roster'],
        classification_revision=basis['classification_revision'],
        candidate_samples=basis.get('candidate_samples'),
        selected_samples=games,
        base=dict(games=games, players=len(players)),
        data_quality=dict(basis.get('diagnostics') or {}, filter_exclusions=exclusions),
        rows=shown,
        total=total, shown=len(shown), truncated=len(shown) < total,
        notes=[] if games else ['当前条件下没有可统计的有效玩家视角。'],
    )
