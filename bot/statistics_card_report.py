"""多卡同源报告：一次资格快照，多张卡各自的指标与区间。

口径与 card_statistics.query 一致：
- 视角单位是"玩家—小局"，胜率按所选条件下已知胜负平计算，不强制 50%；
- 抽到=初始/通常/效果抽卡，不含检索回收；
- 卡组/系列沿用 Filters 的已核验标签；
- 双方排名范围由 statistics_rank_scope 的上下文提供，本模块只读不判定；
- 门槛按"本次样本小局数 / 本次选源基准小局数"的百分比精确比较，N=0 不出比例。
本模块不写库、不缓存跨请求事实、不新增下载或权限能力。
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .card_search import card_name
from .card_statistics import Filters, win_rate
from .card_identity import facts as identity_facts, identity, reference as identity_reference
from .card_series import annotate, reference as series_reference
from .statistics_card_names import index
from .top100 import eligible_views

WILSON = 1.959963984540054
METRICS = ('carried', 'drawn', 'opening')
SORTS = ('people', 'games', 'rate', 'low', 'name')
PARTITIONS = ('auto', 'main', 'side', 'extra')
PHASES = ('main_first', 'main_second', 'side_first', 'side_second')
EXTRA_TYPE_BITS = 0x40 | 0x2000 | 0x800000 | 0x4000000
MAX_CARDS = 100
MAX_ROWS = 500


def interval(wins, games):
    """Wilson 95% 区间；只描述抽样不确定性，不表述为因果。"""
    if not games:
        return None
    z = WILSON
    z2 = z * z
    p = wins / games
    d = 1 + z2 / games
    mid = (p + z2 / (2 * games)) / d
    radius = z * math.sqrt(p * (1 - p) / games + z2 / (4 * games * games)) / d
    return [max(0.0, mid - radius), min(1.0, mid + radius)]


def threshold_value(raw):
    text = str(raw if raw is not None else '1').strip()
    if not text or len(text) > 10 or 'e' in text.lower():
        raise ValueError('样本占比无效。')
    if len(text.partition('.')[2]) > 6:
        raise ValueError('样本占比最多 6 位小数。')
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError('样本占比无效。')
    if not value.is_finite() or value < 0 or value > 100:
        raise ValueError('样本占比应在 0 到 100 之间。')
    return value


def parse_codes(values):
    codes = []
    for value in values or ():
        text = str(value).strip()
        if not text.isdigit() or len(text) > 10 or not 0 < int(text) <= 4294967295:
            raise ValueError('卡号无效。')
        codes.append(int(text))
    unique = list(dict.fromkeys(codes))
    if len(unique) > MAX_CARDS:
        raise ValueError('一次最多比较 %d 张卡。' % MAX_CARDS)
    return tuple(unique)


def parse_combos(values):
    combos = []
    for value in values or ():
        parts = [part.strip() for part in str(value).split(',') if part.strip()]
        if len(parts) < 2 or len(parts) > 8:
            raise ValueError('组合需要 2—8 张卡。')
        codes = []
        for part in parts:
            if not part.isdigit() or len(part) > 10 or not 0 < int(part) <= 4294967295:
                raise ValueError('组合卡号无效。')
            codes.append(int(part))
        combos.append(tuple(dict.fromkeys(codes)))
    if len(combos) > 4:
        raise ValueError('一次最多设置 4 个组合。')
    return tuple(combos)


def parse_metric(value):
    text = str(value if value is not None else 'drawn').strip()
    if text not in METRICS:
        raise ValueError('统计范围无效。')
    return text


def parse_sort(value):
    text = str(value if value is not None else 'people').strip()
    if text not in SORTS:
        raise ValueError('排序方式无效。')
    return text


def parse_partition(value):
    text = str(value if value is not None else 'auto').strip()
    if text not in PARTITIONS:
        raise ValueError('卡区无效。')
    return text


def parse_phases(values):
    """局面多选：主卡局/Side局 × 先攻/后攻（另含先后攻未知）。"""
    phases = []
    for value in values or ():
        text = str(value).strip()
        if text not in PHASES:
            raise ValueError('局面条件无效。')
        if text not in phases:
            phases.append(text)
    return tuple(phases)


def phase_matches(row, phases):
    game = row.get('observed_game_no')
    first = row.get('first_player')
    kind = 'main' if game == 1 else ('side' if isinstance(game, int) and game > 1 else None)
    order = 'first' if first == 1 else ('second' if first == 0 else None)
    if order is None:
        return False
    return kind is not None and (kind + '_' + order) in phases


def parse_min_games(value):
    text = str(value if value is not None else '0').strip()
    if not text.isdigit() or len(text) > 7 or int(text) > 1000000:
        raise ValueError('最小小局数应为 0 到 1000000 的整数。')
    return int(text)


def parse_limit(value):
    text = str(value if value is not None else str(MAX_ROWS)).strip()
    if not text.isdigit() or len(text) > 7:
        raise ValueError('显示行数无效。')
    return max(1, min(MAX_ROWS, int(text)))


def _partition_for(code, names, wanted):
    if wanted != 'auto':
        return wanted
    info = names.get(str(code)) or {}
    kind = info.get('card_type')
    return 'extra' if type(kind) is int and kind & EXTRA_TYPE_BITS else 'main'


def _card_label(code, names):
    info = names.get(str(code))
    return card_name(info) if info else '卡名暂缺'


def _build_codes(db, build):
    return [row['code'] for row in db.execute(
        'SELECT code FROM stat_build_cards WHERE build=? AND copies>0', (build,))]


def _selected_rows(db, replay_root, now, filters):
    """与 card_statistics.query 相同的资格与筛选顺序，只执行一次。"""
    f = filters or Filters()
    if f.first_player is not None and type(f.first_player) is not bool:
        raise ValueError('invalid_order_filter')
    catalog = series_reference(db)
    basis = eligible_views(db, replay_root, now, f.internal_only, f.scope, catalog,
                           annotate_series=bool(f.series))
    # 只有筛选对手卡组/系列时才需要对手上下文；默认查询不再全表取/注解所有视角。
    opponents = {}
    if f.opponent_types or f.opponent_series:
        digests = sorted({row['digest'] for row in basis['views']})
        others = {}
        for start in range(0, len(digests), 400):
            chunk = digests[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            for r in db.execute(
                    'SELECT v.digest,v.player,v.build,b.deck_type,b.classification_source,b.series_status,'
                    'b.main_complete,b.extra_complete FROM stat_views v JOIN stat_builds b '
                    'ON b.fingerprint=v.build WHERE v.digest IN (%s)' % marks, chunk):
                others[(r['digest'], r['player'])] = dict(r)
        if f.opponent_series:
            annotate(db, list(others.values()), catalog)
        for row in others.values():
            opponents.setdefault(row['digest'], []).append(row)
    selected = []
    exclusions = {key: 0 for key in ('deck_type', 'series', 'opponent_type', 'opponent_series', 'first_player', 'start_at', 'end_at')}
    for row in basis['views']:
        opponent = next((o for o in opponents.get(row['digest'], []) if o['player'] != row['player']), {})
        tests = (('deck_type', f.deck_types and row['deck_type'] not in f.deck_types),
                 ('series', f.series and not set(f.series).intersection(row['series'])),
                 ('opponent_type', f.opponent_types and opponent.get('deck_type') not in f.opponent_types),
                 ('opponent_series', f.opponent_series and not set(f.opponent_series).intersection(opponent.get('series', []))),
                 ('first_player', f.first_player is not None and row['first_player'] != int(f.first_player)),
                 ('start_at', f.start_at is not None and row['recorded_at'] < f.start_at),
                 ('end_at', f.end_at is not None and row['recorded_at'] >= f.end_at))
        rejected = next((key for key, failed in tests if failed), None)
        if rejected:
            exclusions[rejected] += 1
            continue
        selected.append(row)
    return basis, selected, exclusions


def _merge_fact_rows(rows, partition):
    """把同一 card/partition 的 facts 行按 (digest,player) 合并；与 card_identity.facts 同规则。"""
    groups = {}
    for row in rows:
        groups.setdefault((row['digest'], row['player']), []).append(row)
    result = {}
    for key, group in groups.items():
        merged = {k: group[0][k] for k in ('digest', 'player', 'partition')}
        merged['partition'] = partition
        for field in ('drawn', 'used', 'used_after_draw', 'appeared'):
            values = [r[field] for r in group]
            merged[field] = 1 if 1 in values else 0 if all(v == 0 for v in values) else None
        for field in ('draw_count', 'initial_count', 'use_count', 'appearance_count'):
            values = [r[field] for r in group]
            merged[field] = sum(values) if all(v is not None for v in values) else None
        if len(group) > 1 and merged['drawn'] == 1 and merged['used'] == 1 and merged['used_after_draw'] != 1:
            merged['used_after_draw'] = None
        result[key] = merged
    return result


def query(db, replay_root, now, codes=(), filters=None, metric='drawn', threshold='1',
          min_games=0, sort='people', partition='auto', limit=MAX_ROWS, exclude=(), phases=(), phase_none=False,
          combos=()):
    codes = parse_codes(codes)
    exclude = parse_codes(exclude)
    combos = parse_combos(combos)
    phases = parse_phases(phases)
    metric = parse_metric(metric)
    sort_key = parse_sort(sort)
    wanted_partition = parse_partition(partition)
    threshold = threshold_value(threshold)
    min_games = parse_min_games(min_games)
    limit = parse_limit(limit)

    basis, selected, exclusions = _selected_rows(db, replay_root, now, filters)
    if phase_none:
        selected = []
    elif phases:
        selected = [row for row in selected if phase_matches(row, phases)]
    # 本次报告只应看这些视角；临时表是连接内临时库，不写主库、不跨请求。
    db.execute('DROP TABLE IF EXISTS temp._report_selected')
    db.execute('CREATE TEMP TABLE _report_selected(digest TEXT NOT NULL, player TEXT NOT NULL, '
               'PRIMARY KEY(digest,player))')
    db.executemany('INSERT OR IGNORE INTO _report_selected(digest,player) VALUES(?,?)',
                   [(row['digest'],row['player']) for row in selected])
    names = index(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent)
    ident_index, ident_version, ident_status = identity_reference(db)

    def identify(code):
        canonical, codes = ident_index.get(code, (code, [code]))
        return dict(requested_card=code, canonical_card=canonical, codes=codes,
                    status=ident_status if code in ident_index or ident_status == 'invalid_reference_exact_only' else 'exact',
                    reference_version=ident_version)
    # 起手不存在：先筛出可确证"共同没有这些牌"的小局，再在筛选后的小局上算各卡分母。
    # 未知/不完整起手不算不存在；没取到事实也按未知处理，不走"整局没抽到"的替代口径。
    opening = {}
    for code in exclude:
        ident = identify(code)
        marks = ','.join('?' for _ in ident['codes'])
        parts = {row[0] for row in db.execute(
            'SELECT DISTINCT partition FROM stat_build_cards WHERE code IN (%s)' % marks, list(ident['codes']))}
        # 额外卡不会出现在起手；用构筑里的分区判断，不依赖卡库元数据是否收录该卡号。
        if parts and not (parts & {'main', 'side'}):
            raise ValueError('起手排除只支持主卡。')
        opening[code] = identity_facts(db, ident['codes'], 'main', '_report_selected')
    excluded_by_opening = 0
    if opening:
        kept = []
        for row in selected:
            proven = row.get('draw_complete') == 1
            if proven:
                for facts in opening.values():
                    fact = facts.get((row['digest'], row['player']))
                    if fact is None or fact.get('initial_count') != 0:
                        proven = False
                        break
            if proven:
                kept.append(row)
        excluded_by_opening = len(selected) - len(kept)
        selected = kept
    base = win_rate(selected)
    roster = {key: value for key, value in basis['roster'].items() if key != 'members'}
    roster['member_count'] = len(basis['roster']['members'])

    if codes or combos:
        targets = codes
    else:
        seen = set()
        for row in selected:
            seen.update(_build_codes(db, row['build']))
        targets = sorted(seen)

    idents = {code: identify(code) for code in targets}
    parts = {code: _partition_for(code, names, wanted_partition) for code in targets}
    # 一次取本次选中构筑的卡表（连接内临时表），避免每张卡查一次 stat_build_cards。
    db.execute('DROP TABLE IF EXISTS temp._report_builds')
    db.execute('CREATE TEMP TABLE _report_builds(build TEXT PRIMARY KEY)')
    db.executemany('INSERT OR IGNORE INTO _report_builds(build) VALUES(?)',
                   [(row['build'],) for row in selected])
    copies_lookup = {}
    for card in db.execute('SELECT c.build,c.partition,c.code,SUM(c.copies) copies '
                           'FROM stat_build_cards c JOIN _report_builds b ON b.build=c.build '
                           'GROUP BY c.build,c.partition,c.code'):
        copies_lookup.setdefault((card['code'], card['partition']), {})[card['build']] = card['copies']
    rows_by_build = {}
    for row in selected:
        rows_by_build.setdefault(row['build'], []).append(row)
    # 本次 selected 的 facts 一次查回，按 (code,partition) 聚合；不再逐卡调用 card_identity.facts。
    facts_by_code = {}
    for fact in db.execute(
            'SELECT f.code,f.digest,f.player,f.partition,f.drawn,f.used,f.used_after_draw,f.appeared,'
            'f.draw_count,f.initial_count,f.use_count,f.appearance_count '
            'FROM stat_card_facts f JOIN _report_selected s '
            'ON s.digest=f.digest AND s.player=f.player'):
        facts_by_code.setdefault((fact['code'], fact['partition']), []).append(dict(fact))
    view_by_key = {(row['digest'], row['player']): row for row in selected}
    fact_cache = {}
    rows = []
    for code in targets:
        ident = idents[code]
        part = parts[code]
        card_rows = []
        for canonical in ident['codes']:
            normal = facts_by_code.get((canonical, part), ())
            card_rows.extend(normal)
            own = {(r['digest'], r['player']) for r in normal}
            for r in facts_by_code.get((canonical, 'other'), ()):
                view = view_by_key.get((r['digest'], r['player']))
                if view is None or view['build'] not in copies_lookup.get((canonical, part), {}):
                    continue
                if (r['digest'], r['player']) in own:
                    continue
                card_rows.append(r)
        facts = _merge_fact_rows(card_rows, part)
        copies_by_build = {}
        for canonical in ident['codes']:
            for build, copies in copies_lookup.get((canonical, part), {}).items():
                copies_by_build[build] = copies_by_build.get(build, 0) + copies
        carried_rows = [row for build in copies_by_build for row in rows_by_build.get(build, ())]
        if metric == 'carried':
            pool = carried_rows
        else:
            pool = []
            for row in carried_rows:
                fact = facts.get((row['digest'], row['player']))
                if not fact:
                    continue
                if metric == 'drawn' and fact.get('drawn') == 1:
                    pool.append(row)
                elif metric == 'opening' and (fact.get('initial_count') or 0) > 0:
                    pool.append(row)
        rate = win_rate(pool)
        games = rate['denominator']
        if not games or games < min_games:
            continue
        if base['denominator'] and Decimal(games) * 100 < threshold * base['denominator']:
            continue
        rows.append(dict(
            code=code,
            name=_card_label(code, names),
            partition=part,
            people=len({row['player'] for row in carried_rows}),
            games=games,
            wins=rate['wins'],
            losses=rate['losses'],
            draws=rate['draws'],
            unknown=len(pool) - games,
            rate=rate['percentage'],
            interval=interval(rate['wins'], games),
            sample_percentage=100 * games / base['denominator'] if base['denominator'] else None,
        ))

    # 组合行：所有组合卡都在初始手牌中（AND）；组合卡不在卡组按已知 0 处理。
    combo_rows = []
    for combo_index, combo in enumerate(combos, 1):
        groups = []
        seen_groups = set()
        for code in combo:
            canonical, members = ident_index.get(code, (code, [code]))
            if canonical in seen_groups:
                continue
            seen_groups.add(canonical)
            groups.append((canonical, tuple(sorted(set(members)))))
        group_builds = []
        initial_maps = []
        for canonical, members in groups:
            build_copies = {}
            for member in members:
                for build, copies in copies_lookup.get((member, 'main'), {}).items():
                    if copies > 0:
                        build_copies[build] = 1
            group_builds.append(build_copies)
            values = {}
            for member in members:
                for fact in facts_by_code.get((member, 'main'), ()):
                    if fact.get('initial_count') is None:
                        continue
                    key = (fact['digest'], fact['player'])
                    values[key] = max(values.get(key, 0), int(fact['initial_count']))
            initial_maps.append(values)
        hits = []
        denominator = 0
        unknown = 0
        for row in selected:
            key = (row['digest'], row['player'])
            values = []
            known = True
            for position, _ in enumerate(groups):
                if row['build'] not in group_builds[position]:
                    values.append(0)
                    continue
                value = initial_maps[position].get(key)
                if value is None:
                    known = False
                    break
                values.append(value)
            if not known:
                unknown += 1
                continue
            denominator += 1
            if all(value > 0 for value in values):
                hits.append(row)
        rate = win_rate(hits)
        games = rate['denominator']
        members = [ident_index.get(code, (code, [code]))[0] for code in combo]
        labels = []
        for code in combo:
            info = names.get(str(code))
            labels.append(dict(code=code, name=card_name(info) if info else '卡名暂缺'))
        combo_rows.append(dict(
            code=-combo_index,
            name='组合%d' % combo_index,
            partition='combo',
            is_combo=True,
            combo_members=labels,
            people=len({row['player'] for row in hits}),
            games=games,
            wins=rate['wins'],
            losses=rate['losses'],
            draws=rate['draws'],
            unknown=unknown,
            rate=rate['percentage'],
            interval=interval(rate['wins'], games),
            sample_percentage=100 * games / base['denominator'] if base['denominator'] else None,
            opening_hits=len(hits),
            opening_denominator=denominator,
            opening_rate=100 * len(hits) / denominator if denominator else None,
        ))
    rows.extend(combo_rows)

    # 同名异画只保留卡号最小的那一行；统计已按 identity 合并，直接去重即可。
    by_name = {}
    for row in rows:
        key = row['name']
        previous = by_name.get(key)
        if previous is None or row['code'] < previous['code']:
            by_name[key] = row
    rows = list(by_name.values())

    if sort_key == 'people':
        rows.sort(key=lambda row: (-row['people'], -row['games'], row['code']))
    elif sort_key == 'games':
        rows.sort(key=lambda row: (-row['games'], -row['people'], row['code']))
    elif sort_key == 'rate':
        rows.sort(key=lambda row: (-(row['rate'] if row['rate'] is not None else -1), -row['games'], row['code']))
    elif sort_key == 'low':
        rows.sort(key=lambda row: (-(row['interval'][0] if row['interval'] else -1), -row['games'], row['code']))
    else:
        rows.sort(key=lambda row: (row['name'], row['code']))

    eligible = len(rows)
    shown = rows[:limit]
    notes = []
    if codes and not eligible:
        notes.append('所选条件下没有达到门槛的卡；可放宽条件或降低样本占比。')
    if eligible > len(shown):
        notes.append('共 %d 行达到门槛，只显示前 %d 行。' % (eligible, len(shown)))
    return dict(
        version=1,
        generated_at=now,
        unit='player_small_game_perspective',
        filters=dict(deck=list(filters.deck_types) if filters else [],
                     opponent=list(filters.opponent_types) if filters else [],
                     series=list(filters.series) if filters else [],
                     opponent_series=list(filters.opponent_series) if filters else [],
                     seat='all' if not filters or filters.first_player is None else ('first' if filters.first_player else 'second'),
                     phase=list(phases),
                     metric=metric, threshold=str(threshold), min_games=min_games,
                     sort=sort_key, partition=wanted_partition, exclude=list(exclude)),
        roster=roster,
        classification_revision=basis['classification_revision'],
        series_reference=basis['series_reference'],
        candidate_samples=len(basis['views']),
        selected_samples=len(selected),
        data_quality=dict(basis['diagnostics'], filter_exclusions=exclusions,
                          filter_exclusion_unit='first_failing_filter_per_perspective',
                          excluded_by_opening=excluded_by_opening),
        base=dict(denominator=base['denominator'], wins=base['wins'], rate=base['percentage'],
                  unknown=base['unknown_samples']),
        rows=shown,
        total=eligible,
        shown=len(shown),
        truncated=len(shown) < eligible,
        notes=notes,
    )
