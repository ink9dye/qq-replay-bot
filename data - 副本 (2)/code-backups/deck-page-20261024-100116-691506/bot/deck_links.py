"""复用 watch 的录像解析，生成 OURYGO 官方 v1 格式卡组链接。"""
from __future__ import annotations

import base64
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

from .watch_core import decks_from_replay


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
