# -*- coding: utf-8 -*-
"""前100卡组分布饼图（QQ 图片）——仿网页：扇区外引导线标签，上下错开。"""
from __future__ import annotations

import colorsys
import io
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


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


def _web_color(index, name):
    if name == '未识别':
        return (170, 181, 196)
    hue = ((210 + index * 137.508) % 360) / 360.0
    sat = (64 + (index * 7) % 14) / 100.0
    light = (49 + (index * 11) % 15) / 100.0
    red, green, blue = colorsys.hls_to_rgb(hue, light, sat)
    return int(red * 255), int(green * 255), int(blue * 255)


def _place(labels, center_y, top, bottom, gap=18):
    """网页 place() 的 Python 版：同一侧标签按纵向错开。"""
    ordered = sorted(labels, key=lambda item: math.sin(item['mid']))
    previous = -10 ** 9
    for item in ordered:
        y = center_y + math.sin(item['mid']) * item['radius']
        y = max(top, y)
        if y - previous < gap:
            y = previous + gap
        item['y'] = y
        previous = y
    nxt = bottom
    for item in reversed(ordered):
        if item['y'] > nxt:
            item['y'] = nxt
        nxt = item['y'] - gap
    return ordered


def _draw_centered(draw, x, y, text, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (box[2] - box[0]) / 2, y - (box[3] - box[1]) / 2), text,
              font=font, fill=fill)


def render(database, replay_root, now):
    payload = collect(database, replay_root, now)
    items = payload['items']
    if not items:
        raise ValueError('暂无卡组档案')

    width, height = 1120, 780
    center_x, center_y, radius = 560, 390, 235
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    title_font, label_font, pct_font = _font(23), _font(14), _font(14)
    draw.text((28, 20), f"前100卡组分布 · 有档案 {payload['covered']}/{payload['current']} 人 · "
                        f"已识别 {len(items)} 类", font=title_font, fill='#173529')

    total = sum(item['count'] for item in items)
    colors = [_web_color(index, item['name']) for index, item in enumerate(items)]
    angle = -90.0
    labels = []
    for item, color in zip(items, colors):
        fraction = item['count'] / total
        end = angle + fraction * 360
        box = (center_x - radius, center_y - radius, center_x + radius, center_y + radius)
        if fraction >= 0.999:
            draw.ellipse(box, fill=color)
        else:
            draw.pieslice(box, angle, end, fill=color)
            # 细白边，避免相邻扇区粘在一起
            draw.arc(box, angle, end, fill='white', width=2)
        mid = math.radians((angle + end) / 2)
        if fraction >= 0.08:
            _draw_centered(draw, center_x + math.cos(mid) * radius * 0.62,
                           center_y + math.sin(mid) * radius * 0.62,
                           f'{fraction * 100:.0f}%' if fraction * 100 >= 10 else f'{fraction * 100:.1f}%',
                           pct_font, 'white')
        labels.append(dict(name=item['name'], count=item['count'], mid=mid,
                           right=math.cos(mid) >= 0, radius=radius + 58))
        angle = end

    left = _place([label for label in labels if not label['right']], center_y, 58, height - 28)
    right = _place([label for label in labels if label['right']], center_y, 58, height - 28)
    for item in left + right:
        sx = center_x + math.cos(item['mid']) * (radius + 4)
        sy = center_y + math.sin(item['mid']) * (radius + 4)
        ex = center_x + math.cos(item['mid']) * (radius + 32)
        ey = item['y']
        lx = ex + 10 if item['right'] else ex - 10
        draw.line([(sx, sy), (ex, ey), (lx, ey)], fill='#9AA79F', width=1)
        label = f"{item['name']} {item['count']}人"
        if len(label) > 28:
            label = label[:27] + '…'
        bbox = draw.textbbox((0, 0), label, font=label_font)
        text_width = bbox[2] - bbox[0]
        tx = lx if item['right'] else lx - text_width
        draw.text((tx, ey - (bbox[3] - bbox[1]) / 2 - 1), label, font=label_font, fill='#22322B')

    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue(), payload
