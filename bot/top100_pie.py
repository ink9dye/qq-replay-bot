# -*- coding: utf-8 -*-
"""前100卡组分布饼图（QQ 图片）：内环主大类、外环具体卡组。

与网页同一口径：内环每个卡组只归一个主大类（按混用该字段的卡组数排序），
外环是该主大类下的精确卡组名；外圈直接标名称和百分比，点击筛选只存在于网页。
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


def _card_thumb(code, size=(30, 42)):
    if not code:
        return None
    roots = (Path(__file__).resolve().parents[1] / 'ygopro' / 'pics',
             Path('/opt/ygobot/ygopro/pics'))
    for root in roots:
        path = root / (str(code) + '.jpg')
        if path.is_file():
            try:
                image = Image.open(path).convert('RGB')
                image.thumbnail(size)
                return image
            except Exception:
                return None
    return None


def _card_art(code, size):
    if not code:
        return None
    roots = (Path(__file__).resolve().parents[1] / 'ygopro' / 'pics',
             Path('/opt/ygobot/ygopro/pics'))
    cache = Path(__file__).resolve().parents[1] / 'data' / 'cache' / 'card-art'
    paths = [root / (str(code) + '.jpg') for root in roots]
    paths.append(cache / (str(code) + '.jpg'))
    path = next((item for item in paths if item.is_file()), None)
    if path is None:
        try:
            import urllib.request
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / (str(code) + '.jpg')
            request = urllib.request.Request(
                'https://cdn.233.momobako.com/ygopro/pics/%s.jpg' % code,
                headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(request, timeout=6) as response:
                target.write_bytes(response.read())
            path = target
        except Exception:
            return None
    try:
        image = Image.open(path).convert('RGB')
        width, height = image.size
        image = image.crop((int(width * 0.14), int(height * 0.165),
                            int(width * (0.14 + 0.74)), int(height * (0.165 + 0.485))))
        scale = max(size / image.width, size / image.height)
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                             Image.LANCZOS)
        left = (image.width - size) // 2
        top = (image.height - size) // 2
        return image.crop((left, top, left + size, top + size))
    except Exception:
        return None


def _paste_wedge(image, center, out_r, in_r, start, end, code, color):
    size = int(out_r * 2) + 2
    art = Image.new('RGB', (size, size), _rgb(color))
    card = _card_art(code, size)
    if card is not None:
        art.paste(card, (0, 0))
    mask = Image.new('L', (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.pieslice((0, 0, size - 1, size - 1), start, end, fill=255)
    if in_r > 0:
        inner = int(size / 2 - in_r)
        mask_draw.ellipse((inner, inner, size - inner, size - inner), fill=0)
    image.paste(art, (int(center[0] - out_r), int(center[1] - out_r)), mask)


def _draw_labels(draw, center, radius, labels, font):
    labels.sort(key=lambda item: item[0])
    last = -99.0
    for angle, text, color in labels:
        r = radius + 20
        if angle - last < 0.18:
            r += 16
        last = angle
        x = center[0] + math.cos(angle) * r
        y = center[1] + math.sin(angle) * r
        edge_x = center[0] + math.cos(angle) * (radius + 2)
        edge_y = center[1] + math.sin(angle) * (radius + 2)
        draw.line((edge_x, edge_y, x - math.cos(angle) * 4, y - math.sin(angle) * 4),
                  fill='#b8c6b6', width=1)
        box = draw.textbbox((0, 0), text, font=font)
        width = box[2] - box[0]
        if math.cos(angle) >= 0:
            draw.text((x, y - (box[3] - box[1]) / 2), text, font=font, fill='#2f4138')
        else:
            draw.text((x - width, y - (box[3] - box[1]) / 2), text, font=font, fill='#2f4138')


def _swatch(draw, x, y, color, size=12):
    draw.rectangle((x, y, x + size, y + size), fill=_rgb(color))


def render(database, replay_root, now):
    payload = collect(database, replay_root, now)
    pie = payload['pie']
    if not pie.get('total'):
        raise ValueError('暂无卡组档案')

    width, height = 1400, 900
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    title_font, head_font, text_font, small_font = _font(24), _font(16), _font(15), _font(13)
    draw.text((30, 22), f"前100卡组分布 · 有档案 {payload['covered']}/{payload['current']} 人 · "
                        f"内环主大类 / 外环具体卡组", font=title_font, fill='#173529')
    center = (430, 470)
    radius = 255
    inner = pie['inner']
    outer_by_primary = {}
    for item in pie.get('outer') or []:
        outer_by_primary.setdefault(item['primary'], []).append(item)

    total = pie['total']
    middle_image = {(m['primary'], tuple(sorted(m.get('extras') or []))): m.get('image')
                    for m in pie.get('middle') or []}
    angle = -90.0
    labels = []
    outer_by_primary = {}
    for item in pie.get('outer') or []:
        outer_by_primary.setdefault(item['primary'], []).append(item)
    # 先画外环（具体卡组），再挖掉内圈，避免覆盖内环。
    for cat in inner:
        span = cat['players'] / total * 360.0
        cursor = angle
        for item in outer_by_primary.get(cat['name']) or []:
            o_span = item['players'] / max(1, cat['players']) * span
            o_end = cursor + o_span
            box = (center[0] - radius * 0.94, center[1] - radius * 0.94,
                   center[0] + radius * 0.94, center[1] + radius * 0.94)
            image_code = middle_image.get((cat['name'], tuple(sorted(item.get('extras') or [])))) or cat.get('image')
            _paste_wedge(image, center, radius * 0.94, radius * 0.60, cursor, o_end, image_code, item['color'])
            draw.arc(box, cursor, o_end, fill='white', width=1)
            if o_span >= 7.5:
                o_mid = math.radians((cursor + o_end) / 2)
                labels.append((o_mid, f"{item['name']}  {item['percent']}%", _rgb(item['color'])))
            cursor = o_end
        angle = cursor
    hole = radius * 0.60
    draw.ellipse((center[0] - hole, center[1] - hole,
                  center[0] + hole, center[1] + hole), fill='white')
    # 内环主大类
    angle = -90.0
    for cat in inner:
        span = cat['players'] / total * 360.0
        end = angle + span
        mid = math.radians((angle + end) / 2)
        color = _rgb(cat['color'])
        box = (center[0] - radius * 0.56, center[1] - radius * 0.56,
               center[0] + radius * 0.56, center[1] + radius * 0.56)
        _paste_wedge(image, center, radius * 0.56, radius * 0.30, angle, end, cat.get('image'), cat['color'])
        draw.arc(box, angle, end, fill='white', width=2)
        angle = end
    _draw_labels(draw, center, radius * 0.94, labels, small_font)
    center_hole = radius * 0.30
    draw.ellipse((center[0] - center_hole, center[1] - center_hole,
                  center[0] + center_hole, center[1] + center_hole), fill='white')
    _center_text(draw, center[0], center[1] - 16, str(total), _font(40), '#173529')
    _center_text(draw, center[0], center[1] + 18, '总人数 · 内环 100%', text_font, '#65776b')

    x, y = 900, 110
    draw.text((x, y), '内环 · 主大类（合计 100%）', font=head_font, fill='#48613e')
    y += 28
    for item in inner:
        if y > height - 40:
            break
        _swatch(draw, x, y + 2, item['color'])
        draw.text((x + 20, y - 1), f"{item['name']}  {item['players']}人 · {item['percent']}%",
                  font=text_font, fill='#22322B')
        y += 24
    y += 10
    draw.text((x, y), '外环 · 具体卡组（外圈文字对应）', font=head_font, fill='#48613e')
    y += 26
    for index, item in enumerate(pie.get('outer') or [], 1):
        if y > height - 34:
            draw.text((x, y), '…还有更多，见网页', font=small_font, fill='#7a887c')
            break
        _swatch(draw, x, y + 2, item['color'])
        label = f"{index}. {item['name']}  {item['players']}人 · {item['percent']}%"
        if len(label) > 34:
            label = label[:33] + '…'
        draw.text((x + 20, y - 1), label, font=small_font, fill='#22322B')
        y += 22
    draw.text((30, height - 32), pie.get('note') or '', font=small_font, fill='#65776b')
    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue(), payload
