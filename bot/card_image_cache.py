"""本地优先的卡图缓存。

卡密和卡图文件名并不总是同一个数字（例如 58406693 的图在 101307051.jpg），
百鸽卡页的 <img src=".../pics/<image_id>.jpg"> 才是权威映射。本模块负责：

* 从百鸽卡页解析并持久化 卡密 -> image id（data/card-image-ids.json）；
* 原图按卡密落盘 data/card-images/<卡密>.jpg，之后优先读本地；
* 缩略图按卡密落盘 data/card-thumbs/<卡密>.webp，供网页和拼图使用。

网络只在本地缺失时发生；同一张卡的多线程请求合并为一次下载。

映射只在能被校验时才落盘：卡号 ≥ 100000000（超先行，图按卡号存放在 super-pre）不需要映射；
普通卡号的图名必须是 ≥ 100000000 的先行图号——把另一张普通卡的图记到本卡上，比没有映射更糟。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

PAGE_URL = "https://ygocdb.com/card/%d"
FULL_MOMO = "https://cdn.233.momobako.com/ygopro/pics/%d.jpg"
FULL_PRE = "https://cdntx.moecube.com/ygopro-super-pre/data/pics/%d.jpg"
THUMB_MOMO = "https://cdn.233.momobako.com/ygopro/pics/%d.jpg!thumb2"

IMAGE_RE = re.compile(r"/pics/(\d+)\.(?:jpg|jpeg|png|webp)", re.I)
MAX_BYTES = 2 * 1024 * 1024
THUMB_MAX = (120, 165)
MAP_FILE = "card-image-ids.json"
MAP_TTL = 30 * 86400
SUPER_PRE_FLOOR = 100_000_000  # 先行/超先行图号段（100xxxxxx / 101xxxxxx）
UA = {"User-Agent": "qq-replay-bot/card-image"}

_lock = threading.RLock()
_maps: dict[str, dict[int, tuple[int, float]]] = {}
_stamps: dict[str, int] = {}
_pending: dict[tuple[str, int, str], Future] = {}
_thumb_pending: set[tuple[str, int]] = set()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="card-image")
_opener = build_opener(ProxyHandler({}))


def _key(data_dir) -> str:
    return str(Path(data_dir).resolve())


def _map_path(data_dir) -> Path:
    return Path(data_dir) / MAP_FILE


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def usable_image_id(card_id, image_id) -> bool:
    """卡密 -> 图名 是否可信；不可信的映射一律不读、不写。

    可信的只有一种：普通卡号（<100000000）对应到先行图号（≥100000000），
    例如 58406693 -> 101307051。

    排除两类：
    * 卡号本身 ≥100000000：超先行图就按卡号放在 super-pre，查映射只会查错；
    * 图名也是普通卡号且不等于卡号：两个普通卡号互相当图名，只可能是百鸽页
      跳转/未收录时抓到了别张卡的第一张图（例如 100268010 -> 25750986）。
    """
    try:
        card_id, image_id = int(card_id), int(image_id)
    except (TypeError, ValueError):
        return False
    if not 0 < card_id <= 0xFFFFFFFF or not 0 < image_id <= 0xFFFFFFFF:
        return False
    if card_id >= SUPER_PRE_FLOOR:
        return False
    return image_id >= SUPER_PRE_FLOOR


def load_map(data_dir) -> dict[int, tuple[int, float]]:
    """读取映射；按文件 mtime 缓存，避免每次查卡都读盘。"""
    key = _key(data_dir)
    path = _map_path(data_dir)
    stamp = _stamp(path)
    with _lock:
        cached = _maps.get(key)
        if cached is not None and _stamps.get(key) == stamp:
            return cached
        cards: dict[int, tuple[int, float]] = {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            raw = document.get("cards") if isinstance(document, dict) else None
            if isinstance(raw, dict):
                for name, value in raw.items():
                    if not (isinstance(name, str) and name.isdigit() and isinstance(value, dict)):
                        continue
                    image = value.get("image")
                    checked = value.get("checked_at")
                    if (isinstance(image, int) and isinstance(checked, (int, float))
                            and usable_image_id(name, image)):
                        cards[int(name)] = (int(image), float(checked))
        except (OSError, ValueError):
            pass
        _maps[key] = cards
        _stamps[key] = stamp
        return cards


def _write_map(data_dir, cards: dict[int, tuple[int, float]]) -> None:
    path = _map_path(data_dir)
    payload = {
        "version": 1,
        "cards": {str(k): {"image": v[0], "checked_at": v[1]} for k, v in cards.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        return
    with _lock:
        _stamps[_key(data_dir)] = _stamp(path)


def remember(data_dir, card_id, image_id, checked_at=None) -> bool:
    """记录 卡密 -> image id；先合并磁盘上的最新条目，避免跨进程覆盖。

    不可信的映射直接丢弃（见 usable_image_id），返回 False。
    """
    if not usable_image_id(card_id, image_id):
        return False
    card_id, image_id = int(card_id), int(image_id)
    now = float(checked_at or time.time())
    with _lock:
        cards = dict(load_map(data_dir))
        disk = load_map(data_dir)
        for key, value in disk.items():
            cards.setdefault(key, value)
        cards[card_id] = (image_id, now)
        _maps[_key(data_dir)] = cards
        _write_map(data_dir, cards)
    return True


def parse_image_id(html) -> int | None:
    match = IMAGE_RE.search(str(html or ""))
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if 0 < value <= 0xFFFFFFFF else None


def _fetch_page(card_id, timeout=2.5) -> tuple[str | None, str]:
    try:
        with _opener.open(Request(PAGE_URL % int(card_id), headers=UA), timeout=timeout) as response:
            body = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
            return body, str(response.geturl() or "")
    except (OSError, ValueError):
        return None, ""


def _same_card_page(url, card_id) -> bool:
    """百鸽是否把我们留在这张卡的页面上；跳转到别的卡页时不能信页面里的第一张图。"""
    try:
        path = urlsplit(str(url or "")).path.rstrip("/")
    except ValueError:
        return False
    return path == "/card/%d" % int(card_id)


def remember_page(data_dir, card_id, html) -> int | None:
    """从已抓到的卡页补映射；页面不是这张卡的、图名不可信时都不落盘。"""
    try:
        card_id = int(card_id)
    except (TypeError, ValueError):
        return None
    if card_id >= SUPER_PRE_FLOOR:
        return None
    if "/card/%d" % card_id not in str(html or ""):
        return None
    image = parse_image_id(html)
    if image is None or not usable_image_id(card_id, image):
        return None
    remember(data_dir, card_id, image)
    return image


def image_id(data_dir, card_id, allow_fetch=True) -> int | None:
    """卡密对应的卡图文件名；本地未知时才抓百鸽卡页。"""
    card_id = int(card_id)
    entry = load_map(data_dir).get(card_id)
    if entry and (not allow_fetch or time.time() - entry[1] < MAP_TTL):
        return entry[0]
    if not allow_fetch:
        return entry[0] if entry else None
    if card_id >= SUPER_PRE_FLOOR:
        # 超先行图按卡号直取 super-pre，不需要也不该写映射。
        return entry[0] if entry else None
    html, final_url = _fetch_page(card_id)
    if html is None or not _same_card_page(final_url, card_id):
        return entry[0] if entry else None
    image = parse_image_id(html)
    if image is None or not usable_image_id(card_id, image):
        return entry[0] if entry else None
    remember(data_dir, card_id, image)
    return image


def image_id_cached(data_dir, card_id) -> int | None:
    return image_id(data_dir, card_id, allow_fetch=False)


def full_path(data_dir, card_id) -> Path:
    return Path(data_dir) / "card-images" / (str(int(card_id)) + ".jpg")


def thumb_path(data_dir, card_id) -> Path:
    return Path(data_dir) / "card-thumbs" / (str(int(card_id)) + ".webp")


def local_full(data_dir, card_id) -> Path | None:
    path = full_path(data_dir, card_id)
    try:
        if path.is_file() and 0 < path.stat().st_size <= MAX_BYTES:
            return path
    except OSError:
        pass
    return None


def local_thumb(data_dir, card_id) -> Path | None:
    folder = Path(data_dir) / "card-thumbs"
    for suffix in (".webp", ".jpg", ".png"):
        path = folder / (str(int(card_id)) + suffix)
        try:
            if path.is_file() and 0 < path.stat().st_size <= 4 * 1024 * 1024:
                return path
        except OSError:
            continue
    return None


def _valid_image(blob: bytes) -> bool:
    if not blob or len(blob) > MAX_BYTES:
        return False
    if blob[:2] == b"\xff\xd8" or blob[:4] == b"\x89PNG" or (blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"):
        return True
    return False


def _download(url: str, timeout=8) -> bytes:
    with _opener.open(Request(url, headers=UA), timeout=timeout) as response:
        return response.read(MAX_BYTES + 1)


def _save_blob(path: Path, blob: bytes) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(blob)
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def ensure_full(data_dir, card_id, timeout=12) -> Path | None:
    """本地有直接返回；没有才解析 image id 并下载原图，按卡密落盘。"""
    card_id = int(card_id)
    found = local_full(data_dir, card_id)
    if found is not None:
        return found
    key = (_key(data_dir), card_id, "full")
    with _lock:
        future = _pending.get(key)
        leader = future is None
        if leader:
            future = _pending[key] = Future()
    if not leader:
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            return None
    result = None
    try:
        candidates = []
        if card_id >= 100_000_000:
            # 萌卡超先行：图只在 super-pre，按卡号直取，不查百鸽页。
            candidates.extend([FULL_PRE % card_id, FULL_MOMO % card_id])
        else:
            candidates.append(FULL_MOMO % card_id)
        seen = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            try:
                blob = _download(url)
            except OSError:
                continue
            if _valid_image(blob) and _save_blob(full_path(data_dir, card_id), blob):
                result = full_path(data_dir, card_id)
                break
        if result is None and card_id < 100_000_000:
            image = image_id(data_dir, card_id) or card_id
            candidates = []
            if image != card_id:
                candidates.append(FULL_PRE % image)
            candidates.extend([FULL_MOMO % image, FULL_MOMO % card_id, FULL_PRE % card_id])
            for url in candidates:
                if url in seen:
                    continue
                seen.add(url)
                try:
                    blob = _download(url)
                except OSError:
                    continue
                if not _valid_image(blob):
                    continue
                if _save_blob(full_path(data_dir, card_id), blob):
                    result = full_path(data_dir, card_id)
                    break
    finally:
        with _lock:
            _pending.pop(key, None)
        try:
            future.set_result(result)
        except Exception:
            pass
    return result


def _derive_thumb(source: Path, target: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail(THUMB_MAX, Image.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            image.save(temporary, "WEBP", quality=82, method=4)
            os.replace(temporary, target)
            return True
    except (OSError, ValueError, ImportError):
        return False


def ensure_thumb(data_dir, card_id, timeout=8) -> Path | None:
    """缩略图优先本地；缺图时先试 !thumb2，先行卡再回退原图离线缩。"""
    card_id = int(card_id)
    found = local_thumb(data_dir, card_id)
    if found is not None:
        return found
    full = local_full(data_dir, card_id)
    if full is not None and _derive_thumb(full, thumb_path(data_dir, card_id)):
        return thumb_path(data_dir, card_id)
    if card_id >= 100_000_000:
        full = ensure_full(data_dir, card_id)
        if full is not None and _derive_thumb(full, thumb_path(data_dir, card_id)):
            return thumb_path(data_dir, card_id)
        return None
    def take(candidate: int) -> bool:
        try:
            blob = _download(THUMB_MOMO % candidate, timeout=timeout)
        except OSError:
            return False
        if len(blob) < 64 or not _valid_image(blob):
            return False
        target = thumb_path(data_dir, card_id)
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            return _save_blob(target, blob)
        source = target.with_name(target.name + ".src")
        if _save_blob(source, blob) and _derive_thumb(source, target):
            try:
                source.unlink()
            except OSError:
                pass
            return True
        return False

    if take(card_id):
        return thumb_path(data_dir, card_id)
    image = image_id(data_dir, card_id) or card_id
    if image != card_id and image < SUPER_PRE_FLOOR and take(image):
        return thumb_path(data_dir, card_id)
    full = ensure_full(data_dir, card_id)
    if full is not None and _derive_thumb(full, thumb_path(data_dir, card_id)):
        return thumb_path(data_dir, card_id)
    return None


def schedule_thumb(data_dir, card_id) -> None:
    """网页线程只排后台任务，不阻塞等待网络。"""
    card_id = int(card_id)
    if local_thumb(data_dir, card_id) is not None:
        return
    key = (_key(data_dir), card_id)
    with _lock:
        if key in _thumb_pending:
            return
        _thumb_pending.add(key)
    def work():
        try:
            ensure_thumb(data_dir, card_id)
        finally:
            with _lock:
                _thumb_pending.discard(key)
    try:
        _pool.submit(work)
    except RuntimeError:
        with _lock:
            _thumb_pending.discard(key)


def page_urls(data_dir, card_id) -> tuple[str, str]:
    """分享页用的（主图, 回退图）：普通卡先 thumb2，先行卡回退 super-pre 原图。"""
    card_id = int(card_id)
    image = image_id_cached(data_dir, card_id) or card_id
    if image != card_id:
        if image >= SUPER_PRE_FLOOR:
            # 先行图号在 momobako 可能是另一张卡，只给 super-pre 原图。
            return FULL_PRE % image, FULL_PRE % image
        return THUMB_MOMO % image, FULL_PRE % image
    if card_id >= 100_000_000:
        # 萌卡超先行没有 !thumb2，直接给 super-pre 原图。
        return FULL_PRE % card_id, FULL_MOMO % card_id
    return THUMB_MOMO % card_id, FULL_MOMO % card_id


def full_url(data_dir, card_id) -> str:
    """聊天兜底 URL：先行卡必须走 super-pre，否则 momobako 会 404。"""
    card_id = int(card_id)
    image = image_id_cached(data_dir, card_id) or card_id
    if image != card_id:
        return FULL_PRE % image
    if card_id >= 100_000_000:
        return FULL_PRE % card_id
    return FULL_MOMO % card_id
