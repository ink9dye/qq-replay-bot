"""竞技分数与排名同次查询、持久化成功快照，短暂失败不清空已有数据。"""
import hashlib
import json
import math
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_LOCKS = [threading.Lock() for _ in range(64)]
_CACHE = {}


class ArenaScore(float):
    def __new__(cls, profile, stale=False):
        result = super().__new__(cls, profile['pt'])
        result.rank = profile['rank']
        result.updated_at = profile['updated_at']
        result.stale = stale
        return result


def parse_profile(payload, player, now):
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict) or isinstance(payload.get('pt'), bool) or isinstance(payload.get('arena_rank'), bool):
        raise ValueError('invalid profile')
    score, rank = float(payload['pt']), int(payload['arena_rank'])
    if not math.isfinite(score) or not 0 <= score <= 1e9 or rank < 0:
        raise ValueError('invalid profile numbers')
    return {'player': player, 'pt': score, 'rank': rank, 'updated_at': now}


def fetch_profile(player):
    request = Request('https://sapi.moecube.com:444/ygopro/arena/user?' + urlencode({'username': player}),
                      headers={'User-Agent': 'qq-replay-bot/arena-profile'})
    with urlopen(request, timeout=4) as response:
        return json.loads(response.read(65536))


def arena_points(player, cache_root=None, *, fetch=None, clock=None, refresh=True):
    clock, fetch = clock or time.time, fetch or fetch_profile
    if cache_root is None:
        from .watch_core import DATA_DIR
        cache_root = Path(DATA_DIR) / 'cache'
    directory = Path(cache_root) / 'arena-profiles'
    digest = hashlib.sha256(player.encode()).hexdigest()
    path = directory / (digest + '.json')
    key = (str(directory.absolute()), player)
    # 同玩家并发等待同一次真实查询；不再提前写入None导致另一页面显示无分数。
    with _LOCKS[int(digest[:4], 16) % len(_LOCKS)]:
        now = clock()
        previous, retry_at = _CACHE.get(key, (None, 0))
        if previous is None:
            try:
                saved = json.loads(path.read_text(encoding='utf8'))
                previous = parse_profile({'pt': saved['pt'], 'arena_rank': saved['rank']}, player, float(saved['updated_at']))
                if saved['player'] != player or previous['updated_at'] > now + 60:
                    previous = None
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if previous and now - previous['updated_at'] < 60:
            return ArenaScore(previous)
        if not refresh:
            return ArenaScore(previous, stale=True) if previous else None
        if now < retry_at:
            return ArenaScore(previous, stale=True) if previous else None
        for _ in range(2):
            try:
                profile = parse_profile(fetch(player), player, clock())
                directory.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                try:
                    temporary.write_text(json.dumps(profile, ensure_ascii=False), encoding='utf8')
                    temporary.replace(path)
                except OSError:
                    pass  # 本机磁盘失败仍保留内存中的成功结果。
                _CACHE[key] = (profile, clock() + 60)
                if len(_CACHE) > 512:
                    _CACHE.pop(next(iter(_CACHE)))
                return ArenaScore(profile)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        _CACHE[key] = (previous, clock() + 15)
        return ArenaScore(previous, stale=True) if previous else None
