"""复用 watch 的录像解析，生成 OURYGO 官方 v1 格式卡组链接。"""
from __future__ import annotations

import base64
import json
import math
import threading
import time
import urllib.request
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

from .watch_core import decks_from_replay, guess_deck_title, with_light_dark_field, _names_from_cards_payload

_point_cache = {}
_point_lock = threading.Lock()


def points_from_payload(payload):
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict) or isinstance(payload.get('pt'), bool):
        return None
    try:
        score = float(payload['pt'])
        return score if math.isfinite(score) and 0 <= score <= 1_000_000_000 else None
    except (KeyError, ValueError, TypeError):
        return None


def arena_points(player: str):
    now = time.monotonic()
    with _point_lock:
        cached = _point_cache.get(player)
        if cached and now < cached[0]:
            return cached[1]
        # 防止同一玩家的页面同时打开时发出重复请求。
        _point_cache[player] = (now + 3, None)
    result = None
    try:
        url = 'https://sapi.moecube.com:444/ygopro/arena/user?username=' + quote(player, safe='')
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(request, timeout=2) as response:
            result = points_from_payload(json.loads(response.read(65536)))
    except (OSError, ValueError, TypeError):
        pass
    with _point_lock:
        _point_cache[player] = (time.monotonic() + (300 if result is not None else 30), result)
    return result


@lru_cache(maxsize=2)
def _local_card_names(path: str, modified: int, size: int):
    return _names_from_cards_payload(json.loads(Path(path).read_text(encoding='utf-8')))


def observed_title(main: list[int], extra: list[int], cache_root: Path) -> str:
    try:
        path = cache_root / 'cards.json'
        info = path.stat()
        names = _local_card_names(str(path), info.st_mtime_ns, info.st_size)
        cards = main + extra
        return with_light_dark_field(guess_deck_title([names[code] for code in cards if code in names]), cards)
    except (OSError, ValueError, TypeError):
        return ''


def relative_age(saved_at: float, now: float) -> str:
    seconds = max(0, int(now - saved_at))
    if seconds < 60:
        return '刚刚'
    if seconds < 3600:
        return f'{seconds // 60}分钟前'
    if seconds < 86400:
        return f'{seconds // 3600}小时前'
    return f'{seconds // 86400}天前'


def card_items(cards: list[int]) -> list[tuple[int, int]]:
    result = []
    for code, count in Counter(cards).items():
        if not isinstance(code, int) or not 0 < code <= 0xffffffff:
            raise ValueError('无效的卡片密码')
        while count:
            amount = min(3, count)
            result.append((code, amount))
            count -= amount
    return result


def ourygo_url(name: str, main: list[int], extra: list[int], side: list[int]) -> str:
    sections = [card_items(cards) for cards in (main, extra, side)]
    query = {'name': name, 'ygotype': 'deck'}
    if (len(sections[0]) <= 255 and len(sections[1]) <= 15 and len(sections[2]) <= 15
            and all(code < (1 << 27) for section in sections for code, count in section)):
        # 站点 ygoUtil.js：8/4/4 位条目数；每项为2位张数 + 27位卡片密码，右补0到整字节。
        bits = f'{len(sections[0]):08b}{len(sections[1]):04b}{len(sections[2]):04b}'
        bits += ''.join(f'{count:02b}{code:027b}' for section in sections for code, count in section)
        bits += '0' * (-len(bits) % 8)
        data = bytes(int(bits[index:index + 8], 2) for index in range(0, len(bits), 8))
        query.update(v='1', d=base64.urlsafe_b64encode(data).decode('ascii').rstrip('='))
    else:
        # 官方 v0 支持十进制密码，避免非标准卡片密码超出27位时被截断。
        query['v'] = '0'
        for key, items in zip(('main', 'extra', 'side'), sections):
            query[key] = '_'.join(str(code) if count == 1 else f'{code}*{count}' for code, count in items)
    return 'http://deck.ourygo.top?' + urlencode(query, quote_via=quote)


@lru_cache(maxsize=128)
def _read_decks(path: str, modified: int, size: int, created: int):
    # 文件来自通过范围检查的录像目录。缓存不可变结果，避免每次打开页面重复解压。
    blob = Path(path).read_bytes()
    return tuple((name, tuple(main), tuple(extra)) for name, main, extra in decks_from_replay(blob))


def observed_deck(path: Path, player: str):
    try:
        info = path.stat()
        if not 32 <= info.st_size <= 16 * 1024 * 1024:
            return None
        for name, main, extra in _read_decks(str(path), info.st_mtime_ns, info.st_size, info.st_ctime_ns):
            if name == player and main:
                # 单局录像不包含完整副卡组，保留未知状态，不借用别的玩家或别场卡组。
                return list(main), list(extra)
    except (OSError, ValueError, EOFError):
        pass
    return None
