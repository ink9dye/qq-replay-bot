# -*- coding: utf-8 -*-
"""前100单卡流向：主卡 / 备牌换入 / 合计 一张三栏图；≤2 人的小卡组用文字描述。"""
from __future__ import annotations

import io
import sqlite3
import time
from contextlib import closing
from pathlib import Path

BINS = ('one', 'two', 'three')
BAR_MIN_PLAYERS = 3  # 少于 3 人的大类不画条形，由消息文本描述


def collect_flow(database, data_dir, now, card_name):
    from .card_usage_pie import _latest_sessions, _main_copies, identity_codes, resolve_card
    from .statistics_dashboard import population
    replay_root = Path(database).parent / 'replays'
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        code, name = resolve_card(data_dir, card_name, db)
        codes = identity_codes(db, code)
        basis, latest, people = population(db, replay_root, now)
        sessions = _latest_sessions(basis['views'])
        builds = {row.get('build') for rows in sessions.values() for row in rows}
        builds |= {(latest.get(person['player']) or {}).get('build') for person in people}
        copies = _main_copies(db, builds, codes)
        records = []
        for person in people:
            player = person['player']
            row = latest.get(player)
            if not row or not row.get('build'):
                # 没有可用首局的场次：按规则忽略，不进任何分母。
                continue
            build = row['build']
            main = copies.get(build, 0)
            games = sessions.get(player) or []
            first = [r for r in games if r.get('observed_game_no') == 1
                     and r.get('observed_identity_confirmed') is True]
            later = [r for r in games if (r.get('observed_game_no') or 0) > 1]
            side = 0
            side_known = False
            if first and later:
                later_counts = [copies.get(r.get('build'), 0) for r in later]
                side = max(0, max(later_counts) - main)
                side_known = True
            records.append(dict(player=player, rank=person.get('rank'),
                                deck=person.get('deck_type') or '未分类',
                                main=main, side=side, side_known=side_known,
                                total=main + side))
    groups = {}
    for row in records:
        group = groups.setdefault(row['deck'], dict(
            deck=row['deck'], players=0,
            main=dict(known=0, used=0, one=0, two=0, three=0),
            side=dict(known=0, used=0, one=0, two=0, three=0),
            total=dict(known=0, used=0, one=0, two=0, three=0)))
        group['players'] += 1
        for key, value in (('main', row['main']), ('total', row['total'])):
            bucket = group[key]
            bucket['known'] += 1
            if value > 0:
                bucket['used'] += 1
                bucket['three' if value >= 3 else 'two' if value == 2 else 'one'] += 1
        if row['side_known']:
            bucket = group['side']
            bucket['known'] += 1
            if row['side'] > 0:
                bucket['used'] += 1
                bucket['three' if row['side'] >= 3 else 'two' if row['side'] == 2 else 'one'] += 1
    for group in groups.values():
        for key in ('main', 'side', 'total'):
            bucket = group[key]
            bucket['rate'] = bucket['used'] / bucket['known'] if bucket['known'] else 0
    items = sorted(groups.values(),
                   key=lambda item: (-item['total']['rate'], -item['total']['used'], item['deck']))
    shown = [item for item in items if item['total']['used'] > 0]
    shown.sort(key=lambda item: (-item['total']['rate'], -item['total']['used'], item['deck']))
    small = {}
    for key in ('main', 'side', 'total'):
        small[key] = sorted(
            [dict(deck=item['deck'], used=item[key]['used'], known=item[key]['known'])
             for item in shown if item[key]['used'] > 0 and item[key]['known'] < BAR_MIN_PLAYERS],
            key=lambda row: (-row['used'], row['deck']))
    bars = {}
    for key in ('main', 'side', 'total'):
        bars[key] = [item for item in shown
                     if item[key]['used'] > 0 and item[key]['known'] >= BAR_MIN_PLAYERS]
        bars[key].sort(key=lambda item: (-item[key]['rate'], -item[key]['used'], item['deck']))
    return dict(code=code, name=name, items=items, shown=shown, records=records,
                small=small, bars=bars,
                covered=sum(1 for row in records if row),
                current=len(records), total_players=len(people),
                main_used=sum(1 for row in records if row['main'] > 0),
                side_used=sum(1 for row in records if row['side'] > 0),
                total_used=sum(1 for row in records if row['total'] > 0))


def _font():
    from matplotlib.font_manager import FontProperties
    candidates = (
        Path('C:/Windows/Fonts/msyh.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
    )
    path = next((item for item in candidates if item.is_file()), None)
    return FontProperties(fname=str(path)) if path else None


def _short(name, limit=16):
    return name if len(name) <= limit else name[:limit - 1] + '…'


def render_flow(database, data_dir, now, card_name):
    """一张图三栏：主卡 / 备牌（换入）/ 合计；只画 ≥3 人的卡组。"""
    payload = collect_flow(database, data_dir, now, card_name)
    if not payload['shown']:
        raise ValueError('暂无带卡数据')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    prop = _font()
    plt.rcParams['axes.unicode_minus'] = False
    colors = {'one': '#4E79A7', 'two': '#EDC948', 'three': '#E15759'}
    labels = {'one': '带1张', 'two': '带2张', 'three': '带3张及以上'}
    # 行 = 三栏里任何一栏达到「≥3 人且有人带」的卡组，按合计带卡率排序。
    rows = {}
    for key in ('main', 'side', 'total'):
        for item in payload['bars'][key]:
            rows[item['deck']] = item
    ordered = sorted(rows.values(),
                     key=lambda item: (-item['total']['rate'], -item['total']['used'], item['deck']))
    positions = list(range(len(ordered)))
    height = max(3.2, 0.42 * max(len(ordered), 1) + 1.9)
    fig, axes = plt.subplots(1, 3, figsize=(12.2, height), dpi=100, sharey=True)
    fig.subplots_adjust(left=0.19, right=0.99, top=0.78, bottom=0.24)
    titles = {'main': f"主卡（确证首局）{payload['main_used']} 人",
              'side': f"备牌（换入）{payload['side_used']} 人",
              'total': f"合计（主卡+换入）{payload['total_used']} 人"}
    for ax, key in zip(axes, ('main', 'side', 'total')):
        ax.set_title(titles[key], fontproperties=prop, fontsize=12, pad=8)
        for pos, item in zip(positions, ordered):
            bucket = item[key]
            known = bucket['known']
            enabled = bucket['used'] > 0 and known >= BAR_MIN_PLAYERS
            ax.barh(pos, 100, color='#ECEFF3', edgecolor='white', height=0.48)
            if not enabled:
                continue
            left = 0.0
            for part in BINS:
                value = bucket[part] / known * 100 if known else 0
                if value <= 0:
                    continue
                ax.barh(pos, value, left=left, color=colors[part], edgecolor='white', height=0.48)
                if value >= 8:
                    ax.text(left + value / 2, pos, f'{value:.0f}', ha='center', va='center',
                            fontsize=7.5, color='black' if part == 'two' else 'white')
                left += value
        ax.set_xlim(0, 100)
        ax.tick_params(axis='x', labelsize=7)
        if not any(item[key]['used'] > 0 and item[key]['known'] >= BAR_MIN_PLAYERS for item in ordered):
            ax.text(50, (len(ordered) - 1) / 2, '没有 ≥3 人的卡组', ha='center', va='center',
                    fontproperties=prop, fontsize=11, color='#8A5A00')
    axes[0].set_yticks(positions)
    axes[0].set_yticklabels([f"{_short(item['deck'], 13)} ({item['total']['used']}/{item['total']['known']})"
                             for item in ordered], fontproperties=prop, fontsize=8)
    axes[0].invert_yaxis()
    handles = [Patch(facecolor=colors[part], edgecolor='white', label=labels[part]) for part in BINS]
    handles.append(Patch(facecolor='#ECEFF3', edgecolor='white', label='该大类全体（灰底轨）'))
    fig.legend(handles=handles, loc='lower center', ncol=4, frameon=False, prop=prop, fontsize=8,
               bbox_to_anchor=(0.5, 0.07))
    fig.suptitle(f"「{payload['name']}」各大类：主卡 {payload['main_used']} 人 / "
                 f"备牌换入 {payload['side_used']} 人 / 合计 {payload['total_used']} 人",
                 fontproperties=prop, fontsize=15, y=0.98)
    fig.text(0.5, 0.012,
             f'只画 ≥{BAR_MIN_PLAYERS} 人的卡组（≤{BAR_MIN_PLAYERS - 1} 人的见消息文字）；'
             '备牌=第 2/3 局相对第 1 局的净换入（未换入≠没带）；分母为各大类有确证首局的人数。',
             ha='center', fontproperties=prop, fontsize=7.5, color='#555555')
    output = io.BytesIO()
    fig.savefig(output, format='png', dpi=100, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output.getvalue(), payload
