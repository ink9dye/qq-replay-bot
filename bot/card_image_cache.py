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
# 2026-10 起 momobako 渐进上新路径：新卡（如 IMPH 的 31321471）只有 ygoimg，
# 老 ygopro/pics 全 404；ygoimg 支持即时转码（!/format/jpg = 原分辨率 jpeg）。
IMG_MOMO = "https://cdn.233.momobako.com/ygoimg/ygopro/%d.webp!/format/jpg"
IMG_THUMB = "https://cdn.233.momobako.com/ygoimg/ygopro/%d.webp!/format/webp/fw/400/quality/85"

IMAGE_RE = re.compile(r"/(?:pics/|ygoimg/ygopro/)(\d+)\.(?:jpg|jpeg|png|webp)", re.I)
MAX_BYTES = 2 * 1024 * 1024
THUMB_MAX = (120, 165)
MAP_FILE = "card-image-ids.json"
MAP_TTL = 30 * 86400
SUPER_PRE_FLOOR = 100_000_000  # 先行/超先行图号段（100xxxxxx / 101xxxxxx）
UA = {"User-Agent": "qq-replay-bot/card-image"}
PLACEHOLDER = "_placeholder.webp"  # 缺图时的兜底卡面，全站共用一张
COOLING_SECONDS = 600  # 失败后的冷却：同一张卡同一用途 10 分钟内不再打源站
MISS_LOG = ("logs", "card-image-misses.jsonl")

_lock = threading.RLock()
_maps: dict[str, dict[int, tuple[int, float]]] = {}
_stamps: dict[str, int] = {}
_pending: dict[tuple[str, int, str], Future] = {}
_thumb_pending: set[tuple[str, int]] = set()
_unavailable: dict[tuple[str, int, str], float] = {}
_miss_logged: dict[tuple[str, int, str, str], float] = {}
_hits: dict[tuple[str, int], str] = {}
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


def _remember_hit(data_dir, card_id, url) -> None:
    """记录某张卡实际下到图的 URL；聊天兜底链接优先复用已知可用源。"""
    with _lock:
        _hits[(_key(data_dir), int(card_id))] = url


def _hit(data_dir, card_id) -> str | None:
    with _lock:
        return _hits.get((_key(data_dir), int(card_id)))


def ensure_full(data_dir, card_id, timeout=12) -> Path | None:
    """本地有直接返回；没有才解析 image id 并下载原图，按卡密落盘。"""
    card_id = int(card_id)
    found = local_full(data_dir, card_id)
    if found is not None:
        return found
    if cooling(data_dir, card_id, "full"):
        return None
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
            # 普通卡：老路径优先（老卡快），新卡老路径 404 后由 ygoimg 兜底。
            candidates.extend([FULL_MOMO % card_id, IMG_MOMO % card_id])
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
                _remember_hit(data_dir, card_id, url)
                result = full_path(data_dir, card_id)
                break
        if result is None and card_id < 100_000_000:
            image = image_id(data_dir, card_id) or card_id
            candidates = []
            if image != card_id:
                candidates.append(FULL_PRE % image)
            candidates.extend([FULL_MOMO % image, IMG_MOMO % image,
                               FULL_MOMO % card_id, IMG_MOMO % card_id, FULL_PRE % card_id])
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
                    _remember_hit(data_dir, card_id, url)
                    result = full_path(data_dir, card_id)
                    break
    finally:
        with _lock:
            _pending.pop(key, None)
        if result is None:
            mark_cooling(data_dir, card_id, "full", "no_source")
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
    if cooling(data_dir, card_id, "thumb"):
        return None
    if card_id >= 100_000_000:
        full = ensure_full(data_dir, card_id)
        if full is not None and _derive_thumb(full, thumb_path(data_dir, card_id)):
            return thumb_path(data_dir, card_id)
        mark_cooling(data_dir, card_id, "thumb", "no_source")
        return None
    def take(candidate: int) -> bool:
        # 老路径 !thumb2 优先；新卡老路径 404 时由 ygoimg 缩略图兜底。
        for url in (THUMB_MOMO % candidate, IMG_THUMB % candidate):
            try:
                blob = _download(url, timeout=timeout)
            except OSError:
                continue
            if len(blob) < 64 or not _valid_image(blob):
                continue
            target = thumb_path(data_dir, card_id)
            if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
                if _save_blob(target, blob):
                    return True
                continue
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
    mark_cooling(data_dir, card_id, "thumb", "no_source")
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
    # 普通卡：回退图用 ygoimg（新卡只有这里才有图），浏览器可直接渲染 jpeg。
    return THUMB_MOMO % card_id, IMG_MOMO % card_id


def full_url(data_dir, card_id) -> str:
    """聊天兜底 URL：先行卡必须走 super-pre，否则 momobako 会 404。"""
    card_id = int(card_id)
    # 实际下到过图的源优先——新卡老路径 404，命中过 ygoimg 就直接给新路径。
    hit = _hit(data_dir, card_id)
    if hit:
        return hit
    image = image_id_cached(data_dir, card_id) or card_id
    if image != card_id:
        return FULL_PRE % image
    if card_id >= 100_000_000:
        return FULL_PRE % card_id
    return FULL_MOMO % card_id


# ---------------------------------------------------------------------------
# 统一入口：冷却（负缓存）、缺图台账、占位图
# ---------------------------------------------------------------------------

_PLACEHOLDER_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


def cooling(data_dir, card_id, want="full") -> bool:
    """同一张卡同一用途是否在失败冷却期内；期内不再打源站，直接走保底。"""
    key = (_key(data_dir), int(card_id), want)
    with _lock:
        stamp = _unavailable.get(key)
        if stamp is None:
            return False
        if time.time() - stamp < COOLING_SECONDS:
            return True
        _unavailable.pop(key, None)
        return False


def mark_cooling(data_dir, card_id, want="full", reason="no_source", entry="") -> None:
    key = (_key(data_dir), int(card_id), want)
    with _lock:
        _unavailable[key] = time.time()
    log_miss(data_dir, card_id, want, reason, entry)


def log_miss(data_dir, card_id, want="full", reason="no_source", entry="") -> None:
    """缺图台账：同卡同用途同入口 10 分钟只记一行，避免刷日志。"""
    try:
        card_id = int(card_id)
    except (TypeError, ValueError):
        return
    dedup = (_key(data_dir), card_id, want, entry)
    now = time.time()
    with _lock:
        if now - _miss_logged.get(dedup, 0) < COOLING_SECONDS:
            return
        if len(_miss_logged) > 4096:
            _miss_logged.clear()
        _miss_logged[dedup] = now
    folder = Path(data_dir).joinpath(*MISS_LOG[:-1])
    try:
        folder.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": round(now, 3), "code": card_id, "want": want,
                           "reason": reason, "entry": entry}, ensure_ascii=False)
        with (folder / MISS_LOG[-1]).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _build_placeholder(path: Path) -> Path | None:
    """造一张 240x330 的「图暂缺」卡面；没有 PIL 或字体时返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    width, height = 240, 330
    try:
        image = Image.new("RGB", (width, height), "#26303d")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((7, 7, width - 8, height - 8), radius=12, outline="#44546a", width=2)
        title, note = "图暂缺", "IMAGE NOT AVAILABLE"
        font = None
        for candidate in _PLACEHOLDER_FONTS:
            try:
                font = ImageFont.truetype(candidate, 30)
                break
            except OSError:
                continue
        if font is None:
            title, note = "NO IMAGE", ""
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), title, font=font)
        draw.text(((width - (box[2] - box[0])) / 2, height / 2 - 30), title, fill="#aab6c6", font=font)
        if note:
            try:
                small = ImageFont.truetype(font.path, 11)
            except (OSError, AttributeError):
                small = ImageFont.load_default()
            box = draw.textbbox((0, 0), note, font=small)
            draw.text(((width - (box[2] - box[0])) / 2, height / 2 + 12), note, fill="#6f8098", font=small)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        image.save(temporary, "WEBP", quality=85, method=4)
        os.replace(temporary, path)
        return path
    except (OSError, ValueError, AttributeError):
        return None


def placeholder_path(data_dir) -> Path | None:
    """占位卡面路径；首次调用时生成并落盘，之后只读。"""
    path = Path(data_dir) / "card-images" / PLACEHOLDER
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        pass
    return _build_placeholder(path)


def placeholder_bytes(data_dir) -> bytes:
    path = placeholder_path(data_dir)
    if path is None:
        return b""
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _result(path: Path, source: str) -> dict:
    suffix = path.suffix.lower()
    mime = {".webp": "image/webp", ".png": "image/png"}.get(suffix, "image/jpeg")
    return {"path": path, "mime": mime, "source": source, "hit": True}


def resolve(data_dir, card_id, want="full", allow_fetch=True, entry="") -> dict:
    """全站唯一取图入口：本地 → 网络 → 占位图，失败进冷却并记台账。

    返回 {'path','mime','source','hit'}；source 为 'placeholder' 表示走了保底。
    网页线程要避免阻塞时应传 allow_fetch=False，由调用方决定 302 还是占位。
    """
    try:
        card_id = int(card_id)
    except (TypeError, ValueError):
        return {"path": None, "mime": "", "source": "invalid", "hit": False}
    if want == "thumb":
        found = local_thumb(data_dir, card_id)
        if found is not None:
            return _result(found, "local-thumb")
        full = local_full(data_dir, card_id)
        if full is not None and _derive_thumb(full, thumb_path(data_dir, card_id)):
            return _result(thumb_path(data_dir, card_id), "derived")
        if allow_fetch and not cooling(data_dir, card_id, "thumb"):
            found = ensure_thumb(data_dir, card_id)
            if found is not None:
                return _result(found, "network")
    else:
        found = local_full(data_dir, card_id)
        if found is not None:
            return _result(found, "local-full")
        if allow_fetch and not cooling(data_dir, card_id, "full"):
            found = ensure_full(data_dir, card_id)
            if found is not None:
                return _result(found, "network")
    fallback = placeholder_path(data_dir)
    if fallback is None:
        return {"path": None, "mime": "", "source": "none", "hit": False}
    log_miss(data_dir, card_id, want, "placeholder", entry)
    return {"path": fallback, "mime": "image/webp", "source": "placeholder", "hit": False}
