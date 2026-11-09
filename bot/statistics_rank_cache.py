# -*- coding: utf-8 -*-
"""非前100玩家名次的本地缓存（旁路缓存，不写事实库）。"""
import json
import os
import tempfile
import threading
from pathlib import Path

TTL = 14 * 86400
FUTURE_SKEW = 3600
MAX = 5000
_LOCK = threading.Lock()


def cache_path(data_dir):
    return Path(data_dir) / 'statistics-rank-cache.json'


def load(data_dir, now, ttl=TTL):
    """返回 {player: (rank, checked_at)}；超过 ttl 的条目丢弃（默认 14 天，与对局窗口一致）。"""
    try:
        payload = json.loads(cache_path(data_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    players = payload.get('players') if isinstance(payload, dict) else None
    if not isinstance(players, dict):
        return {}
    result = {}
    for name, entry in players.items():
        if not isinstance(entry, dict):
            continue
        rank = entry.get('rank')
        checked = entry.get('checked_at')
        age = float(now) - float(checked) if isinstance(checked, (int, float)) else ttl + 1
        # 查询的 now 可能比写入时刻早几分钟（排队后才计算），未来时间戳不能当过期：
        # 否则一次迟到的请求会把整份缓存改写成本次刚查到的那几条。
        if isinstance(rank, int) and rank > 0 and -FUTURE_SKEW <= age <= ttl:
            result[str(name).strip()] = (rank, float(checked))
    return result


def update(data_dir, values, now):
    """把新查到的名次合并进缓存；原子替换，失败静默（不影响查询）。"""
    fresh = {str(name).strip(): rank for name, rank in (values or {}).items()
             if isinstance(rank, int) and rank > 0 and str(name).strip()}
    if not fresh:
        return
    try:
        with _LOCK:
            current = load(data_dir, now)
            for name, rank in fresh.items():
                current[name] = (rank, float(now))
            items = sorted(current.items(), key=lambda item: item[1][1], reverse=True)[:MAX]
            payload = {'version': 1, 'updated_at': float(now),
                       'players': {name: {'rank': rank, 'checked_at': checked}
                                   for name, (rank, checked) in items}}
            path = cache_path(data_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.rank-cache-', suffix='.json')
            try:
                with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                    json.dump(payload, stream, ensure_ascii=False)
                os.replace(temp, path)
            except OSError:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
    except OSError:
        pass
