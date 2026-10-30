"""Read-only local YGOPro public card library, enabled by an explicit local config."""
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

_cache = {}
_lock = threading.RLock()


def library_root(data_dir):
    try:
        value = json.loads((Path(data_dir) / 'card-library.json').read_text(encoding='utf-8-sig'))
        root = Path(value['ygopro_root']).resolve()
        return root if root.is_dir() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def local_index(data_dir):
    root = library_root(data_dir)
    if root is None:
        return {}
    files = [root / 'cards.cdb', *sorted((root / 'expansions').glob('*.cdb'))]
    files = [p for p in files if p.is_file() and p.resolve().is_relative_to(root)]
    stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    with _lock:
        old = _cache.get(str(root))
        if old and old[0] == stamp:
            return old[1]
        result = {}
        # Main library wins conflicts; supplementary expansion files fill absent names.
        for path in files:
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    for code, name in db.execute('SELECT id,name FROM texts'):
                        if type(code) is int and 0 < code <= 4294967295 and isinstance(name, str) and name.strip():
                            result.setdefault(str(code), dict(id=code, cn_name=name.strip(), name_source='local_ygopro'))
            except sqlite3.Error:
                continue
        _cache[str(root)] = (stamp, result)
        if len(_cache) > 8:
            _cache.pop(next(iter(_cache)))
        return result


def picture(data_dir, code):
    if type(code) is not int or not 0 < code <= 4294967295:
        return None
    root = library_root(data_dir)
    if root is None:
        return None
    pics = (root / 'pics').resolve()
    if not pics.is_relative_to(root):
        return None
    for suffix, mime in (('.jpg', 'image/jpeg'), ('.png', 'image/png'), ('.webp', 'image/webp')):
        path = (pics / (str(code) + suffix)).resolve()
        if path.is_relative_to(pics) and path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
            return path, mime
    return None
