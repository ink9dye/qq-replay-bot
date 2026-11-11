# -*- coding: utf-8 -*-
"""前100单卡流向：主卡 / 备牌换入 / 合计 一张三栏图；≤2 人的小卡组列在图下方。"""
from __future__ import annotations

import io
import sqlite3
import time
from contextlib import closing
from pathlib import Path

BINS = ('one', 'two', 'three')
BAR_MIN_PLAYERS = 3  # 少于 3 人的大类不画条形，改在图下方列出


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
    small_groups = sorted(
        [dict(deck=item['deck'], players=item['players'],
              main=dict(item['main']), side=dict(item['side']), total=dict(item['total']))
         for item in shown if item['total']['known'] < BAR_MIN_PLAYERS],
        key=lambda group: (-group['total']['used'], group['deck']))
    bars = {}
    for key in ('main', 'side', 'total'):
        bars[key] = [item for item in shown
                     if item[key]['used'] > 0 and item[key]['known'] >= BAR_MIN_PLAYERS]
        bars[key].sort(key=lambda item: (-item[key]['rate'], -item[key]['used'], item['deck']))
    return dict(code=code, name=name, items=items, shown=shown, records=records,
                small_groups=small_groups, bars=bars,
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


def _bucket_text(bucket, label):
    if not bucket or not bucket.get('known'):
        return f"{label} 无证据"
    bits = []
    for copies, key in ((1, 'one'), (2, 'two'), (3, 'three')):
        count = bucket.get(key) or 0
        if count:
            bits.append(f"{copies}张({count}/{bucket['known']})")
    return f"{label} " + ("、".join(bits) if bits else "无")


def _group_line(group):
    return (f"{group['deck']} {group['total']['used']}/{group['total']['known']}："
            + _bucket_text(group['main'], '主卡') + "；"
            + _bucket_text(group['side'], 'side') + "；"
            + _bucket_text(group['total'], '合计'))


def render_flow(database, data_dir, now, card_name):
    """一张图：上面三栏条形（≥3 人的卡组），下面列出 ≤2 人卡组的完整明细。"""
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
    rows = {}
    for key in ('main', 'side', 'total'):
        for item in payload['bars'][key]:
            rows[item['deck']] = item
    ordered = sorted(rows.values(),
                     key=lambda item: (-item['total']['rate'], -item['total']['used'], item['deck']))
    positions = list(range(len(ordered)))
    lines = [_group_line(group) for group in payload['small_groups']]

    # ---- 尺寸（英寸）----
    max_name = max((len(item['deck']) for item in ordered), default=6)
    side_in = min(7.2, 0.14 * max_name + 1.2)
    panel_in = 3.25
    gap_in = 0.18
    title_in = 1.10
    chart_in = max(2.6, 0.55 * max(len(ordered), 1) + 1.10)
    legend_in = 0.78
    text_in = 0.19 * len(lines) + (0.40 if lines else 0.0)
    note_in = 0.44
    width = 3 * panel_in + 2 * gap_in + 2 * side_in
    height = title_in + chart_in + legend_in + text_in + note_in
    fig = plt.figure(figsize=(width, height), dpi=100)

    left = side_in / width
    right = 1 - side_in / width
    bottom = (text_in + legend_in + note_in + 0.26) / height
    top = 1 - title_in / height
    gap = gap_in / width
    panel_w = (right - left - 2 * gap) / 3.0
    axes = [fig.add_axes([left + index * (panel_w + gap), bottom, panel_w, top - bottom])
            for index in range(3)]

    def in_chart(key):
        return sum(item[key]['used'] for item in ordered
                   if item[key]['used'] > 0 and item[key]['known'] >= BAR_MIN_PLAYERS)

    titles = {'main': f"主卡（确证首局）{payload['main_used']} 人 · 图内 {in_chart('main')} 人",
              'side': f"备牌（换入）{payload['side_used']} 人 · 图内 {in_chart('side')} 人",
              'total': f"合计（主卡+换入）{payload['total_used']} 人 · 图内 {in_chart('total')} 人"}
    for ax, key in zip(axes, ('main', 'side', 'total')):
        ax.set_title(titles[key], fontproperties=prop, fontsize=13, pad=10)
        for pos, item in zip(positions, ordered):
            bucket = item[key]
            known = bucket['known']
            ax.barh(pos, 100, color='#ECEFF3', edgecolor='white', height=0.48)
            if not (bucket['used'] > 0 and known >= BAR_MIN_PLAYERS):
                continue
            start = 0.0
            for part in BINS:
                count = bucket[part]
                if count <= 0:
                    continue
                span = count / known * 100
                ax.barh(pos, span, left=start, color=colors[part], edgecolor='white', height=0.48)
                if span >= 9:
                    ax.text(start + span / 2, pos, f'{count}人', ha='center', va='center',
                            fontproperties=prop, fontsize=8.5,
                            color='black' if part == 'two' else 'white')
                start += span
        ax.set_xlim(0, 100)
        ax.tick_params(axis='x', labelsize=8.5)
        if not any(item[key]['used'] > 0 and item[key]['known'] >= BAR_MIN_PLAYERS for item in ordered):
            ax.text(0.5, 0.97, '这一栏没有 ≥3 人的卡组', transform=ax.transAxes, ha='center', va='top',
                    fontproperties=prop, fontsize=9, color='#8A5A00')

    tick_labels = [f"{item['deck']} ({item['total']['used']}/{item['total']['known']})"
                   for item in ordered]
    axes[0].set_yticks(positions)
    axes[0].set_yticklabels(tick_labels, fontproperties=prop, fontsize=9)
    for ax in axes:
        ax.set_ylim(len(ordered) - 0.5, -0.5)
    axes[1].tick_params(axis='y', labelleft=False)
    axes[2].yaxis.set_ticks_position('right')
    axes[2].yaxis.set_label_position('right')
    axes[2].set_yticks(positions)
    axes[2].set_yticklabels(tick_labels, fontproperties=prop, fontsize=9)
    axes[2].tick_params(axis='y', pad=4)

    handles = [Patch(facecolor=colors[part], edgecolor='white', label=labels[part]) for part in BINS]
    handles.append(Patch(facecolor='#ECEFF3', edgecolor='white', label='该大类全体（灰底轨）'))
    fig.legend(handles=handles, loc='center', ncol=4, frameon=False, prop=prop, fontsize=9.5,
               bbox_to_anchor=(0.5, bottom - 0.80 * legend_in / height))
    fig.suptitle(f"「{payload['name']}」各大类：主卡 {payload['main_used']} 人 / "
                 f"备牌换入 {payload['side_used']} 人 / 合计 {payload['total_used']} 人",
                 fontproperties=prop, fontsize=17, y=1 - 0.18 / height)
    if lines:
        header = f"≤{BAR_MIN_PLAYERS - 1} 人卡组（{len(lines)} 个，全列；括号=有该张数的玩家数/该卡组人数）："
        fig.text(0.006, (note_in + text_in - 0.02) / height, "\n".join([header] + lines),
                 ha='left', va='top', fontproperties=prop, fontsize=9.0, linespacing=1.35,
                 color='#222222')
    fig.text(0.5, 0.006,
             f'条形只画 ≥{BAR_MIN_PLAYERS} 人的卡组（长度=该大类带卡率，条内=玩家数）；'
             '备牌=第 2/3 局相对第 1 局的净换入（未换入≠没带）；分母为各大类有确证首局的人数。',
             ha='center', fontproperties=prop, fontsize=8.5, color='#555555')
    output = io.BytesIO()
    fig.savefig(output, format='png', dpi=100, facecolor='white')
    plt.close(fig)
    return output.getvalue(), payload
