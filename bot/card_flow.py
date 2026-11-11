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
    """固定窄版分页，保持全部卡组、分布及原统计口径。"""
    payload = collect_flow(database, data_dir, now, card_name)
    return render_flow_pages(payload), payload


def render_flow_pages(payload):
    from PIL import Image, ImageDraw, ImageFont

    if not payload['shown']:
        raise ValueError('暂无带卡数据')
    font_path = _font()
    if font_path is None:
        raise ValueError('缺少中文字体，无法生成单卡流向图')
    fonts = {size: ImageFont.truetype(font_path.get_file(), size) for size in (23, 25, 28, 34)}
    width, limit, margin = 720, 840, 24
    colors = ('#4E79A7', '#EDC948', '#E15759')
    ink, muted = '#172B4D', '#52647A'
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))

    def wrap(text, size, available):
        lines, line = [], ''
        for char in str(text):
            if line and measure.textlength(line + char, font=fonts[size]) > available:
                lines.append(line)
                line = ''
            line += char
        return lines + [line]

    def bucket_text(bucket):
        if not bucket.get('known'):
            return '无证据'
        bits = [f'{n}张×{bucket[k]}人' for n, k in enumerate(BINS, 1) if bucket[k]]
        return '、'.join(bits) if bits else '0人'

    # 分页只作用于展示；不修改 collect_flow 的分组、顺序或分母。
    chart = [item for item in payload['shown'] if item['total']['known'] >= BAR_MIN_PLAYERS]
    sections = []
    for kind, groups in (('chart', chart), ('detail', payload['small_groups'])):
        blocks = []
        for group in groups:
            names = wrap(group['deck'], 28, width - 2 * margin)
            cells = [wrap(bucket_text(group[k]) + f" /{group[k]['known']}", 25, 208) for k in ('main', 'side', 'total')]
            block_height = len(names) * 36 + (132 if kind == 'chart' else 32 * max(map(len, cells))) + 22
            blocks.append((group, names, cells, block_height))
        page, used = [], 0
        for block in blocks:
            if page and used + block[3] > limit - 230:
                sections.append((kind, page))
                page, used = [], 0
            page.append(block)
            used += block[3]
        if page:
            sections.append((kind, page))

    output = []
    for page_no, (kind, blocks) in enumerate(sections, 1):
        title = wrap(payload['name'], 34, width - 2 * margin)
        header_height = 134 + 42 * len(title)
        height = header_height + sum(b[3] for b in blocks) + 66
        image = Image.new('RGB', (width, height), '#FFFFFF')
        draw = ImageDraw.Draw(image)

        def text(x, y, value, size=25, fill=ink):
            draw.text((x, y), str(value), font=fonts[size], fill=fill)

        y = 14
        for line in title:
            text(margin, y, line, 34)
            y += 42
        text(margin, y, f"主卡 {payload['main_used']}人 · 换入 {payload['side_used']}人 · 合计 {payload['total_used']}人", 25)
        y += 36
        section = '≥3人卡组 · 条长=带卡比例' if kind == 'chart' else '≤2人卡组 · 全部明细'
        text(margin, y, f'{section}    {page_no}/{len(sections)}', 23, muted)
        y += 36
        if kind == 'chart':
            for index, label in enumerate(('1张', '2张', '3张及以上')):
                x = margin + index * 208
                draw.rectangle((x, y + 5, x + 20, y + 23), fill=colors[index])
                text(x + 30, y, label, 23, muted)
        else:
            for i, label in enumerate(('主卡', '换入', '合计')):
                text(margin + i * 224, y, label + '（/分母）', 23, muted)
        y = header_height
        for group, names, cells, block_height in blocks:
            start = y
            draw.line((margin, y - 8, width - margin, y - 8), fill='#DDE5EE', width=2)
            for line in names:
                text(margin, y, line, 28)
                y += 36
            if kind == 'chart':
                for key, label in (('main', '主卡'), ('side', '换入'), ('total', '合计')):
                    bucket = group[key]
                    text(margin, y, label, 25, muted)
                    x0, x1 = 94, 552
                    draw.rectangle((x0, y + 3, x1, y + 34), fill='#E9EEF4')
                    left = float(x0)
                    for part, color in zip(BINS, colors):
                        count = bucket[part]
                        span = (x1 - x0) * count / bucket['known'] if bucket['known'] else 0
                        if span:
                            draw.rectangle((round(left), y + 3, round(left + span), y + 34), fill=color)
                            if span >= 34:
                                value = str(count)
                                tw = measure.textlength(value, font=fonts[23])
                                text(left + (span - tw) / 2, y + 1, value, 23,
                                     '#172B4D' if part == 'two' else '#FFFFFF')
                            left += span
                    status = f"{bucket['used']}/{bucket['known']}人" if bucket['known'] else '无证据'
                    text(566, y, status, 23, muted)
                    y += 42
            else:
                for i, (key, label) in enumerate((('main', '主卡'), ('side', '换入'), ('total', '合计'))):
                    x = margin + i * 224
                    for j, line in enumerate(cells[i]):
                        text(x, y + j * 32, line, 25)
            y = start + block_height
        text(margin, height - 60, '主卡=确证首局；换入=第2/3局相对首局净增', 23, muted)
        text(margin, height - 32, '未换入≠没带；合计人数去重；条内数字=人数', 23, muted)
        blob = io.BytesIO()
        image.save(blob, format='PNG')
        output.append(blob.getvalue())
    return output
