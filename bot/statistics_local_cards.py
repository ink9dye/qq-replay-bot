"""Read-only local YGOPro public card library, enabled by an explicit local config."""
import json
import os
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

_cache = {}
_lock = threading.RLock()
_pending = set()
_failed = {}
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='card-image')
REMOTE = 'https://cdntx.moecube.com/ygopro-super-pre/data/pics/%d.jpg'
RETRY_SECONDS = 600


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


def _image_codes(code):
    """卡图文件名候选：本体号、100/101 前缀特殊编号。"""
    code = int(code)
    values = [code]
    if code >= 101_000_000:
        values.append(code - 101_000_000)
    if code >= 100_000_000:
        values.append(code - 100_000_000)
    if code < 100_000_000:
        values.extend([code + 100_000_000, code + 101_000_000])
    result = []
    for value in values:
        if 0 < value <= 4294967295 and value not in result:
            result.append(value)
    return result


def _schedule(data_dir, code, pics):
    now = time.time()
    with _lock:
        if code in _pending:
            return
        if now - _failed.get(code, 0) < RETRY_SECONDS:
            return
        _pending.add(code)
    def work():
        try:
            pics.mkdir(parents=True, exist_ok=True)
            for candidate in _image_codes(code):
                try:
                    with urllib.request.urlopen(REMOTE % candidate, timeout=60) as response:
                        blob = response.read(8 * 1024 * 1024 + 1)
                except OSError:
                    continue
                if not blob or len(blob) > 8 * 1024 * 1024:
                    continue
                path = (pics / (str(candidate) + '.jpg')).resolve()
                if not path.is_relative_to(pics):
                    continue
                temporary = path.with_suffix('.jpg.tmp')
                temporary.write_bytes(blob)
                os.replace(temporary, path)
                with _lock:
                    _failed.pop(code, None)
                return
            raise ValueError('no image for card')
        except (OSError, ValueError):
            with _lock:
                _failed[code] = time.time()
        finally:
            with _lock:
                _pending.discard(code)
    _pool.submit(work)


def _card_number(code):
    """回放里先行卡可能写成 <101307061>；统一取纯数字卡号。"""
    text = str(code).strip()
    if text.startswith('<') and text.endswith('>'):
        text = text[1:-1].strip()
    return int(text) if text.isdigit() else None


def prefetch(data_dir, codes):
    """后台预热一批卡图；不阻塞请求。"""
    root = library_root(data_dir)
    if root is None:
        return
    pics = (root / 'pics').resolve()
    if not pics.is_relative_to(root):
        return
    numbers = {_card_number(c) for c in codes}
    for code in numbers:
        if code is None or not 0 < code <= 4294967295:
            continue
        if any((pics / (str(candidate) + suffix)).is_file()
               for candidate in _image_codes(code) for suffix in ('.jpg', '.png', '.webp')):
            continue
        _schedule(data_dir, code, pics)



def wait_for(data_dir, codes, timeout=30):
    """预热并等待缺图落盘，最多 timeout 秒；给机器人构筑图用。"""
    prefetch(data_dir, codes)
    root = library_root(data_dir)
    if root is None:
        return
    pics = (root / 'pics').resolve()
    wanted = {int(c) for c in codes if str(c).isdigit()}
    deadline = time.time() + max(0, timeout)
    while time.time() < deadline:
        remaining = [c for c in wanted if not any((pics / (str(candidate) + suffix)).is_file()
                                                  for candidate in _image_codes(c)
                                                  for suffix in ('.jpg', '.png', '.webp'))]
        with _lock:
            pending = [c for c in remaining if c in _pending]
        if not pending:
            return
        time.sleep(0.2)


def picture(data_dir, code):
    number = _card_number(code)
    if number is None or not 0 < number <= 4294967295:
        return None
    code = number
    root = library_root(data_dir)
    if root is None:
        return None
    pics = (root / 'pics').resolve()
    if not pics.is_relative_to(root):
        return None
    for candidate in _image_codes(code):
        for suffix, mime in (('.jpg', 'image/jpeg'), ('.png', 'image/png'), ('.webp', 'image/webp')):
            path = (pics / (str(candidate) + suffix)).resolve()
            if path.is_relative_to(pics) and path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
                return path, mime
    # 卡号可能是本体号，实际卡图存在 100xxxxxx 异画编号；后台缓存，当前请求立即返回。
    _schedule(data_dir, code, pics)
    return None
