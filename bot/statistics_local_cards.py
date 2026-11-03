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
_image_futures = {}
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='card-image')
REMOTE = 'https://cdn.233.momobako.com/ygopro/pics/%d.jpg'
REMOTE_FALLBACK = 'https://cdntx.moecube.com/ygopro-super-pre/data/pics/%d.jpg'
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
            return _image_futures.get(code)
        if now - _failed.get(code, 0) < RETRY_SECONDS:
            return
        _pending.add(code)
    def work():
        try:
            pics.mkdir(parents=True, exist_ok=True)
            existing = _existing_picture(pics, code)
            if existing is not None:
                return existing[0]
            for candidate in _image_codes(code):
                for template in (REMOTE, REMOTE_FALLBACK):
                    try:
                        with urllib.request.urlopen(template % candidate, timeout=10) as response:
                            blob = response.read(2 * 1024 * 1024 + 1)
                    except OSError:
                        continue
                    if not blob or len(blob) > 2 * 1024 * 1024:
                        continue
                    path = (pics / (str(candidate) + '.jpg')).resolve()
                    if not path.is_relative_to(pics):
                        continue
                    temporary = path.with_suffix('.jpg.tmp')
                    temporary.write_bytes(blob)
                    os.replace(temporary, path)
                    with _lock:
                        _failed.pop(code, None)
                    return path
            raise ValueError('no image for card')
        except (OSError, ValueError):
            with _lock:
                _failed[code] = time.time()
        finally:
            with _lock:
                _pending.discard(code)
                _image_futures.pop(code, None)
    with _lock:
        future = _pool.submit(work)
        _image_futures[code] = future
        return future


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


def _existing_picture(pics, code):
    for candidate in _image_codes(code):
        for suffix, mime in (('.jpg', 'image/jpeg'), ('.png', 'image/png'), ('.webp', 'image/webp')):
            path = (pics / (str(candidate) + suffix)).resolve()
            if path.is_relative_to(pics) and path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
                return path, mime
    return None


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
    found = _existing_picture(pics, code)
    if found is not None:
        return found
    # 卡号可能是本体号，实际卡图存在 100xxxxxx 异画编号；后台缓存，当前请求立即返回。
    _schedule(data_dir, code, pics)
    return None


THUMB_REMOTE = 'https://cdn.233.momobako.com/ygopro/pics/%d.jpg!thumb2'
THUMB_MAX = (120, 165)
_thumb_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='card-thumb')
_thumb_pending = set()
_thumb_failed = {}


def thumb_dir(data_dir):
    return (Path(data_dir) / 'card-thumbs').resolve()


def _thumb_file(thumbs, code):
    for candidate in _image_codes(code):
        for suffix in ('.webp', '.jpg', '.png'):
            path = thumbs / (str(candidate) + suffix)
            try:
                if path.is_file() and 0 < path.stat().st_size <= 4 * 1024 * 1024:
                    return path
            except OSError:
                continue
    return None


def _derive_thumb(thumbs, source, code):
    """从已下载的原图离线缩一张缩略图；只落盘，不联网。"""
    try:
        from PIL import Image
        with Image.open(source) as image:
            image = image.convert('RGB')
            image.thumbnail(THUMB_MAX, Image.LANCZOS)
            thumbs.mkdir(parents=True, exist_ok=True)
            path = thumbs / (str(code) + '.webp')
            temporary = path.with_suffix('.webp.tmp')
            image.save(temporary, 'WEBP', quality=82, method=4)
            os.replace(temporary, path)
            return path
    except (OSError, ValueError, ImportError):
        return None


def _schedule_thumb(data_dir, code, thumbs, pics=None):
    now = time.time()
    with _lock:
        if code in _thumb_pending:
            return
        if now - _thumb_failed.get(code, 0) < RETRY_SECONDS:
            return
        _thumb_pending.add(code)
    def work():
        try:
            thumbs.mkdir(parents=True, exist_ok=True)
            for candidate in _image_codes(code):
                try:
                    with urllib.request.urlopen(THUMB_REMOTE % candidate, timeout=15) as response:
                        blob = response.read(2 * 1024 * 1024 + 1)
                except OSError:
                    continue
                if len(blob) < 64 or len(blob) > 2 * 1024 * 1024:
                    continue
                if blob[:4] == b'RIFF' and blob[8:12] == b'WEBP':
                    suffix = '.webp'
                elif blob[:2] == b'\xff\xd8':
                    suffix = '.jpg'
                elif blob[:4] == b'\x89PNG':
                    suffix = '.png'
                else:
                    continue
                path = (thumbs / (str(candidate) + suffix)).resolve()
                if not path.is_relative_to(thumbs):
                    continue
                temporary = path.with_suffix(suffix + '.tmp')
                temporary.write_bytes(blob)
                os.replace(temporary, path)
                with _lock:
                    _thumb_failed.pop(code, None)
                return
            raise ValueError('no thumb')
        except (OSError, ValueError):
            with _lock:
                _thumb_failed[code] = time.time()
            # 缩略图不存在（多为先行卡）时，让原图走慢速后台下载，下次渲染可从本地缩。
            if pics is not None:
                _schedule(data_dir, code, pics)
        finally:
            with _lock:
                _thumb_pending.discard(code)
    _thumb_pool.submit(work)


def thumb_picture(data_dir, code):
    """拼图用缩略图：优先 3.5KB 的 !thumb2，其次本地原图离线缩，都没有则后台补。"""
    number = _card_number(code)
    if number is None or not 0 < number <= 4294967295:
        return None
    thumbs = thumb_dir(data_dir)
    found = _thumb_file(thumbs, number)
    if found is not None:
        return found
    root = library_root(data_dir)
    pics = (root / 'pics').resolve() if root is not None else None
    if pics is not None and not pics.is_relative_to(root):
        pics = None
    if pics is not None:
        existing = _existing_picture(pics, number)
        if existing is not None:
            derived = _derive_thumb(thumbs, existing[0], number)
            if derived is not None:
                return derived
            return existing[0]
    _schedule_thumb(data_dir, number, thumbs, pics)
    return None


def wait_for_thumbs(data_dir, codes, timeout=3):
    """预热缩略图并最多等待 timeout 秒；本地已有的原图直接离线缩，不走网络。"""
    numbers = {_card_number(c) for c in codes}
    numbers = {n for n in numbers if n is not None and 0 < n <= 4294967295}
    if not numbers:
        return
    thumbs = thumb_dir(data_dir)
    root = library_root(data_dir)
    pics = (root / 'pics').resolve() if root is not None else None
    if pics is not None and not pics.is_relative_to(root):
        pics = None
    pending = []
    for code in numbers:
        if _thumb_file(thumbs, code) is not None:
            continue
        if pics is not None:
            existing = _existing_picture(pics, code)
            if existing is not None and _derive_thumb(thumbs, existing[0], code) is not None:
                continue
        pending.append(code)
        _schedule_thumb(data_dir, code, thumbs, pics)
    if not pending:
        return
    deadline = time.time() + max(0, timeout)
    while time.time() < deadline:
        remaining = [code for code in pending if _thumb_file(thumbs, code) is None]
        with _lock:
            active = [code for code in remaining if code in _thumb_pending]
        if not active:
            return
        time.sleep(0.1)
