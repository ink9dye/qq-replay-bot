# -*- coding: utf-8 -*-
"""前100卡组分布饼图（QQ 图片）：内环主大类、外环具体卡组。

与网页同一口径：内环每个卡组只归一个主大类（按混用该字段的卡组数排序），
外环是该主大类下的精确卡组名；外圈直接标名称和百分比，点击筛选只存在于网页。
"""
from __future__ import annotations

import io
import hashlib
import itertools
import json
import os
import threading
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
    pie = data.get('deck_pie')
    if not pie or not pie.get('total'):
        from .deck_pie import from_data_dir
        pie = from_data_dir([dict(name=item['name'], players=item['count']) for item in items],
                            Path(database).parent)
    return dict(items=items, pie=pie, single=data.get('deck_single'),
                covered=int(data.get('covered_players') or 0),
                current=int(data.get('current_players') or 0),
                unidentified=int(data.get('unidentified_players') or 0))


def _font(size, bold=False):
    candidates = (
        Path('C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc' if bold
             else '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
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


def _card_file(code):
    if not code:
        return None
    roots = (Path(__file__).resolve().parents[1] / 'ygopro' / 'pics',
             Path('/opt/ygobot/ygopro/pics'))
    cache = Path(__file__).resolve().parents[1] / 'data' / 'cache' / 'card-art'
    paths = [root / (str(code) + '.jpg') for root in roots]
    paths.append(cache / (str(code) + '.jpg'))
    path = next((item for item in paths if item.is_file()), None)
    if path is None:
        from .statistics_local_cards import picture
        found = picture(Path(__file__).resolve().parents[1] / 'data', code)
        return found[0] if found else None
    return path


def _card_work(code):
    path = _card_file(code)
    if path is None:
        return None
    try:
        image = Image.open(path).convert('RGB')
    except Exception:
        return None
    width, height = image.size
    return image.crop((int(width * 0.14), int(height * 0.165),
                       int(width * (0.14 + 0.74)), int(height * (0.165 + 0.485))))


def _card_art(code, size):
    image = _card_work(code)
    if image is None:
        return None
    try:
        scale = max(size / image.width, size / image.height)
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                             Image.Resampling.LANCZOS)
        left = (image.width - size) // 2
        top = (image.height - size) // 2
        return image.crop((left, top, left + size, top + size))
    except Exception:
        return None


def _card_art_rect(code, width, height):
    image = _card_work(code)
    if image is None:
        return None
    try:
        return image.resize((max(1, int(width)), max(1, int(height))), Image.Resampling.LANCZOS)
    except Exception:
        return None


def _paste_wedge(image, center, out_r, in_r, start, end, code, color, thumb=False):
    size = int(out_r * 2) + 2
    layer = Image.new('RGB', (size, size), _rgb(color))
    mask = Image.new('L', (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.pieslice((0, 0, size - 1, size - 1), start, end, fill=255)
    if in_r > 0:
        inner = int(size / 2 - in_r)
        mask_draw.ellipse((inner, inner, size - inner, size - inner), fill=0)
    if thumb and code:
        span = math.radians(abs(end - start))
        mid = math.radians((start + end) / 2)
        r_mid = (out_r + in_r) / 2
        side = max(out_r * span, out_r - in_r) * 1.02
        side = min(side, out_r * 2 * 0.60)
        width = side * 1.526
        height = side
        card = _card_art_rect(code, width, height)
        if card is not None:
            px = int(size / 2 + math.cos(mid) * r_mid - card.width / 2)
            py = int(size / 2 + math.sin(mid) * r_mid - card.height / 2)
            layer.paste(card, (px, py))
    else:
        card = _card_art(code, size)
        if card is not None:
            layer.paste(card, (0, 0))
    image.paste(layer, (int(center[0] - out_r), int(center[1] - out_r)), mask)


def _draw_labels(draw, center, radius, labels, font, canvas_width, canvas_height):
    """外圈标签：左右两列排开，折线从扇区边缘接到列上。"""
    groups = {'left': [], 'right': []}
    for angle, text, _color in labels:
        groups['right' if math.cos(angle) >= 0 else 'left'].append((angle, text))
    for side, rows in groups.items():
        rows.sort(key=lambda item: math.sin(item[0]))
        if not rows:
            continue
        spacing = min(22, max(15, (canvas_height - 180) / len(rows)))
        top = center[1] - (len(rows) - 1) * spacing / 2
        top = max(100, min(top, canvas_height - 40 - (len(rows) - 1) * spacing))
        text_x = canvas_width - 300 if side == 'right' else 300
        bend_x = center[0] + radius + 24 if side == 'right' else center[0] - radius - 24
        for index, (angle, text) in enumerate(rows):
            y = top + index * spacing
            edge_x = center[0] + math.cos(angle) * (radius + 2)
            edge_y = center[1] + math.sin(angle) * (radius + 2)
            end_x = text_x - 4 if side == 'right' else text_x + 4
            draw.line([(edge_x, edge_y), (bend_x, y), (end_x, y)], fill='#b8c6b6', width=1)
            box = draw.textbbox((0, 0), text, font=font)
            width = box[2] - box[0]
            tx = text_x if side == 'right' else text_x - width
            draw.text((tx, y - (box[3] - box[1]) / 2 - box[1]), text, font=font, fill='#2f4138')


def _swatch(draw, x, y, color, size=12):
    draw.rectangle((x, y, x + size, y + size), fill=_rgb(color))


def _draw_top_legend(draw, inner, width, font):
    x, y = 40, 92
    for cat in inner:
        text = f"{cat['name']}  {cat['players']}人 · {cat['percent']}%"
        box = draw.textbbox((0, 0), text, font=font)
        text_width = box[2] - box[0]
        if x + text_width + 60 > width - 40:
            x, y = 40, y + 28
        draw.rectangle((x, y + 3, x + 12, y + 15), fill=_rgb(cat['color']))
        draw.text((x + 20, y), text, font=font, fill='#22322B')
        x += text_width + 50


def _pct_text(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = ('%.1f' % number)
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _render_single_payload(payload):
    """与网页 renderSinglePie 逐项对齐：单环卡图 + 交集阶梯边界 + 图例（顺序/人头/分栏一致）。"""
    import base64
    import subprocess

    single = payload.get('single') or {}
    total = int(single.get('total') or 0)
    if not total:
        raise ValueError('暂无卡组档案')

    items = []
    for s in single.get('slices') or []:
        items.append(dict(name=s['name'], players=s['players'], percent=s['percent'],
                          color=s.get('color') or '#7A8B99', image=s.get('image'),
                          kind=s.get('kind') or 'deck', bands=s.get('bands') or [],
                          decks=s.get('decks') or [], parts=[]))
    other = single.get('other') or {}
    if int(other.get('players') or 0) > 0:
        items.append(dict(name=other.get('name') or '其他', players=other['players'],
                          percent=other['percent'], color=other.get('color') or '#7A8B99',
                          image=other.get('image'), kind='other', bands=[], decks=[],
                          parts=other.get('parts') or []))
    # 与网页一致：按人数（百分比）降序、同人数按名称；「其他」固定最后。
    items.sort(key=lambda item: (item['kind'] == 'other', -float(item['players']), item['name']))

    W, H = 980, 780
    cx, cy, R = 490, 390, 300
    r0, r1, b0 = R * 0.30, R * 0.94, R * 0.56
    unit = 360.0 / total
    cursor = -90.0
    for item in items:
        item['a0'] = cursor
        item['a1'] = cursor + item['players'] * unit
        cursor = item['a1']

    def find_overlap(left, right):
        # 与网页一致：先看左片 partner=右片，再看右片 partner=左片（后者覆盖前者）。
        found = None
        for band in left['bands']:
            if band.get('kind') == 'overlap' and band.get('partner') == right['name'] and band.get('players'):
                found = band
        for band in right['bands']:
            if band.get('kind') == 'overlap' and band.get('partner') == left['name'] and band.get('players'):
                found = band
        return found

    steps = {}
    middle_rows = (payload.get('pie') or {}).get('middle') or []
    for index in range(len(items) - 1):
        left, right = items[index], items[index + 1]
        band = find_overlap(left, right)
        players = band.get('players') if band else None
        from_middle = False
        if not players and left['name'] != '其他' and right['name'] != '其他':
            # 与网页一致：只认“primary + 恰好一个 extras”的相邻交集，「其他」不参与。
            for row in middle_rows:
                extras = row.get('extras') or []
                if len(extras) != 1:
                    continue
                if row.get('primary') == left['name'] and extras[0] == right['name']:
                    players = row.get('players'); from_middle = True; break
                if row.get('primary') == right['name'] and extras[0] == left['name']:
                    players = row.get('players'); from_middle = True; break
        if not players:
            continue
        width = float(players) * unit
        steps[index] = (left['a1'] - width / 2, left['a1'] + width / 2)
        if from_middle and not any(b.get('kind') == 'overlap' and b.get('partner') == right['name']
                                   for b in left['bands']):
            # 网页只把补出来的交集记到左侧切片（图例只出现在左块）。
            left['bands'].append(dict(kind='overlap', name=f"{left['name']}∩{right['name']}",
                                      players=players, partner=right['name'], side='left'))

    def pt(r, deg):
        t = math.radians(deg)
        return [cx + r * math.cos(t), cy + r * math.sin(t)]

    def fmt(q):
        return f"{q[0]:.1f} {q[1]:.1f}"

    def arc(r, a0, a1, sweep):
        large = 1 if abs(a1 - a0) > 180 else 0
        return f"A{r:.1f} {r:.1f} 0 {large} {1 if sweep else 0} {fmt(pt(r, a1))}"

    def slice_path(index):
        s = items[index]
        left_step = steps.get(index - 1)
        right_step = steps.get(index)
        # 与网页一致：交集边界是“阶梯”——外段/内段角度错开半个交集宽度，在 b0 处横拐。
        oa0 = left_step[1] if left_step else s['a0']
        oa1 = right_step[1] if right_step else s['a1']
        ia1 = right_step[0] if right_step else s['a1']
        ia0 = left_step[0] if left_step else s['a0']
        d = "M" + fmt(pt(r1, oa0)) + arc(r1, oa0, oa1, True)
        if right_step:
            d += " L" + fmt(pt(b0, oa1)) + arc(b0, oa1, ia1, False)
        d += " L" + fmt(pt(r0, ia1)) + arc(r0, ia1, ia0, False)
        if left_step:
            d += " L" + fmt(pt(b0, ia0)) + arc(b0, ia0, oa0, True)
        return d + " Z"

    def band_path(a0, a1, inner, outer):
        return ("M" + fmt(pt(outer, a0)) + arc(outer, a0, a1, True)
                + " L" + fmt(pt(inner, a1)) + arc(inner, a1, a0, False) + " Z")

    def sample_bbox(a0, a1, radii):
        points = []
        for step in range(17):
            angle = a0 + (a1 - a0) * step / 16
            for radius in radii:
                points.append(pt(radius, angle))
        xs = [q[0] for q in points]
        ys = [q[1] for q in points]
        return min(xs), min(ys), max(xs), max(ys)

    _ART_CACHE = {}

    def _art_info(code):
        if code in _ART_CACHE:
            return _ART_CACHE[code]
        path = _card_file(code)
        if path is None:
            _ART_CACHE[code] = None
            return None
        try:
            image = Image.open(path).convert('RGB')
        except Exception:
            _ART_CACHE[code] = None
            return None
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=88)
        info = dict(data='data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii'),
                    width=image.width, height=image.height,
                    ax=image.width * 63 / 400.0, ay=image.height * 103 / 580.0,
                    aw=image.width * 282 / 400.0, ah=image.height * 273 / 580.0)
        _ART_CACHE[code] = info
        return info

    def art_markup(code, bbox, clip_id, opacity=0.22):
        info = _art_info(code) if code else None
        if info is None:
            return ''
        x0, y0, x1, y1 = bbox
        bw = max(1.0, x1 - x0); bh = max(1.0, y1 - y0)
        scale = max(bw / info['aw'], bh / info['ah'])
        image_x = x0 - info['ax'] * scale
        image_y = y0 - info['ay'] * scale
        art_id = f"a{clip_id}"
        clip = (f'<clipPath id="{art_id}"><rect x="{x0:.1f}" y="{y0:.1f}" '
                f'width="{info["aw"] * scale:.1f}" height="{info["ah"] * scale:.1f}"/></clipPath>')
        markup = (clip + f'<g clip-path="url(#{clip_id})"><g clip-path="url(#{art_id})">'
                  f'<image href="{info["data"]}" x="{image_x:.1f}" y="{image_y:.1f}" '
                  f'width="{info["width"] * scale:.1f}" height="{info["height"] * scale:.1f}" '
                  f'preserveAspectRatio="none"/></g>'
                  f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                  f'fill="#0a1812" opacity="{opacity}"/></g>')
        return markup

    defs, shapes = [], []
    clip_index = 0
    for index, item in enumerate(items):
        path = slice_path(index)
        lo, hi = item['a0'], item['a1']
        if steps.get(index - 1):
            lo = min(lo, steps[index - 1][0])
        if steps.get(index):
            hi = max(hi, steps[index][1])
        bbox = sample_bbox(lo, hi, (r0, r1))
        width = bbox[2] - bbox[0]; height = bbox[3] - bbox[1]
        clip_id = f"c{clip_index}"; clip_index += 1
        defs.append(f'<clipPath id="{clip_id}"><path d="{path}"/></clipPath>')
        art_svg = art_markup(item.get('image'), bbox, clip_id)
        if art_svg:
            shapes.append(art_svg)
        else:
            shapes.append(f'<path d="{path}" fill="{item["color"]}"/>')
        shapes.append(f'<path d="{path}" fill="none" stroke="#ffffff" stroke-width="1.4"/>')

        # 嵌套小类：相邻同卡图合并后各自成带
        groups = []
        for band in item['bands']:
            if band.get('kind') != 'member' or not band.get('players'):
                continue
            if groups and groups[-1]['image'] == band.get('image'):
                groups[-1]['players'] += band['players']
            else:
                groups.append(dict(image=band.get('image'), players=band['players']))
        band_cursor = item['a0']
        span = item['a1'] - item['a0']
        for group in groups:
            # 与网页一致：小类卡图与母片相同就不单独成块，整块归母片（避免时黑这种伪小类）。
            if not group.get('image') or group.get('image') == item.get('image'):
                continue
            band_width = (group['players'] / max(1, item['players'])) * span
            b_path = band_path(band_cursor, band_cursor + band_width, b0, r1)
            b_bbox = sample_bbox(band_cursor, band_cursor + band_width, (b0, r1))
            b_clip = f"c{clip_index}"; clip_index += 1
            defs.append(f'<clipPath id="{b_clip}"><path d="{b_path}"/></clipPath>')
            b_art = art_markup(group['image'], b_bbox, b_clip, opacity=0.22)
            if b_art:
                shapes.append(b_art)
            shapes.append(f'<path d="{b_path}" fill="none" stroke="#ffffff" stroke-width="1.2"/>')
            band_cursor += band_width

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
           f'<rect width="{W}" height="{H}" fill="white"/><defs>{"".join(defs)}</defs>'
           f'{"".join(shapes)}'
           f'<circle cx="{cx}" cy="{cy}" r="{r0:.1f}" fill="white"/></svg>')

    scale = 1.12
    try:
        result = subprocess.run(["rsvg-convert", "-w", str(int(W * scale)), "-h", str(int(H * scale))],
                                input=svg.encode("utf-8"), capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "ignore")[:200])
        ring = Image.open(io.BytesIO(result.stdout)).convert("RGB")
    except Exception:
        ring = Image.new("RGB", (int(W * scale), int(H * scale)), "white")

    width, height = 1600, 2000
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, text_font, small_font, tiny_font = _font(28), _font(16), _font(14), _font(13)
    draw.text((40, 30), f"前100卡组分布 · 有档案 {payload['covered']}/{payload['current']} 人 · 单环",
              font=title_font, fill='#173529')
    ox = (width - ring.width) // 2
    oy = 80
    image.paste(ring, (ox, oy))
    center = (ox + cx * scale, oy + cy * scale)
    _center_text(draw, center[0], center[1] - 24, str(total), _font(42), '#173529')
    _center_text(draw, center[0], center[1] + 24, '总人数 · 单环 100%', _font(15), '#65776b')

    label_font = _font(max(12, int(round(14 * scale))), bold=True)
    for item in items:
        mid = math.radians((item['a0'] + item['a1']) / 2)
        right = math.cos(mid) >= 0
        # 与网页一致：圆点（±4,-5，r=3.2）贴在 r1+16，文字在点外侧 10，14px/600/#173529。
        rr = (r1 + 16) * scale
        dot_x = center[0] + math.cos(mid) * rr + (4 * scale if right else -4 * scale)
        dot_y = center[1] + math.sin(mid) * rr - 5 * scale
        radius = 3.2 * scale
        draw.ellipse((dot_x - radius, dot_y - radius, dot_x + radius, dot_y + radius),
                     fill=_rgb(item['color']))
        x = center[0] + math.cos(mid) * rr + (10 * scale if right else -10 * scale)
        y = center[1] + math.sin(mid) * rr
        draw.text((x, y), f"{item['name']}  {_pct_text(item['percent'])}%", font=label_font,
                  fill='#173529', anchor='lm' if right else 'rm')

    line_height = 21
    gap = 12
    columns = [(60, 500), (560, 500), (1060, 500)]
    top = 1070
    draw.text((60, 1010), '每个切片包含的卡组', font=_font(15), fill='#22322B')
    draw.text((60, 1034), '◆ = 外环嵌套 >4%；交集 = 两个大类共同占有', font=tiny_font, fill='#7a8b80')

    outer_rows = (payload.get('pie') or {}).get('outer') or []

    def _people(item):
        # 与网页一致：「其他」用切片人数（等于下面 parts 之和）；其余用“参与该字段的玩家数”。
        if item.get('kind') == 'other':
            return int(round(float(item['players'])))
        involved = sum(float(row.get('players') or 0) for row in middle_rows
                       if row.get('primary') == item['name']
                       or item['name'] in (row.get('extras') or []))
        return int(round(involved)) if involved else int(round(float(item['players'])))

    def _deck_line(row, name):
        mark = '◆' if float(row.get('players') or 0) / total * 100 > 4 else '·'
        primary = row.get('primary')
        suffix = f"  ∩{primary}" if primary and primary != name and row.get('name') != name else ''
        return f"{mark} {row['name']}  {row['players']}{suffix}"

    def _hits(name, kind):
        found = []
        for row in outer_rows:
            hit = row.get('name') == name if kind == 'deck' else row.get('primary') == name
            if not hit and name in (row.get('extras') or []):
                hit = True
            if hit:
                found.append(row)
        return found

    blocks = []
    for item in items:
        lines = [f"{item['name']} {_people(item)}人 · {_pct_text(item['percent'])}%"]
        if item.get('kind') == 'category':
            # 列出所有带该字段的卡组（含混合构筑）；跨字段的标 ∩主字段，行之和=表头人数。
            for row in _hits(item['name'], 'category'):
                lines.append(_deck_line(row, item['name']))
            for band in item['bands']:
                if band.get('kind') == 'overlap':
                    lines.append(f"· 交集 {band['name']}  {band['players']}人（内环/外环各一张卡图）")
        elif item.get('kind') == 'other':
            for part in item.get('parts') or []:
                lines.append(f"· {part['name']}  {part['players']}")
        elif item.get('kind') == 'deck':
            hits = _hits(item['name'], 'deck')
            if len(hits) > 1:
                for row in hits:
                    lines.append(_deck_line(row, item['name']))
        blocks.append(dict(lines=lines, color=item['color'],
                           height=len(lines) * line_height + gap))

    # 分栏与网页 CSS columns 的平衡效果对齐：按顺序切 3 栏，取“最高一栏最矮”的切法。
    count = len(blocks)
    cuts = set()
    if count > len(columns):
        best = None
        for candidate in itertools.combinations(range(1, count), len(columns) - 1):
            bounds = (0,) + candidate + (count,)
            heights = [sum(blocks[k]['height'] for k in range(bounds[i], bounds[i + 1]))
                       for i in range(len(columns))]
            score = max(heights)
            if best is None or score < best[0]:
                best = (score, candidate)
        if best:
            cuts = set(best[1])

    column_index = 0
    y = top
    bottom = top
    for index, block in enumerate(blocks):
        if index in cuts:
            column_index += 1
            y = top
        x = columns[min(column_index, len(columns) - 1)][0]
        draw.rectangle((x, y + 3, x + 10, y + 13), fill=_rgb(block['color']))
        draw.text((x + 16, y), block['lines'][0], font=small_font, fill='#22322B')
        y += line_height
        for line in block['lines'][1:]:
            draw.text((x + 16, y), line, font=tiny_font, fill='#4a5a52')
            y += line_height
        y += gap
        bottom = max(bottom, y)

    # 脚注紧跟图例内容，避免整图下半截留白（机器人发的图例图会被裁成大片空白）。
    draw.text((40, min(height - 40, bottom + 6)), single.get('note') or '', font=small_font, fill='#65776b')
    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue(), payload


def _render_payload(payload):
    pie = payload['pie']
    if not pie.get('total'):
        raise ValueError('暂无卡组档案')

    width, height = 1600, 1100
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    title_font, head_font, text_font, small_font = _font(28), _font(18), _font(16), _font(14)
    draw.text((40, 30), f"前100卡组分布 · 有档案 {payload['covered']}/{payload['current']} 人 · "
                        f"内环主大类 / 外环具体卡组", font=title_font, fill='#173529')
    center = (800, 560)
    radius = 330
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
        start = angle
        merged = not cat.get('image')
        band = cat.get('band_image')
        cursor = angle
        for item in outer_by_primary.get(cat['name']) or []:
            o_span = item['players'] / max(1, cat['players']) * span
            o_end = cursor + o_span
            code = item.get('image') or middle_image.get(
                (cat['name'], tuple(sorted(item.get('extras') or [])))) or cat.get('image')
            if merged:
                _paste_wedge(image, center, radius * 0.94, radius * 0.30,
                             cursor, o_end, code, item['color'], thumb=True)
            elif not band:
                _paste_wedge(image, center, radius * 0.94, radius * 0.56,
                             cursor, o_end, code, item['color'], thumb=True)
            o_mid = math.radians((cursor + o_end) / 2)
            labels.append((o_mid, f"{item['name']}  {_pct_text(item['percent'])}%", _rgb(item['color'])))
            cursor = o_end
        angle = cursor
    hole = radius * 0.56
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
        if cat.get('image') and not cat.get('band_image'):
            _paste_wedge(image, center, radius * 0.56, radius * 0.30,
                         angle, end, cat.get('image'), cat['color'], thumb=True)
            draw.arc(box, angle, end, fill='white', width=2)
        angle = end
    # 没有主大类的卡组（其他/未识别）直接占据内外环整段
    start = -90.0
    for cat in inner:
        span = cat['players'] / total * 360.0
        if not cat.get('image'):
            cursor = start
            for item in outer_by_primary.get(cat['name']) or []:
                o_span = item['players'] / max(1, cat['players']) * span
                o_end = cursor + o_span
                code = item.get('image') or middle_image.get(
                    (cat['name'], tuple(sorted(item.get('extras') or [])))) or cat.get('image')
                _paste_wedge(image, center, radius * 0.94, radius * 0.30,
                             cursor, o_end, code, item['color'], thumb=True)
                cursor = o_end
        start += span
    # 单一大类：一张图铺满内外环，避免中间断层
    start = -90.0
    for cat in inner:
        span = cat['players'] / total * 360.0
        if cat.get('band_image'):
            _paste_wedge(image, center, radius * 0.94, radius * 0.30,
                         start, start + span, cat['band_image'], cat['color'], thumb=True)
        start += span
    # 扇区边界线
    start = -90.0
    box = (center[0] - radius * 0.94, center[1] - radius * 0.94,
           center[0] + radius * 0.94, center[1] + radius * 0.94)
    for cat in inner:
        span = cat['players'] / total * 360.0
        r0 = radius * (0.30 if (cat.get('band_image') or not cat.get('image')) else 0.56)
        cursor = start
        for item in outer_by_primary.get(cat['name']) or []:
            o_span = item['players'] / max(1, cat['players']) * span
            a = math.radians(cursor)
            draw.line((center[0] + math.cos(a) * r0, center[1] + math.sin(a) * r0,
                       center[0] + math.cos(a) * radius * 0.94,
                       center[1] + math.sin(a) * radius * 0.94), fill='white', width=1)
            draw.arc(box, cursor, cursor + o_span, fill='white', width=1)
            cursor += o_span
        start += span
    _draw_labels(draw, center, radius * 0.94, labels, _font(16), width, height)
    center_hole = radius * 0.30
    draw.ellipse((center[0] - center_hole, center[1] - center_hole,
                  center[0] + center_hole, center[1] + center_hole), fill='white')
    _center_text(draw, center[0], center[1] - 16, str(total), _font(46), '#173529')
    _center_text(draw, center[0], center[1] + 18, '总人数 · 内环 100%', text_font, '#65776b')

    _draw_top_legend(draw, inner, width, text_font)
    draw.text((40, height - 38), pie.get('note') or '', font=small_font, fill='#65776b')
    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue(), payload


_RENDER_LOCK = threading.Lock()


def _payload_cache_path(database):
    return Path(database).parent / 'cache' / 'top100-pie' / 'payload.json'


def _db_stamp(database):
    try:
        stat = Path(database).stat()
        return [stat.st_mtime_ns // 1_000_000_000, stat.st_size]
    except OSError:
        return None


def load_payload(database, now, max_age=120.0):
    """2 分钟内的同一库版本复用 collect() 结果（QQ 饼图每次重算名册约 8 秒）。"""
    path = _payload_cache_path(database)
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if record.get('stamp') != _db_stamp(database):
        return None
    if not 0 <= float(now) - float(record.get('saved') or 0) <= max_age:
        return None
    payload = record.get('payload')
    return payload if isinstance(payload, dict) and payload.get('pie') else None


def save_payload(database, now, payload):
    path = _payload_cache_path(database)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(saved=float(now), stamp=_db_stamp(database), payload=payload),
                                  ensure_ascii=False, default=str), encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        pass


def render(database, replay_root, now):
    """Return (parts, payload): pie/legend PNGs generated by the website module."""
    from .statistics_pie_image import render_parts
    return render_parts(database, replay_root, now)
