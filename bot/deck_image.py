# -*- coding: utf-8 -*-
"""构筑图：把主卡组/额外/Side 拼成一张 PNG（本地卡图，不联网）。

只做渲染，不查库、不发消息；卡图由调用方注入 picture(code)->Path|None。
"""
from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CARD_W, CARD_H = 76, 109
GAP = 5
COLS = 10
HEADER_H = 64
SECTION_TITLE_H = 26
MARGIN = 12


def _font(size):
    candidates = [
        Path('C:/Windows/Fonts') / 'msyh.ttc',
        Path('C:/Windows/Fonts') / 'msyhbd.ttc',
        Path('C:/Windows/Fonts') / 'simhei.ttf',
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _sections(cards):
    result = {'main': [], 'extra': [], 'side': []}
    for card in cards:
        result.setdefault(card.get('partition') or 'main', []).append(card)
    for items in result.values():
        items.sort(key=lambda c: (str(c.get('name') or ''), int(c.get('code') or 0)))
    return result


def _grid_rows(count):
    return max(1, math.ceil(count / COLS))


def render(result, picture, title=None, stats=None):
    """result 用 statistics_build_detail.query 的返回；picture(code) 返回本地卡图路径或 None。

    直接记录的 Side 之外，把 inferred_side 的推测候选单独画成“Side（推测）”，不混入直接记录。
    """
    cards = list(result.get('cards') or [])
    missing = 0
    sections = _sections(cards)
    inferred = list((result.get('inferred_side') or {}).get('cards') or [])
    if inferred:
        sections['inferred'] = sorted(inferred, key=lambda c: (str(c.get('name') or ''), int(c.get('code') or 0)))
    person = result.get('person') or {}
    player = person.get('player') or '玩家'
    deck = person.get('deck_type') or '卡组未识别'
    counts = {name: sum(int(c.get('copies') or 1) for c in items) for name, items in sections.items()}
    present = [(name, label, sections.get(name) or [])
               for name, label in (('main', '主卡组'), ('extra', '额外'), ('side', 'Side'),
                                   ('inferred', 'Side（推测，非直接记录）'))
               if sections.get(name)]
    rows = sum(_grid_rows(len(items)) for _, _, items in present)
    width = MARGIN * 2 + COLS * CARD_W + (COLS - 1) * GAP
    height = HEADER_H + len(present) * SECTION_TITLE_H + rows * (CARD_H + GAP) + MARGIN
    image = Image.new('RGB', (width, height), '#f5f7fb')
    draw = ImageDraw.Draw(image)
    title_font, body_font, badge_font = _font(22), _font(15), _font(13)
    draw.text((MARGIN, 10), (title or f'{player} · {deck}')[:70], fill='#173529', font=title_font)
    summary = f'主 {counts["main"]} / 额外 {counts["extra"]} / Side {counts["side"]}'
    if inferred:
        summary += f' / 推测 Side {counts["inferred"]}'
    draw.text((MARGIN, 38), summary, fill='#5d7163', font=body_font)
    y = HEADER_H
    for name, label, items in present:
        draw.text((MARGIN, y), f'{label}（{counts[name]} 张）', fill='#2f6b4f', font=body_font)
        y += SECTION_TITLE_H
        for index, card in enumerate(items):
            row, col = divmod(index, COLS)
            x = MARGIN + col * (CARD_W + GAP)
            top = y + row * (CARD_H + GAP)
            path = None
            try:
                path = picture(int(card.get('code') or 0))
            except (OSError, ValueError, TypeError):
                path = None
            thumb = None
            if path:
                try:
                    with Image.open(path) as source:
                        thumb = source.convert('RGB').resize((CARD_W, CARD_H), Image.LANCZOS)
                except (OSError, ValueError):
                    thumb = None
            if thumb is not None:
                image.paste(thumb, (x, top))
            else:
                missing += 1
                draw.rectangle((x, top, x + CARD_W, top + CARD_H), fill='#e3e9f2', outline='#c3cfdf')
                code = str(card.get('code') or '?')
                draw.text((x + 5, top + CARD_H // 2 - 7), code[-6:], fill='#6b7d93', font=badge_font)
            if name == 'inferred':
                draw.rectangle((x, top, x + CARD_W - 1, top + CARD_H - 1), outline='#d4a44b', width=2)
            copies = int(card.get('copies') or 1)
            if copies > 1:
                badge = f'×{copies}'
                box = draw.textbbox((0, 0), badge, font=badge_font)
                bw, bh = box[2] - box[0] + 8, box[3] - box[1] + 4
                draw.rectangle((x + CARD_W - bw, top, x + CARD_W, top + bh), fill='#1e3858')
                draw.text((x + CARD_W - bw + 4, top + 1), badge, fill='white', font=badge_font)
        y += _grid_rows(len(items)) * (CARD_H + GAP)
    if stats is not None:
        stats['missing'] = missing
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=88, optimize=True, progressive=True)
    return output.getvalue()
