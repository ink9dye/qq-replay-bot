# -*- coding: utf-8 -*-
"""前100卡组分布饼图（QQ 图片）：内环主大类、中环交集细分、外环具体卡组。

三层各自合计 100%：内环每个卡组只归一个主大类，中环是内环的“独占/交集”细分，
外环是精确卡组名。颜色与网页共用 deck_pie 的色板。
"""
from __future__ import annotations

import io
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont


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
    pie = data.get('deck_pie')
    if not pie or not pie.get('total'):
        from .deck_pie import from_data_dir
        pie = from_data_dir([dict(name=item['name'], players=item['count']) for item in items],
                            Path(database).parent)
    return dict(items=items, pie=pie, covered=int(data.get('covered_players') or 0),
                current=int(data.get('current_players') or 0),
                unidentified=int(data.get('unidentified_players') or 0))


def _font(size):
    candidates = (
        Path('C:/Windows/Fonts/msyh.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
    )
    path = next((item for item in candidates if item.is_file()), None)
    if path is not None:
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _rgb(color):
    try:
        return ImageColor.getrgb(str(color))
    except ValueError:
        return (170, 181, 196)


def _center_text(draw, x, y, text, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (box[2] - box[0]) / 2, y - (box[3] - box[1]) / 2), text, font=font, fill=fill)


def _draw_ring(draw, center, out_r, in_r, items, font, label_radius, label_kind):
    total = sum(item['players'] for item in items)
    if not total:
        return
    angle = -90.0
    for index, item in enumerate(items):
        fraction = item['players'] / total
        end = angle + fraction * 360
        box = (center[0] - out_r, center[1] - out_r, center[0] + out_r, center[1] + out_r)
        color = _rgb(item.get('mixed') or item.get('color'))
        if fraction >= 0.999:
            draw.ellipse(box, fill=color)
        else:
            draw.pieslice(box, angle, end, fill=color)
            draw.arc(box, angle, end, fill='white', width=2)
        mid = math.radians((angle + end) / 2)
        if fraction >= (0.08 if label_kind != 'outer' else 0.035):
            x = center[0] + math.cos(mid) * label_radius
            y = center[1] + math.sin(mid) * label_radius
            if label_kind == 'inner':
                label = str(item['name'])
            elif label_kind == 'middle':
                label = str(item['label'])
            else:
                label = str(index + 1)
            light = (0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]) / 255
            _center_text(draw, x, y, label, font, '#173529' if light > 0.62 else 'white')
        angle = end


def _swatch(draw, x, y, color, size=12):
    draw.rectangle((x, y, x + size, y + size), fill=_rgb(color))


def render(database, replay_root, now):
    payload = collect(database, replay_root, now)
    pie = payload['pie']
    if not pie.get('total'):
        raise ValueError('暂无卡组档案')

    width, height = 1320, 880
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    title_font, head_font, text_font, small_font = _font(24), _font(16), _font(15), _font(13)
    draw.text((30, 22), f"前100卡组分布 · 有档案 {payload['covered']}/{payload['current']} 人 · "
                        f"内环主大类 / 中环交集 / 外环名字", font=title_font, fill='#173529')
    center = (430, 450)
    _draw_ring(draw, center, 262, 236, pie['outer'], small_font, 249, 'outer')
    draw.ellipse((center[0] - 236, center[1] - 236, center[0] + 236, center[1] + 236), fill='white')
    _draw_ring(draw, center, 230, 178, pie['middle'], small_font, 204, 'middle')
    draw.ellipse((center[0] - 178, center[1] - 178, center[0] + 178, center[1] + 178), fill='white')
    _draw_ring(draw, center, 172, 120, pie['inner'], small_font, 146, 'inner')
    draw.ellipse((center[0] - 120, center[1] - 120, center[0] + 120, center[1] + 120), fill='white')
    _center_text(draw, center[0], center[1] - 18, str(pie['total']), _font(40), '#173529')
    _center_text(draw, center[0], center[1] + 16, '总人数', text_font, '#65776b')
    _center_text(draw, center[0], center[1] + 38, '内环 100% · 外环 100%', small_font, '#65776b')

    x, y = 850, 120
    rows = []
    rows.append(('head', '内环 · 主大类（合计 100%）'))
    for item in pie['inner']:
        rows.append(('inner', item))
    rows.append(('head', '中环 · 交集细分'))
    for item in pie['middle']:
        rows.append(('middle', item))
    rows.append(('head', '外环 · 具体卡组'))
    for index, item in enumerate(pie['outer']):
        rows.append(('outer', item))
    index = 0
    outer_number = 0
    for kind, payload_item in rows:
        if y > height - 26:
            draw.text((x, y), '…还有更多，见网页', font=small_font, fill='#7a887c')
            break
        if kind == 'head':
            if index:
                y += 8
            draw.text((x, y), payload_item, font=head_font, fill='#48613e')
            y += 26
        elif kind == 'inner':
            _swatch(draw, x, y + 2, payload_item['color'])
            draw.text((x + 20, y - 1), f"{payload_item['name']}  {payload_item['players']}人 · {payload_item['percent']}%",
                      font=text_font, fill='#22322B')
            y += 24
        elif kind == 'middle':
            _swatch(draw, x, y + 2, payload_item.get('mixed') or payload_item['color'])
            label = f"{payload_item['label']} @ {payload_item['primary']}  {payload_item['players']}人 · {payload_item['percent']}%"
            draw.text((x + 20, y - 1), label, font=small_font, fill='#22322B')
            y += 22
        else:
            outer_number += 1
            _swatch(draw, x, y + 2, payload_item['color'])
            label = f"{outer_number}. {payload_item['name']}  {payload_item['players']}人 · {payload_item['percent']}%"
            if len(label) > 34:
                label = label[:33] + '…'
            draw.text((x + 20, y - 1), label, font=small_font, fill='#22322B')
            y += 22
        index += 1
    draw.text((30, height - 34), pie.get('note') or '',
              font=small_font, fill='#65776b')
    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue(), payload
