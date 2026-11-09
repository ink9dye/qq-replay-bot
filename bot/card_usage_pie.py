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


def _latest_builds(db, now):
    cutoff = float(now) - 14 * 86400
    rows = db.execute('''
        WITH ranked AS (
            SELECT t.rank, t.player, v.build, v.digest, r.recorded_at,
                   b.main_complete, b.side_complete,
                   ROW_NUMBER() OVER (PARTITION BY t.player ORDER BY r.recorded_at DESC) AS rn
            FROM stat_current_top100 t
            JOIN stat_views v ON v.player = t.player
            JOIN stat_files f ON f.digest = v.digest
            JOIN replays r ON r.id = f.replay_id
            JOIN stat_builds b ON b.fingerprint = v.build
            WHERE r.recorded_at >= ? AND b.main_complete = 1
        )
        SELECT rank, player, build, digest, recorded_at, main_complete, side_complete
        FROM ranked WHERE rn = 1
    ''', (cutoff,)).fetchall()
    return [dict(row) for row in rows]


def collect(database, data_dir, now, card_name):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        code, name = resolve_card(data_dir, card_name, db)
        codes = identity_codes(db, code)
        marks = ','.join('?' for _ in codes)
        builds = _latest_builds(db, now)
        players = {row['player']: row for row in builds}
        top = [dict(row) for row in db.execute(
            'SELECT player, rank FROM stat_current_top100 ORDER BY rank')]
        for row in top:
            players.setdefault(row['player'], {'player': row['player'], 'rank': row['rank'],
                                               'build': None, 'digest': None})
        records = []
        for player, row in players.items():
            main = side = 0
            if row.get('build'):
                for count in db.execute(
                        'SELECT partition, SUM(copies) AS copies FROM stat_build_cards '
                        f'WHERE build=? AND code IN ({marks}) GROUP BY partition',
                        [row['build'], *codes]):
                    if count['partition'] == 'main':
                        main = int(count['copies'] or 0)
                    elif count['partition'] == 'side':
                        side = int(count['copies'] or 0)
            records.append(dict(player=player, rank=int(row.get('rank') or 999),
                                main=main, side=side, total=main + side,
                                build=row.get('build'), has_build=bool(row.get('build'))))
    records.sort(key=lambda item: item['rank'])
    main_dist = {0: 0, 1: 0, 2: 0, 3: 0}
    side_dist = {0: 0, 1: 0, 2: 0, 3: 0}
    total_dist = {0: 0, 1: 0, 2: 0, 3: 0}
    for row in records:
        main_dist[min(row['main'], 3)] += 1
        side_dist[min(row['side'], 3)] += 1
        total_dist[min(row['total'], 3)] += 1
    side_available = any(row['side'] for row in records)
    return dict(code=code, name=name, records=records,
                covered=sum(1 for row in records if row['has_build']),
                current=len(records), side_available=side_available,
                main_dist=main_dist, side_dist=side_dist, total_dist=total_dist,
                main_total=sum(row['main'] for row in records),
                side_total=sum(row['side'] for row in records),
                total_total=sum(row['total'] for row in records))


def _pie(ax, values, labels, title, colors, prop):
    labels = [label for label, value in zip(labels, values) if value]
    values = [value for value in values if value]
    if not values:
        values, labels = [1], ['无记录']
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, colors=colors, startangle=90, counterclock=False,
        autopct=lambda pct: f'{pct:.0f}%' if pct >= 4 else '',
        pctdistance=0.68, labeldistance=1.12,
        textprops={'fontproperties': prop, 'fontsize': 9} if prop else {'fontsize': 9},
        wedgeprops={'linewidth': 0.8, 'edgecolor': 'white'})
    for text in autotexts:
        text.set_fontsize(9)
        text.set_color('white')
        text.set_fontweight('bold')
    ax.set_title(title, fontproperties=prop, fontsize=15, pad=12)


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
    colors = ['#AAB5C4', '#4E79A7', '#59A14F', '#E15759', '#B07AA1', '#EDC948']

    def labels(dist, suffix='张'):
        return [f'{key}{suffix} {value}人' for key, value in dist.items()]

    fig, axes = plt.subplots(1, 3, figsize=(18, 6.4), dpi=100)
    _pie(axes[0], [payload['main_dist'][key] for key in (0, 1, 2, 3)],
         labels(payload['main_dist']), '主卡投入（人数）', colors, prop)
    if payload['side_available']:
        _pie(axes[1], [payload['side_dist'][key] for key in (0, 1, 2, 3)],
             labels(payload['side_dist']), '备牌投入（人数）', colors, prop)
    else:
        _pie(axes[1], [payload['current']], ['备牌无记录'], '备牌投入（暂无记录）',
             ['#D9DEE7'], prop)
    _pie(axes[2], [payload['total_dist'][key] for key in (0, 1, 2, 3)],
         labels(payload['total_dist'], '张'), '总投入（主+备，人数）', colors, prop)
    fig.suptitle(f"前100使用量 · {payload['name']}（{payload['code']:08d}） · "
                 f"有构筑 {payload['covered']}/{payload['current']} 人",
                 fontproperties=prop, fontsize=20, y=0.99)
    if not payload['side_available']:
        fig.text(0.5, 0.015, '当前构筑库没有备牌记录，备牌图仅占位；总投入暂等于主卡投入。',
                 ha='center', fontproperties=prop, fontsize=11, color='#8A5A00')
    output = io.BytesIO()
    fig.savefig(output, format='png', dpi=100, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output.getvalue(), payload
