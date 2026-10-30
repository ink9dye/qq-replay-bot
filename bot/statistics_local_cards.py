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
                    columns = {r[1] for r in db.execute('PRAGMA table_info(texts)')}
                    descriptions = {r[0]: r[1] for r in db.execute('SELECT id,desc FROM texts')} if 'desc' in columns else {}
                    types = {r[0]: r[1] for r in db.execute('SELECT id,type FROM datas')} if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='datas'").fetchone() else {}
                    for code, name in db.execute('SELECT id,name FROM texts'):
                        if type(code) is int and 0 < code <= 4294967295 and isinstance(name, str) and name.strip():
                            result.setdefault(str(code), dict(id=code, cn_name=name.strip(), description=descriptions.get(code, ''), card_type=types.get(code), name_source='local_ygopro'))
            except sqlite3.Error:
                continue
        _cache[str(root)] = (stamp, result)
        if len(_cache) > 8:
            _cache.pop(next(iter(_cache)))
        return result


def card_details(data_dir, code):
    from .statistics_card_names import index
    from .card_search import card_name
    info = index(data_dir).get(str(code), {})
    kind = info.get('card_type')
    extra = bool(kind & (0x40 | 0x2000 | 0x800000 | 0x4000000)) if type(kind) is int else None
    return dict(code=code, name=card_name(info) if info else '卡名暂缺',
                description=info.get('description') or (info.get('text') or {}).get('desc') or '',
                base_partition=('extra' if extra else 'main') if extra is not None else None,
                allowed_partitions=['extra', 'side'] if extra else ['main', 'side'] if extra is not None else ['main', 'side', 'extra'])


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
