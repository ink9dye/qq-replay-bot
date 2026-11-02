# -*- coding: utf-8 -*-
"""卡牌组合的起手概率：所有指定卡牌都必须在初始手牌中（AND）。"""
from __future__ import annotations

from pathlib import Path

from .card_identity import reference
from .card_search import card_name
from .statistics_card_names import index as card_index
from .statistics_card_report import Filters, _selected_rows, interval, phase_matches

MAX_COMBO = 8


def query(db, replay_root, now, combo, filters=None, phases=(), phase_none=False):
    wanted = []
    for value in combo:
        try:
            code = int(value)
        except (TypeError, ValueError):
            raise ValueError('组合卡号无效。')
        if code <= 0 or code > 4294967295:
            raise ValueError('组合卡号无效。')
        wanted.append(code)
    if len(wanted) < 2:
        raise ValueError('组合至少需要两张卡。')
    if len(wanted) > MAX_COMBO:
        raise ValueError(f'组合最多 {MAX_COMBO} 张卡。')

    basis, selected, exclusions = _selected_rows(db, replay_root, now, filters or Filters())
    if phase_none:
        selected = []
    elif phases:
        selected = [row for row in selected if phase_matches(row, phases)]
    if not selected:
        return dict(version=1, generated_at=now, combo=[], denominator=0, matched=0,
                    rate=None, unknown=0, notes=['当前条件下没有有效视角。'])

    mapping, _, _ = reference(db)
    groups = []
    seen = set()
    for code in wanted:
        canonical = mapping.get(code, (code,))[0]
        if canonical in seen:
            continue
        seen.add(canonical)
        members = mapping.get(canonical, (canonical,))[1] if canonical in mapping else [canonical]
        groups.append((canonical, tuple(sorted(set(members)))))

    builds = sorted({row['build'] for row in selected})
    partitions = {}
    for start in range(0, len(builds), 400):
        chunk = builds[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        for code in {value for _, members in groups for value in members}:
            found = {row[0] for row in db.execute(
                'SELECT DISTINCT partition FROM stat_build_cards WHERE code=? '
                f'AND build IN ({marks}) AND copies>0', [code, *chunk])}
            partitions.setdefault(code, set()).update(found)
    for _, members in groups:
        parts = set().union(*(partitions.get(code, set()) for code in members))
        if parts and 'main' not in parts:
            raise ValueError('组合只支持主卡组卡片。')

    db.execute('DROP TABLE IF EXISTS temp._combo_selected')
    db.execute('CREATE TEMP TABLE _combo_selected(digest TEXT, player TEXT, PRIMARY KEY(digest,player))')
    db.executemany('INSERT OR IGNORE INTO _combo_selected(digest,player) VALUES(?,?)',
                   [(row['digest'], row['player']) for row in selected])

    all_codes = {code for _, members in groups for code in members}
    marks = ','.join('?' for _ in all_codes)
    group_builds = {}
    for row in db.execute(
            'SELECT build,code,partition,copies FROM stat_build_cards '
            f'WHERE copies>0 AND code IN ({marks})', list(all_codes)):
        if row['partition'] != 'main':
            continue
        group = mapping.get(int(row['code']), (int(row['code']),))[0]
        group_builds.setdefault(group, set()).add(row['build'])

    initials = {}
    facts = {}
    for fact in db.execute(
            'SELECT f.code,f.digest,f.player,f.initial_count,f.draw_count,f.drawn '
            'FROM stat_card_facts f JOIN _combo_selected s '
            'ON s.digest=f.digest AND s.player=f.player '
            f'WHERE f.partition=\'main\' AND f.code IN ({marks})', list(all_codes)):
        key = (fact['digest'], fact['player'])
        group = mapping.get(int(fact['code']), (int(fact['code']),))[0]
        facts.setdefault(group, {})[key] = fact
        if fact['initial_count'] is not None:
            initials.setdefault(group, {})[key] = int(fact['initial_count'])

    names = card_index(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent)
    labels = []
    for canonical, members in groups:
        code = min(members)
        info = names.get(str(code))
        labels.append(dict(code=code, name=card_name(info) if info else '卡名暂缺',
                           canonical=canonical))

    denominator = 0
    matched = 0
    unknown = 0
    wins = losses = draws = 0
    for row in selected:
        key = (row['digest'], row['player'])
        values = []
        known = True
        for canonical, _ in groups:
            if row['build'] not in group_builds.get(canonical, set()):
                values.append(0)
                continue
            value = initials.get(canonical, {}).get(key)
            if value is None:
                known = False
                break
            values.append(value)
        if not known:
            unknown += 1
            continue
        denominator += 1
        if all(value > 0 for value in values):
            matched += 1
            outcome = str(row.get('outcome') or '')
            if outcome == 'win':
                wins += 1
            elif outcome == 'loss':
                losses += 1
            elif outcome == 'draw':
                draws += 1

    total_known = wins + losses + draws
    return dict(
        version=1, generated_at=now, combo=labels,
        denominator=denominator, matched=matched,
        rate=100 * matched / denominator if denominator else None,
        interval=interval(matched, denominator),
        unknown=unknown, wins=wins, losses=losses, draws=draws,
        win_rate=100 * wins / total_known if total_known else None,
        base_denominator=len(selected),
        filter_exclusions=exclusions,
        notes=[] if denominator else ['组合的所有卡都没有完整的初始手牌记录。'],
    )
