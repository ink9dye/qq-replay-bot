# -*- coding: utf-8 -*-
"""前100单卡流向：按卡组大类统计带 1/2/3 张的比例。"""
from __future__ import annotations

import io
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def _latest_builds(db, now):
    cutoff = float(now) - 14 * 86400
    return [dict(row) for row in db.execute('''
        WITH ranked AS (
            SELECT t.rank, t.player, v.build, v.digest, r.recorded_at,
                   b.main_complete, b.side_complete, b.deck_type,
                   ROW_NUMBER() OVER (PARTITION BY t.player ORDER BY r.recorded_at DESC) AS rn
            FROM stat_current_top100 t
            JOIN stat_views v ON v.player = t.player
            JOIN stat_files f ON f.digest = v.digest
            JOIN replays r ON r.id = f.replay_id
            JOIN stat_builds b ON b.fingerprint = v.build
            WHERE r.recorded_at >= ? AND b.main_complete = 1
        )
        SELECT rank, player, build, digest, recorded_at, main_complete, side_complete, deck_type
        FROM ranked WHERE rn = 1
    ''', (cutoff,))]


def collect_flow(database, data_dir, now, card_name):
    from .card_usage_pie import resolve_card
    from .statistics_dashboard import population
    code, name = resolve_card(data_dir, card_name)
    replay_root = Path(database).parent / 'replays'
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        basis, latest, people = population(db, replay_root, now)
        records = []
        for person in people:
            player = person['player']
            row = latest.get(player)
            main = side = 0
            if row and row.get('build'):
                for count in db.execute(
                        'SELECT partition, SUM(copies) AS copies FROM stat_build_cards '
                        'WHERE build=? AND code=? GROUP BY partition', (row['build'], code)):
                    if count['partition'] == 'main':
                        main = int(count['copies'] or 0)
                    elif count['partition'] == 'side':
                        side = int(count['copies'] or 0)
            total = min(3, main + side)
            deck = person.get('deck_type') or '未分类'
            records.append(dict(player=player, rank=person.get('rank'), deck=deck,
                                copies=total, main=main, side=side,
                                has_build=bool(row)))
    groups = {}
    for row in records:
        group = groups.setdefault(row['deck'], dict(deck=row['deck'], total=0, one=0, two=0, three=0, used=0))
        group['total'] += 1
        if row['copies'] >= 3:
            group['three'] += 1
            group['used'] += 1
        elif row['copies'] == 2:
            group['two'] += 1
            group['used'] += 1
        elif row['copies'] == 1:
            group['one'] += 1
            group['used'] += 1
    items = list(groups.values())
    for item in items:
        item['rate'] = item['used'] / item['total'] if item['total'] else 0
    items.sort(key=lambda item: (-item['rate'], -item['total'], item['deck']))
    return dict(code=code, name=name, items=items, records=records,
                covered=sum(1 for row in records if row['has_build']),
                current=len(records), used=sum(1 for row in records if row['copies'] > 0))


def _font():
    from matplotlib.font_manager import FontProperties
    path = Path('C:/Windows/Fonts/msyh.ttc')
    return FontProperties(fname=str(path)) if path.exists() else None


def render_flow(database, data_dir, now, card_name):
    payload = collect_flow(database, data_dir, now, card_name)
    items = payload['items']
    if not items:
        raise ValueError('暂无卡组大类数据')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    prop = _font()
    if prop is not None:
        plt.rcParams['font.family'] = prop.get_name()
    plt.rcParams['axes.unicode_minus'] = False
    categories = [item['deck'] for item in items]
    positions = list(range(len(items)))
    height = max(7.2, 0.44 * len(items) + 2.2)
    fig, ax = plt.subplots(figsize=(12.2, height), dpi=100)
    ax.set_title(f'「{payload["name"]}」各大类带卡率', fontproperties=prop, fontsize=18, pad=16)
    for pos, item in zip(positions, items):
        total = item['total']
        one = item['one'] / total * 100 if total else 0
        two = item['two'] / total * 100 if total else 0
        three = item['three'] / total * 100 if total else 0
        ax.barh(pos, 100, color='#ECEFF3', edgecolor='white', height=0.72)
        left = 0
        for value, color, label in ((one, '#4E79A7', '带1张'), (two, '#EDC948', '带2张'),
                                    (three, '#E15759', '带3张及以上')):
            if value <= 0:
                continue
            ax.barh(pos, value, left=left, color=color, edgecolor='white', height=0.72, label=label)
            if value >= 4:
                ax.text(left + value / 2, pos, f'{value:.1f}'.rstrip('0').rstrip('.'),
                        ha='center', va='center', fontsize=8, color='black' if color == '#EDC948' else 'white')
            left += value
        ax.text(101.5, pos, f"{item['rate'] * 100:.0f}%", va='center', ha='left',
                fontproperties=prop, fontsize=9)
    ax.set_yticks(positions)
    ax.set_yticklabels([f"{item['deck']} ({item['used']}/{item['total']})" for item in items],
                       fontproperties=prop, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 112)
    ax.set_xlabel('占该大类人数比例（%）', fontproperties=prop, fontsize=10)
    ax.tick_params(axis='x', labelsize=8)
    from matplotlib.patches import Patch
    handles = [Patch(facecolor='#4E79A7', edgecolor='white', label='带1张'),
               Patch(facecolor='#EDC948', edgecolor='white', label='带2张'),
               Patch(facecolor='#E15759', edgecolor='white', label='带3张及以上'),
               Patch(facecolor='#ECEFF3', edgecolor='white', label='该大类全体（灰底轨）')]
    ax.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, -0.08), ncol=4,
              frameon=False, prop=prop, fontsize=9)
    fig.text(0.01, 0.01,
             f'「{payload["name"]}」前100名：{payload["used"]}/100 人带 · 各大类带卡率'
             f'（主卡+备牌合计，全部 {len(items)} 个大类；'
             f'大类标签=总使用人数/大类总人数 · 蓝=1张/黄=2张/红=3张及以上）',
             fontproperties=prop, fontsize=8, color='#555555')
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    output = io.BytesIO()
    fig.savefig(output, format='png', dpi=100, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output.getvalue(), payload
