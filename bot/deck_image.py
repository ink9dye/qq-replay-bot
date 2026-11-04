# -*- coding: utf-8 -*-
"""构筑图：把主卡组/额外/Side 拼成一张 PNG（本地卡图，不联网）。

只做渲染，不查库、不发消息；卡图由调用方注入 picture(code)->Path|None。
"""
from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from .deck_sort import sorted_cards

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


def _sections(cards, card_data=None):
    result = {'main': [], 'extra': [], 'side': []}
    for card in cards:
        result.setdefault(card.get('partition') or 'main', []).append(card)
    for items in result.values():
        items[:] = sorted_cards(items, card_data)
    return result


def _grid_rows(count):
    return max(1, math.ceil(count / COLS))


def render(result, picture, title=None, stats=None, card_data=None):
    """result 用 statistics_build_detail.query 的返回；picture(code) 返回本地卡图路径或 None。

    直接记录的 Side 之外，把 inferred_side 的推测候选单独画成“Side（推测）”，不混入直接记录。
    """
    cards = list(result.get('cards') or [])
    missing = 0
    sections = _sections(cards, card_data)
    inferred = list((result.get('inferred_side') or {}).get('cards') or [])
    if inferred:
        sections['inferred'] = sorted_cards(inferred, card_data)
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


def render_cached(result, picture, cache_dir, stats=None, card_data=None):
    """跨QQ用户复用同一构筑图片；只落盘完整图片，最多32份/64MiB。"""
    import hashlib, json, os, tempfile
    folder=Path(cache_dir)
    payload={key:result.get(key) for key in ('person','cards','inferred_side')}
    paths={}
    stamps=[]
    for card in list(result.get('cards') or [])+list((result.get('inferred_side') or {}).get('cards') or []):
        code=int(card.get('code') or 0)
        if code in paths:continue
        try:
            path=picture(code)
            file=Path(path) if path else None
            stat=file.stat() if file else None
        except (OSError,ValueError,TypeError):
            file,stat=None,None
        paths[code]=file
        stamps.append((code,str(file),stat.st_mtime_ns,stat.st_size) if stat else (code,None))
    key=hashlib.sha256(json.dumps([1,payload,card_data,sorted(stamps)],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    target=folder/(key+'.jpg')
    try:
        if 0<target.stat().st_size<=4*1024*1024:
            blob=target.read_bytes()
            if blob.startswith(b'\xff\xd8'):
                if stats is not None:stats['missing']=0
                return blob
    except OSError:pass
    current={}
    blob=render(result,paths.get,stats=current,card_data=card_data)
    if stats is not None:stats.update(current)
    if current.get('missing') or len(blob)>4*1024*1024:return blob
    temporary=None
    try:
        folder.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder,suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name);stream.write(blob)
        os.replace(temporary,target);temporary=None
        entries=[]
        for file in folder.glob('*.jpg'):
            if len(file.stem)==64 and all(c in '0123456789abcdef' for c in file.stem):
                info=file.stat();entries.append((info.st_mtime_ns,file,info.st_size))
        total=sum(item[2] for item in entries)
        entries.sort()
        while entries and (len(entries)>32 or total>64*1024*1024):
            _,file,size=entries.pop(0);file.unlink();total-=size
    except OSError:pass
    finally:
        if temporary is not None:
            try:temporary.unlink()
            except OSError:pass
    return blob
