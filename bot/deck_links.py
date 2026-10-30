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

from .watch_core import decks_from_replay, card_name_map, deck_title_for

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
    from .arena_profile import arena_points as profile_points
    return profile_points(player, refresh=False)


def observed_title(main, extra, side=None, cache_root=None) -> str:
    """卡组名复用 watch.py：先按相似度匹配参考卡组，否则按卡名字段猜测。"""
    return deck_title_for(main, extra, side or [])


def card_names(cache_root=None) -> dict:
    """返回 id -> 卡名 映射，复用 watch.py 的卡表缓存。"""
    return card_name_map()


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
    for name, main, extra in observed_decks(path):
        if name == player and main:
            return list(main), list(extra)
    return None


def observed_decks(path: Path):
    try:
        info = path.stat()
        if not 32 <= info.st_size <= 16 * 1024 * 1024:
            return ()
        return _read_decks(str(path), info.st_mtime_ns, info.st_size, info.st_ctime_ns)
    except (OSError, ValueError, EOFError):
        pass
    return ()


def matchup_labels(replay: dict, path: Path, cache_root: Path):
    titles = {name: observed_title(list(main), list(extra), [], cache_root)
              for name, main, extra in observed_decks(path)}
    players = [name for name in replay['players'] if name and name != '[Server]'][:4]
    if len(players) == 4:
        players = [players[0], players[2], players[1], players[3]]
        split = 2
    else:
        split = 1
    labels = [f'{name} · {titles.get(name) or "类型待识别"}' for name in players]
    heading = ' ＆ '.join(labels[:split]) + ' vs ' + ' ＆ '.join(labels[split:]) if len(labels) > 1 else ''.join(labels)
    if not any(titles.values()):
        return heading, path.name
    from datetime import datetime
    from .watch_core import safe_filename_part
    names = []
    for name in players:
        rank = replay['ranks'].get(name)
        rank_label = '排名未知' if rank is None else ('未上榜' if rank <= 0 else str(rank))
        names.append(f'“{safe_filename_part(name)}”（{rank_label}）【{safe_filename_part(titles.get(name) or "类型待识别")}】')
    middle = '＆'.join(names[:split]) + 'vs' + '＆'.join(names[split:]) if len(names) > 1 else ''.join(names)
    return heading, datetime.fromtimestamp(replay['recorded_at']).strftime('%H-%M') + ' ' + middle + '.yrp'
