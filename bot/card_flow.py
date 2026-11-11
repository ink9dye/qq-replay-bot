# -*- coding: utf-8 -*-
"""前100单卡流向：按卡组大类分别看主卡 / 备牌换入 / 合计的张数分布。"""
from __future__ import annotations

import io
import sqlite3
import time
from contextlib import closing
from pathlib import Path

BINS = ('one', 'two', 'three')


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
    for item in shown:
        for key in ('main', 'side', 'total'):
            bucket = item[key]
            bucket['rate'] = bucket['used'] / bucket['known'] if bucket['known'] else 0
    return dict(code=code, name=name, items=items, shown=shown, records=records,
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


def _panel(payload, shown, key, title, note, prop):
    """单张图：一个大类一行人，堆叠显示 1/2/3 张及以上。"""
    import io
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    colors = {'one': '#4E79A7', 'two': '#EDC948', 'three': '#E15759'}
    labels = {'one': '带1张', 'two': '带2张', 'three': '带3张及以上'}
    height = max(6.4, 0.5 * len(shown) + 2.8)
    fig, ax = plt.subplots(figsize=(13.0, height), dpi=100)
    fig.subplots_adjust(left=0.24, right=0.97, top=0.86, bottom=0.2)
    positions = list(range(len(shown)))
    any_value = False
    for pos, item in zip(positions, shown):
        bucket = item[key]
        known = bucket['known']
        ax.barh(pos, 100, color='#ECEFF3', edgecolor='white', height=0.72)
        left = 0.0
        for part in BINS:
            value = bucket[part] / known * 100 if known else 0
            if value <= 0:
                continue
            any_value = True
            ax.barh(pos, value, left=left, color=colors[part], edgecolor='white', height=0.72)
            if value >= 6:
                ax.text(left + value / 2, pos, f'{value:.0f}', ha='center', va='center',
                        fontsize=9, color='black' if part == 'two' else 'white')
            left += value
        ax.text(101.5, pos, f"{bucket['used']}人 · {bucket['rate'] * 100:.0f}%",
                va='center', ha='left', fontproperties=prop, fontsize=9)
    if not any_value:
        ax.text(50, (len(shown) - 1) / 2, '无换入证据', ha='center', va='center',
                fontproperties=prop, fontsize=15, color='#8A5A00')
    ax.set_yticks(positions)
    ax.set_yticklabels(
        [(item['deck'] if len(item['deck']) <= 18 else item['deck'][:17] + '…')
         + f" ({item[key]['used']}/{item['players']})" for item in shown],
        fontproperties=prop, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlim(0, 118)
    ax.set_xlabel('占该大类人数比例（%）', fontproperties=prop, fontsize=10)
    ax.tick_params(axis='x', labelsize=8)
    handles = [Patch(facecolor=colors[part], edgecolor='white', label=labels[part])
               for part in BINS]
    handles.append(Patch(facecolor='#ECEFF3', edgecolor='white', label='该大类全体（灰底轨）'))
    ax.legend(handles=handles, loc='lower center', ncol=4, frameon=False,
              prop=prop, fontsize=9, bbox_to_anchor=(0.5, -0.2))
    ax.set_title(f"「{payload['name']}」{title}", fontproperties=prop, fontsize=16, pad=12)
    fig.text(0.5, 0.02, note, ha='center', fontproperties=prop, fontsize=9, color='#555555')
    output = io.BytesIO()
    fig.savefig(output, format='png', dpi=100, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output.getvalue()


def render_flow(database, data_dir, now, card_name):
    """返回三张图：主卡 / 备牌（换入）/ 合计，QQ 端分三条图片发送。"""
    payload = collect_flow(database, data_dir, now, card_name)
    shown = payload['shown']
    if not shown:
        raise ValueError('暂无带卡数据')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    prop = _font()
    plt.rcParams['axes.unicode_minus'] = False
    shown_count = f'只显示合计有人带卡的 {len(shown)} 个卡组大类（共 {len(payload["items"])} 类）。'
    parts = {
        'main': _panel(
            payload, shown, 'main', f'主卡（确证首局）：{payload["main_used"]} 人',
            '主卡=每人最近一场确证第 1 局的张数；分母为各大类有确证首局的人数。' + shown_count, prop),
        'side': _panel(
            payload, shown, 'side', f'备牌（换入证据）：{payload["side_used"]} 人',
            '备牌=第 2/3 局相对第 1 局的净换入（录像无备牌表：未换入≠没带）；分母同上。' + shown_count, prop),
        'total': _panel(
            payload, shown, 'total', f'合计（主卡+换入）：{payload["total_used"]} 人',
            '合计=主卡+换入（下界）；分母为各大类有确证首局的人数。' + shown_count, prop),
    }
    return parts, payload

