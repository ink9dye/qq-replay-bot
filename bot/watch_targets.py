"""只读取独立观战名单；不覆盖账号、QQ授权或订阅设置。"""
import json
from pathlib import Path

_cache = {}


def standalone_targets(path):
    path = Path(path)
    key = str(path.absolute())
    try:
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        if key in _cache and _cache[key][0] == stamp:
            return list(_cache[key][1])
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict): return []
        values = []
        for field in ('watch_any', 'watch_names'):
            if isinstance(value.get(field), list): values.extend(value[field])
        if isinstance(value.get('watch_vs_top'), list):
            values.extend(item.get('name') for item in value['watch_vs_top'] if isinstance(item, dict))
        names = list(dict.fromkeys(name.strip() for name in values if isinstance(name, str) and name.strip()))
        _cache[key] = (stamp, names)
        return names
    except (OSError, ValueError):
        # 临时写入不完整时沿用上次成功名单，不停止已经在看的小局。
        return list(_cache.get(key, (None, []))[1])
