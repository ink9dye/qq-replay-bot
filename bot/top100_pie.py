# -*- coding: utf-8 -*-
"""前100卡组分布饼图（QQ 图片）。"""
from __future__ import annotations

import io
import sqlite3
from contextlib import closing
from pathlib import Path


def collect(database, replay_root, now):
    from .statistics_dashboard import query
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        data = query(db, Path(replay_root), float(now))
    items = [dict(name=str(row['name']), count=int(row['players']))
             for row in data.get('deck_distribution') or [] if int(row.get('players') or 0) > 0]
    items.sort(key=lambda row: (-row['count'], row['name']))
    return dict(items=items, covered=int(data.get('covered_players') or 0),
                current=int(data.get('current_players') or 0),
                unidentified=int(data.get('unidentified_players') or 0))


def render(database, replay_root, now):
    payload = collect(database, replay_root, now)
    items = payload['items']
    if not items:
        raise ValueError('暂无卡组档案')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    font_path = Path('C:/Windows/Fonts/msyh.ttc')
    prop = FontProperties(fname=str(font_path)) if font_path.exists() else None
    if prop is not None:
        plt.rcParams['font.family'] = prop.get_name()
    plt.rcParams['axes.unicode_minus'] = False

    total = sum(row['count'] for row in items)
    labels = [f"{row['name']} {row['count']}人" for row in items]
    values = [row['count'] for row in items]
    import colorsys
    def web_color(index, name):
        if name == '未识别':
            return '#AAB5C4'
        hue = ((210 + index * 137.508) % 360) / 360.0
        sat = (64 + (index * 7) % 14) / 100.0
        light = (49 + (index * 11) % 15) / 100.0
        return colorsys.hls_to_rgb(hue, light, sat)
    colors = [web_color(index, row['name']) for index, row in enumerate(items)]
    fig = plt.figure(figsize=(15, 12), dpi=100)
    ax = fig.add_axes([0.03, 0.06, 0.94, 0.84])
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, colors=colors, startangle=90, counterclock=False,
        autopct=lambda pct: f'{pct:.1f}%' if pct >= 1.5 else '',
        pctdistance=0.70, labeldistance=1.10,
        textprops={'fontproperties': prop, 'fontsize': 7} if prop else {'fontsize': 7},
        wedgeprops={'linewidth': 0.5, 'edgecolor': 'white'})
    for text in autotexts:
        text.set_fontsize(7)
        text.set_color('white')
        text.set_fontweight('bold')
    ax.set_title(f"前100卡组分布 · 有档案 {payload['covered']}/{payload['current']} 人 · "
                 f"已识别 {len(items)} 类", fontproperties=prop, fontsize=18, pad=18)
    output = io.BytesIO()
    fig.savefig(output, format='png', dpi=100, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output.getvalue(), payload
