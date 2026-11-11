# -*- coding: utf-8 -*-
"""前100卡牌使用量：主卡/备牌/总投入三张饼图。"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path


def normalize(value):
    text = unicodedata.normalize('NFKC', str(value or '')).lower()
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]', '', text)


def _card_index(data_dir):
    from .statistics_card_names import index
    return index(Path(data_dir))


def _recorded_copies(db, codes):
    """构筑库里这几个卡号各自的记录量；查询失败当作没有记录。"""
    marks = ','.join('?' for _ in codes)
    try:
        # 卡号只来自本地卡库的整数 id，占位符数量由 codes 决定。
        return {int(code): int(total or 0) for code, total in db.execute(
            'SELECT code, SUM(copies) FROM stat_build_cards '
            f'WHERE code IN ({marks}) GROUP BY code', list(codes))}
    except sqlite3.Error:
        return {}


def _prefer_recorded(db, exact, chosen):
    """同名卡号不止一个时，优先选构筑库里真有记录的那个号。

    先行服把卡写成 1013xxxxx，公开库收的是正式号；两边中文名相同，但统计
    数据只落在其中一个号上。旧逻辑永远取最小卡号，会落到空号，导致「使用量」
    「单卡流向」全是 0。只在当前选中的号没有记录时才改用有记录的号，其余情况
    保持原口径不变。
    """
    codes = sorted({item[1] for item in exact})
    if len(codes) < 2:
        return chosen
    counts = _recorded_copies(db, codes)
    if not counts or counts.get(chosen[1], 0) > 0:
        return chosen
    best = max((item for item in exact if counts.get(item[1], 0) > 0),
               key=lambda item: (counts[item[1]], -item[1]), default=None)
    return best or chosen


def resolve_card(data_dir, query, db=None):
    """返回 (code, name)；本地库按中文名/别名匹配。

    传入 db 时，同名多卡号会优先落到统计库里实际有记录的那个号。
    """
    wanted = normalize(query)
    if not wanted:
        raise ValueError('卡名不能为空')
    if wanted.isdigit() and 0 < int(wanted) <= 4294967295:
        code = int(wanted)
        info = _card_index(data_dir).get(str(code)) or {}
        return code, str(info.get('cn_name') or ('#%d' % code))
    exact, loose = [], []
    for info in _card_index(data_dir).values():
        names = [info.get(key) for key in ('cn_name', 'sc_name', 'md_name', 'nwbbs_n', 'cnocg_n')]
        for name in names:
            if not name:
                continue
            if normalize(name) == wanted:
                exact.append((0, int(info['id']), str(info.get('cn_name') or name)))
                break
            if wanted in normalize(name):
                loose.append((1, int(info['id']), str(info.get('cn_name') or name)))
                break
    hits = exact or loose
    if not hits:
        raise ValueError('本地卡库没有找到这张卡')
    hits.sort(key=lambda item: (item[0], len(item[2]), item[1]))
    chosen = hits[0]
    if db is not None and exact:
        chosen = _prefer_recorded(db, exact, chosen)
    return chosen[1], chosen[2]


def identity_codes(db, code):
    """同一张卡的异画/先行卡号；没有身份映射文件时只有它自己。"""
    from .card_identity import identity
    return identity(db, int(code))['codes'] or [int(code)]


def _session_key(row):
    session = row.get('observed_session_id')
    if session:
        return (session, tuple(row.get('observed_match_players') or ()))
    return row.get('digest')


def _latest_sessions(views):
    """每人最近一场的全部局次；分组与排序规则和 latest_views 一致。"""
    grouped = {}
    for row in views:
        grouped.setdefault(str(row['player']).strip(), {}).setdefault(_session_key(row), []).append(row)
    latest = {}
    for player, matches in grouped.items():
        latest[player] = max(
            matches.values(),
            key=lambda rows: max((r['recorded_at'], r.get('received_at', 0), r['digest'])
                                 for r in rows))
    return latest


def _main_copies(db, builds, codes):
    """这些构筑里该卡在主卡分区的张数；换备换入的牌在第 2/3 局也算 main。"""
    builds = sorted({b for b in builds if b})
    if not builds or not codes:
        return {}
    marks = ','.join('?' for _ in codes)
    result = {}
    for start in range(0, len(builds), 400):
        chunk = builds[start:start + 400]
        slots = ','.join('?' for _ in chunk)
        for row in db.execute(
                'SELECT build, SUM(copies) AS copies FROM stat_build_cards '
                f"WHERE partition='main' AND code IN ({marks}) AND build IN ({slots}) "
                'GROUP BY build', [*codes, *chunk]):
            result[row['build']] = int(row['copies'] or 0)
    return result


def collect(database, data_dir, now, card_name):
    from .statistics_dashboard import population
    replay_root = Path(database).parent / 'replays'
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        code, name = resolve_card(data_dir, card_name, db)
        codes = identity_codes(db, code)
        # 代表构筑与「单卡流向」/网页/换备同一份：最近一场优先取首局
        # （game_no=1）的主卡组。旧实现按 recorded_at 取最近一局，常常落到
        # BO3 第 2/3 局的换备构筑，把 Side 换进来的卡算成主卡。
        basis, latest, people = population(db, replay_root, now)
        sessions = _latest_sessions(basis['views'])
        builds = {row.get('build') for rows in sessions.values() for row in rows}
        builds |= {(latest.get(person['player']) or {}).get('build') for person in people}
        copies = _main_copies(db, builds, codes)
        records = []
        for person in people:
            player = person['player']
            row = latest.get(player) or {}
            build = row.get('build')
            games = sessions.get(player) or []
            first = [r for r in games if r.get('observed_game_no') == 1
                     and r.get('observed_identity_confirmed') is True]
            later = [r for r in games if (r.get('observed_game_no') or 0) > 1]
            # 主卡只认确证第 1 局；没有首局就不把“最新观测”当主卡（那可能是换备后的卡表）。
            main_known = len(first) == 1
            main = copies.get(first[0].get('build'), 0) if main_known else copies.get(build, 0)
            side = side_out = 0
            side_known = False
            # 备牌没有直接记录（录像不含备牌表），只能看第 2/3 局相对第 1 局的净换入。
            if main_known and later:
                later_counts = [copies.get(r.get('build'), 0) for r in later]
                side = max(0, max(later_counts) - main)
                side_out = max(0, main - min(later_counts))
                side_known = True
            records.append(dict(player=player, rank=int(person.get('rank') or 999),
                                main=main, side=side, side_out=side_out,
                                main_known=main_known, known=side_known,
                                total=main + side, build=build, has_build=bool(row)))
    records.sort(key=lambda item: item['rank'])
    # 没有确认首局的录像直接忽略（不进任何分母），只在文案里报人数。
    valid = [row for row in records if row['main_known']]
    excluded = len(records) - len(valid)
    main_dist = {0: 0, 1: 0, 2: 0, 3: 0}
    side_dist = {0: 0, 1: 0, 2: 0, 3: 0}
    total_dist = {0: 0, 1: 0, 2: 0, 3: 0}
    for row in valid:
        main_dist[min(row['main'], 3)] += 1
        total_dist[min(row['total'], 3)] += 1
        side_dist[min(row['side'], 3)] += 1
    return dict(code=code, name=name, records=valid,
                covered=sum(1 for row in valid if row['has_build']),
                current=len(valid), total_players=len(records), excluded=excluded,
                side_available=bool(valid),
                side_known=len(valid), side_unknown=0,
                main_known=len(valid), main_unknown=0,
                side_used=sum(1 for row in valid if row['side'] > 0),
                main_dist=main_dist, side_dist=side_dist, total_dist=total_dist,
                main_total=sum(row['main'] for row in valid),
                side_total=sum(row['side'] for row in valid),
                total_total=sum(row['total'] for row in valid))


def _pie(ax, items, title, prop):
    """items=[(label, value, color)]；小切片不再画外部标签，人数写在标题第二行。"""
    shown = [(label, value, color) for label, value, color in items if value]
    if not shown:
        shown = [('无记录', 1, '#D9DEE7')]
    values = [value for _, value, _ in shown]
    colors = [color for _, _, color in shown]
    _wedges, _texts, autotexts = ax.pie(
        values, labels=None, colors=colors, startangle=90, counterclock=False,
        autopct=lambda pct: f'{pct:.0f}%' if pct >= 4 else '',
        pctdistance=0.68,
        textprops={'fontproperties': prop} if prop is not None else None,
        wedgeprops={'linewidth': 0.8, 'edgecolor': 'white'})
    for text in autotexts:
        text.set_fontsize(9)
        text.set_color('white')
        text.set_fontweight('bold')
    detail = ' · '.join(f'{label} {value}人' for label, value, _ in shown)
    ax.set_title(f'{title}\n{detail}', fontproperties=prop, fontsize=13, pad=10)


def render(database, data_dir, now, card_name):
    payload = collect(database, data_dir, now, card_name)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    font_candidates = (
        Path('C:/Windows/Fonts/msyh.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
    )
    font_path = next((path for path in font_candidates if path.is_file()), None)
    prop = FontProperties(fname=str(font_path)) if font_path else None
    if prop is not None:
        plt.rcParams['font.family'] = prop.get_name()
    plt.rcParams['axes.unicode_minus'] = False
    palette = ['#AAB5C4', '#4E79A7', '#59A14F', '#E15759', '#B07AA1']
    unknown_color = '#D9DEE7'
    unknown = payload['side_unknown']

    def items(dist):
        return [(f'{key}张', dist[key], palette[key]) for key in (0, 1, 2, 3)]

    fig, axes = plt.subplots(1, 3, figsize=(18, 6.4), dpi=100)
    main_items = items(payload['main_dist'])
    if payload['main_unknown']:
        main_items = main_items + [('未知', payload['main_unknown'], unknown_color)]
    _pie(axes[0], main_items, '主卡投入（确证首局，人数）', prop)
    if payload['side_available']:
        side_items = items(payload['side_dist'])
        total_items = items(payload['total_dist'])
        if unknown:
            side_items = side_items + [('未知', unknown, unknown_color)]
        if payload['main_unknown']:
            total_items = total_items + [('未知', payload['main_unknown'], unknown_color)]
        _pie(axes[1], side_items, '备牌换入（证据，人数）', prop)
        _pie(axes[2], total_items, '总投入（主卡+换入，人数）', prop)
    else:
        _pie(axes[1], [('无换备证据', payload['current'], unknown_color)],
             '备牌换入（暂无证据）', prop)
        _pie(axes[2], [('无换备证据', payload['current'], unknown_color)],
             '总投入（暂无证据）', prop)
    fig.suptitle(f"前100使用量 · {payload['name']}（{payload['code']:08d}） · "
                 f"有构筑 {payload['covered']}/{payload['current']} 人",
                 fontproperties=prop, fontsize=20, y=0.99)
    fig.text(0.5, 0.015,
             '主卡=确证第 1 局；备牌=最近一场第 2/3 局相对第 1 局的净换入（录像无备牌表：未换入≠没带）；'
             f'主卡未知 {payload["main_unknown"]} 人、备牌未知 {unknown} 人；总投入=主卡+换入（下界）。',
             ha='center', fontproperties=prop, fontsize=10, color='#8A5A00')
    output = io.BytesIO()
    fig.savefig(output, format='png', dpi=100, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output.getvalue(), payload
