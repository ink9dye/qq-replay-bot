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




def _font_file():
    prop = _font()
    if prop is None:
        raise ValueError('缺少中文字体，无法生成单卡流向图')
    return prop.get_file()


SIZES = dict(title=44, sub=30, section=28, name=30, body=24, cell=23, foot=22)


def _fonts():
    from PIL import ImageFont
    path = _font_file()
    return {key: ImageFont.truetype(path, size) for key, size in SIZES.items()}


def _wrap(draw, text, font, width):
    lines, line = [], ''
    for char in str(text):
        if line and draw.textlength(line + char, font=font) > width:
            lines.append(line)
            line = ''
        line += char
    return lines + [line] if line else lines


def _cell(bucket):
    if not bucket or not bucket.get('known'):
        return '无证据'
    bits = [f'{copies}张×{bucket[key]}人' for copies, key in ((1, 'one'), (2, 'two'), (3, 'three'))
            if bucket.get(key)]
    return ('、'.join(bits) if bits else '0人') + f" /{bucket['known']}"


BOX_COLORS = ('#4E79A7', '#EDC948', '#E15759')
INK, MUTED, RULE = '#172B4D', '#52647A', '#DDE5EE'
PAGE_W, MARGIN = 1500, 32


def _chart_image(payload, groups, fonts):
    from PIL import Image, ImageDraw

    width = PAGE_W
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    blocks = []
    for group in groups:
        names = _wrap(measure, group['deck'], fonts['name'], width - 2 * MARGIN)
        height = len(names) * 40 + 3 * 50 + 24
        blocks.append((group, names, height))
    header = 52 + 44 + 44 + 56
    footer = 96
    height = header + sum(block[2] for block in blocks) + footer
    image = Image.new('RGB', (width, height), '#FFFFFF')
    draw = ImageDraw.Draw(image)

    def text(x, y, value, font='body', fill=INK):
        draw.text((x, y), str(value), font=fonts[font], fill=fill)

    y = 18
    for line in _wrap(draw, f"「{payload['name']}」单卡流向", fonts['title'], width - 2 * MARGIN):
        text(MARGIN, y, line, 'title')
        y += 52
    text(MARGIN, y, f"主卡 {payload['main_used']} 人 · 备牌换入 {payload['side_used']} 人 · "
                    f"合计 {payload['total_used']} 人", 'sub')
    y += 44
    text(MARGIN, y, f"≥3 人的卡组：条形长度=该卡组带卡率，条内数字=人数（{len(groups)} 个）", 'section', MUTED)
    y += 40
    for index, label in enumerate(('1张', '2张', '3张及以上')):
        x = MARGIN + index * 210
        draw.rectangle((x, y + 6, x + 24, y + 30), fill=BOX_COLORS[index])
        text(x + 34, y + 2, label, 'cell', MUTED)
    y = header
    bar_x0, bar_x1 = MARGIN + 120, width - MARGIN - 210
    for group, names, block_height in blocks:
        draw.line((MARGIN, y - 10, width - MARGIN, y - 10), fill=RULE, width=2)
        for line in names:
            text(MARGIN, y, line, 'name')
            y += 40
        for row, key in enumerate(('main', 'side', 'total')):
            bucket = group[key]
            label = ('主卡', '换入', '合计')[row]
            text(MARGIN, y + 8, label, 'body', MUTED)
            by = y + 6
            draw.rectangle((bar_x0, by, bar_x1, by + 34), fill='#E9EEF4')
            left = float(bar_x0)
            known = bucket['known']
            for part, color in zip(('one', 'two', 'three'), BOX_COLORS):
                count = bucket[part]
                if not count or not known:
                    continue
                span = (bar_x1 - bar_x0) * count / known
                draw.rectangle((round(left), by, round(left + span), by + 34), fill=color)
                if span >= 40:
                    value = str(count)
                    tw = draw.textlength(value, font=fonts['cell'])
                    text(left + (span - tw) / 2, by + 2, value, 'cell',
                         '#172B4D' if part == 'two' else '#FFFFFF')
                left += span
            status = f"{bucket['used']}/{known}人" if known else '无证据'
            text(bar_x1 + 16, y + 8, status, 'cell', MUTED)
            y += 50
        y += 24
    text(MARGIN, height - footer + 24, '主卡=确证首局；换入=第2/3局相对首局净增；未换入≠没带', 'foot', MUTED)
    text(MARGIN, height - footer + 58, '条形只画 ≥3 人的卡组；≤2 人的见下一张明细', 'foot', MUTED)
    return image


def _detail_image(payload, groups, fonts):
    from PIL import Image, ImageDraw

    width = PAGE_W
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    name_w = 660
    cell_w = (width - 2 * MARGIN - name_w) // 3
    rows = []
    for group in groups:
        names = _wrap(measure, group['deck'], fonts['name'], name_w - 12)
        cells = []
        for key in ('main', 'side', 'total'):
            cells.append(_wrap(measure, _cell(group[key]), fonts['cell'], cell_w - 12))
        rows.append((names, cells, max(len(names), *(len(c) for c in cells))))
    header = 52 + 44 + 44 + 56
    footer = 96
    height = header + sum(rows_i[2] * 36 + 18 for rows_i in rows) + footer
    image = Image.new('RGB', (width, height), '#FFFFFF')
    draw = ImageDraw.Draw(image)

    def text(x, y, value, font='body', fill=INK):
        draw.text((x, y), str(value), font=fonts[font], fill=fill)

    y = 18
    for line in _wrap(draw, f"「{payload['name']}」单卡流向 · 明细", fonts['title'], width - 2 * MARGIN):
        text(MARGIN, y, line, 'title')
        y += 52
    text(MARGIN, y, f"主卡 {payload['main_used']} 人 · 备牌换入 {payload['side_used']} 人 · "
                    f"合计 {payload['total_used']} 人", 'sub')
    y += 44
    text(MARGIN, y, f"≤2 人的卡组：全部 {len(groups)} 个，逐组列出（张数×人数 / 该组人数）", 'section', MUTED)
    y += 44
    text(MARGIN, y, '卡组', 'cell', MUTED)
    for index, label in enumerate(('主卡', '换入', '合计')):
        text(MARGIN + name_w + index * cell_w, y, label, 'cell', MUTED)
    y = header
    for names, cells, line_count in rows:
        draw.line((MARGIN, y - 8, width - MARGIN, y - 8), fill=RULE, width=2)
        for index, line in enumerate(names):
            text(MARGIN, y + index * 36, line, 'name')
        for index, cell in enumerate(cells):
            for row, line in enumerate(cell):
                text(MARGIN + name_w + index * cell_w, y + row * 36, line, 'cell')
        y += line_count * 36 + 18
    text(MARGIN, height - footer + 24, '主卡=确证首局；换入=第2/3局相对首局净增；未换入≠没带', 'foot', MUTED)
    text(MARGIN, height - footer + 58, '合计=主卡+换入（下界）；括号里的分母=该卡组人数', 'foot', MUTED)
    return image


def render_flow(database, data_dir, now, card_name):
    """最多两张图：≥3 人卡组的条形图 + ≤2 人卡组的全量明细表。"""
    import io

    payload = collect_flow(database, data_dir, now, card_name)
    if not payload['shown']:
        raise ValueError('暂无带卡数据')
    fonts = _fonts()
    chart = [item for item in payload['shown']
             if item['total']['known'] >= BAR_MIN_PLAYERS]
    chart.sort(key=lambda item: (-item['total']['rate'], -item['total']['used'], item['deck']))
    detail = payload['small_groups']
    pages = []
    if chart:
        pages.append(_chart_image(payload, chart, fonts))
    if detail:
        pages.append(_detail_image(payload, detail, fonts))
    blobs = []
    for page in pages:
        output = io.BytesIO()
        page.save(output, format='PNG')
        blobs.append(output.getvalue())
    return blobs, payload
