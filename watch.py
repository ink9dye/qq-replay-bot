# -*- coding: utf-8 -*-
"""统一观战后端：独立运行，或由QQ前端导入同一份逻辑。

用法:
    python watch.py
    python watch.py --name 玩家名
    python watch.py --name 玩家A --name 玩家B
    python watch.py --self-test

只使用 Python 标准库。独立配置在data/watch-config.json，运行数据统一在data。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import lzma
import os
import random
import re
import secrets
import socket
import ssl
import struct
import sys
import tempfile
import time
import zipfile
from collections import Counter
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
# 游戏目录必须由配置显式指定，不能再从本文件的上一级目录推断。
DEFAULT_YGOPRO_DIR = Path(r"D:\MyCardLibrary\ygopro")
YGOPRO_DIR = DEFAULT_YGOPRO_DIR
DATA_DIR = HERE / "data"
CONFIG_PATH = DATA_DIR / "watch-config.json"
SESSION_PATH = DATA_DIR / "session.json"
CACHE_DIR = DATA_DIR / "cache"
REFERENCE_DECKS_PATH = HERE / "reference_decks.json"


def configure_paths(ygopro_dir: Path, data_dir: Path) -> None:
    """登录状态和卡表缓存放在新项目的数据目录，卡图和脚本仍读游戏目录。"""
    global YGOPRO_DIR, DATA_DIR, CONFIG_PATH, SESSION_PATH, CACHE_DIR
    YGOPRO_DIR = Path(ygopro_dir)
    DATA_DIR = Path(data_dir)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH = DATA_DIR / "watch-config.json"
    SESSION_PATH = DATA_DIR / "session.json"
    CACHE_DIR = DATA_DIR / "cache"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
ACCOUNTS_API = "https://api.moecube.com/accounts/"
# 萌卡助手使用的进行中对局列表。游戏服仍是 tiramisu.moenext.com:8911。
DEFAULT_DUEL_LIST = "wss://tiramisu.moecube.com:8923/?filter=started"
REPLAY_MAGIC_YRP1 = 0x31707279
REPLAY_MAGIC_YRP2 = 0x32707279
REPLAY_MAGICS = {REPLAY_MAGIC_YRP1, REPLAY_MAGIC_YRP2}
REPLAY_UNIFORM = 0x10

CTOS_PLAYER_INFO = 0x10
CTOS_JOIN_GAME = 0x12
CTOS_LEAVE_GAME = 0x13
CTOS_HS_TOOBSERVER = 0x21

STOC_GAME_MSG = 0x01
STOC_ERROR_MSG = 0x02
STOC_JOIN_GAME = 0x12
STOC_TYPE_CHANGE = 0x13
STOC_DUEL_START = 0x15
STOC_DUEL_END = 0x16
STOC_REPLAY = 0x17
STOC_CHAT = 0x19
STOC_HS_PLAYER_ENTER = 0x20
STOC_HS_PLAYER_CHANGE = 0x21
STOC_HS_WATCH_CHANGE = 0x22

NETPLAYER_OBSERVER = 7
DEFAULT_VERSION = 0x1362
from retention_policy import RETENTION_SECONDS, next_cleanup_delay
from bot.join_throttle import DEFAULT_FAIL_THRESHOLD, DEFAULT_PAUSE, JoinGate

# 进场节流（观战侧口径，2026-10-31 用户定）：并发上限从 8 起**自动调**（6–8，不再往上），
# 两次进场至少隔 20 秒、连续 3 次失败暂停 60 秒。bot/join_throttle.py 自己的默认仍是 2/5/3/60，
# 由配置或这里覆盖。20 秒同时用于"计划进入时刻"的错峰（见 plan_gap_after）。
JOIN_CONCURRENT_DEFAULT = 8
JOIN_MIN_INTERVAL_DEFAULT = 20.0
# 自动调并发的边界：不低于 6（失败时退到这里），上限 8（不再自动往上，避免并行太多触发平台限制：
# 若以后确认安全，再人工把上限调高）；每 3 分钟评估一次，
# 有排队且同期 0 失败就 +2，被拒或有暂停就 -2（退下来后先冷却两个周期，避免来回抖）。
JOIN_CONCURRENT_FLOOR = 6
JOIN_CONCURRENT_CEILING = 8
JOIN_TUNE_INTERVAL = 180.0
JOIN_TUNE_STEP = 2
# 进场间隔加 0–5 秒随机抖动，避免固定 20.0 秒的机器节奏；每小时最多 60 次进场。
JOIN_INTERVAL_JITTER_DEFAULT = 5.0
JOIN_MAX_PER_HOUR_DEFAULT = 60
# 房间从"我们看到开局"到真正进场的最大等待。实测一局通常 ≥15 分钟，所以留到 15 分钟：
# 超过 15 分钟才判定"多半已到局末"；比 6 分钟能多捞回大量中盘对局。
JOIN_MAX_ROOM_AGE = 900.0
# 排名和优先只在开始时间差 ≤5 分钟的对局之间生效；超过则先看开始时间更早的。
JOIN_PRIORITY_WINDOW_SECONDS = 300.0
# 夜间降档：02:00–07:00 并发 4–6、间隔 30 秒，把累计曲线压平（可配置/可关）。
NIGHT_START_HOUR = 2
NIGHT_END_HOUR = 7
NIGHT_MIN_CONCURRENT = 4
NIGHT_MAX_CONCURRENT = 6
NIGHT_MIN_INTERVAL = 30.0
NIGHT_JITTER = 10.0
# 重启后追"启动前已在打"的局，最多追这么多；其余放弃（新 IP 刚解封时别一口气全撞）。
STARTUP_CATCHUP_LIMIT = 20
# 新 IP 观察期：启动/换网后前 10 分钟 4 路、30 秒；10 分钟内 0 失败才恢复。
PROBATION_SECONDS = 600.0
PROBATION_MAX_CONCURRENT = 4
PROBATION_MIN_INTERVAL = 30.0
# 出口 IP 被平台限制时的“熔断”：连续多次 0 包静默关闭 → 暂停 15 分钟，别再往上撞。
SILENT_STREAK_LIMIT = 6
SYSTEMIC_PAUSE_SECONDS = 900.0
# 1 小时成功率熔断：低于 50% 且样本 >=10 就停 15 分钟（比等全灭更早刹车）。
SUCCESS_BREAKER_RATE = 0.5
SUCCESS_BREAKER_MIN_SAMPLES = 10
SUCCESS_BREAKER_PAUSE_SECONDS = 900.0
SUCCESS_BREAKER_CHECK_SECONDS = 300.0
# 最近 30 分钟的进场/失败/峰值并发，写进 capture-status，用于和“被封前”对照。
STATS_WINDOW_SECONDS = 1800.0
# 启动前就在打的对局（积压）：到期窗口 20—40 秒随机错峰，让它们慢慢进，不在启动瞬间一起扑进去。
PREEXISTING_JOIN_MIN_DEFAULT = 20.0
PREEXISTING_JOIN_MAX_DEFAULT = 40.0
# bot/service.py 在 config.json 没配这些键时，会把它自己的旧缺省原样传进来（2/5/3/60、积压 1/10），
# 和"用户确实想要 2/5"无法区分；所以下列值与"未配置"等价，一律回落到上面的观战口径。
# 真要 2 并发/5 秒，得让服务端传一个非旧缺省的值（或改 service.py 在不配置时不传）。
SERVICE_JOIN_CONCURRENT_FALLBACK = 2
SERVICE_JOIN_INTERVAL_FALLBACK = 5.0
SERVICE_PREEXISTING_MIN_FALLBACK = 1.0
SERVICE_PREEXISTING_MAX_FALLBACK = 10.0


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


METRICS_INTERVAL = 60.0
SUMMARY_INTERVAL = 3600.0
METRICS_MAX_BYTES = 5 * 1024 * 1024


def _append_log_jsonl(name: str, record: dict) -> None:
    """往 data/logs/<name> 追加一行 JSON；单文件超 5MB 时轮转成 .1（失败不影响观战）。"""
    try:
        path = Path(DATA_DIR) / "logs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size >= METRICS_MAX_BYTES:
            try:
                path.replace(path.with_suffix(path.suffix + ".1"))
            except OSError:
                pass
        record = dict(record, at=time.time(), t=time.strftime("%H:%M:%S"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def watch_event(name: str, **fields) -> None:
    """结构化事件：startup/pause/breaker/shutdown 等，写 data/logs/watch-events.jsonl。"""
    _append_log_jsonl("watch-events.jsonl", dict(event=name, **fields))


def write_metrics(record: dict) -> None:
    """每分钟一条运行指标，写 data/logs/watch-metrics.jsonl（用于离线复盘）。"""
    _append_log_jsonl("watch-metrics.jsonl", record)


def write_join_diag(record: dict) -> None:
    """每次进场结束写一行诊断到 data/logs/join-diagnostics.jsonl（失败不影响观战）。"""
    try:
        path = Path(DATA_DIR) / "logs" / "join-diagnostics.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(record, at=time.time())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_system_nickname() -> str:
    path = YGOPRO_DIR / "system.conf"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("nickname"):
            _, _, value = line.partition("=")
            return value.strip()
    return ""


def load_system_host() -> tuple[str, int]:
    host, port = "tiramisu.moenext.com", 8911
    path = YGOPRO_DIR / "system.conf"
    if not path.is_file():
        return host, port
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "lasthost" and value:
            host = value
        elif key == "lastport" and value.isdigit():
            port = int(value)
    return host, port


def default_config() -> dict:
    host, port = load_system_host()
    nickname = load_system_nickname() or "Spectator"
    return {
        "_comment": "watch_names：只要该玩家在对局中就观战。watch_vs_top：仅当该玩家的对手竞技排名在前 top 名时才观战，例如 {\"name\": \"玩家\", \"top\": 100}。同一名字若已写在 watch_names，会忽略排名条件。萌卡服请填写 account 和 password。",
        "host": host,
        "port": port,
        "version": "0x1362",
        "nickname": nickname,
        "account": "",
        "password": "",
        "mycard_secret": None,
        "join_mode": "auto",
        "watch_names": [],
        "watch_vs_top": [],
        "name_match": "exact",
        "watch_waiting_rooms": True,
        "direct_rooms": [],
        "room_list_urls": [],
        "websocket_urls": [],
        "room_list_username": "",
        "room_list_password": "",
        "replay_dir": str(DATA_DIR / "replays"),
        "deck_dir": str(DATA_DIR / "decks"),
        "cleanup_files": True,
        "ygopro_dir": str(YGOPRO_DIR),
        "data_dir": str(DATA_DIR),
        "poll_seconds": 5,
        "max_concurrent": 2,
        "join_after_min_seconds": 120,
        "join_after_max_seconds": 240,
    }


def load_config() -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.is_file():
        CONFIG_PATH.write_text(
            json.dumps(default_config(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log(f"已生成配置 {CONFIG_PATH}")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    data = {key: value for key, value in data.items() if not str(key).startswith("_")}
    base = default_config()
    base.update(data)
    return base


def parse_version(value) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def frame(proto: int, body: bytes = b"") -> bytes:
    if len(body) + 1 > 0xFFFF:
        raise ValueError("数据包过长")
    return struct.pack("<H", len(body) + 1) + bytes((proto,)) + body


def utf16_fixed(text: str, chars: int = 20) -> bytes:
    raw = text.encode("utf-16le")[: chars * 2]
    return raw + b"\x00" * (chars * 2 - len(raw))


def decode_utf16z(raw: bytes) -> str:
    if len(raw) % 2:
        raw = raw[:-1]
    return raw.decode("utf-16le", "replace").split("\x00", 1)[0]


def player_info_packet(name: str) -> bytes:
    return frame(CTOS_PLAYER_INFO, utf16_fixed(name, 20))


def join_packet(version: int, password: str, gameid: int = 0) -> bytes:
    body = struct.pack("<HHI", version & 0xFFFF, 0, gameid & 0xFFFFFFFF) + utf16_fixed(password, 20)
    return frame(CTOS_JOIN_GAME, body)


def encode_mycard_password(secret_id: int, action: int, suffix: str, opt0: int = 0) -> str:
    """生成萌卡房间口令：6 字节动作块按用户密钥异或后做 base64，再拼房间 id 或标题。"""
    block = bytearray(6)
    block[1] = ((action & 0xF) << 4) | (opt0 & 0xF)
    checksum = sum(block[1:]) & 0xFF
    block[0] = (-checksum) & 0xFF
    key = (int(secret_id) % 65535) + 1
    encrypted = bytearray(6)
    for offset in (0, 2, 4):
        value = int.from_bytes(block[offset : offset + 2], "little") ^ key
        encrypted[offset : offset + 2] = value.to_bytes(2, "little")
    token = base64.b64encode(encrypted).decode("ascii")
    return token + suffix


def decode_mycard_block(secret_id: int, token: str) -> bytes:
    encrypted = base64.b64decode(token)
    key = (int(secret_id) % 65535) + 1
    block = bytearray(6)
    for offset in (0, 2, 4):
        value = int.from_bytes(encrypted[offset : offset + 2], "little") ^ key
        block[offset : offset + 2] = value.to_bytes(2, "little")
    return bytes(block)


def name_matches(player: str, targets: Iterable[str], mode: str) -> bool:
    player = player.strip()
    if not player:
        return False
    for target in targets:
        target = target.strip()
        if not target:
            continue
        if mode == "contains":
            if target.casefold() in player.casefold() or player.casefold() in target.casefold():
                return True
        elif player == target:
            return True
    return False


def safe_filename_part(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", text).strip().rstrip(".")
    return cleaned[:40] or "unknown"


def parse_error(body: bytes) -> tuple[int, int]:
    msg = body[0] if body else 0
    code = struct.unpack_from("<I", body, 4)[0] if len(body) >= 8 else 0
    return msg, code


def parse_chat(body: bytes) -> tuple[int, str]:
    """前两个字节是发送者座位。0 到 3 是决斗者，其余多半是系统消息，名字已经写在正文里。"""
    if len(body) < 2:
        return -1, ""
    player = struct.unpack_from("<H", body, 0)[0]
    return player, decode_utf16z(body[2:])


def format_room_chat(player: int, text: str, seats: dict[int, str]) -> str:
    if 0 <= player <= 3:
        name = seats.get(player) or f"座位{player + 1}"
        return f"{name}: {text}"
    return text


def is_dealer_chat(text: str) -> bool:
    """发牌姬广播、观战进出和服务器状态，不写进终端。"""
    head = text.lstrip()
    if head.startswith("发牌姬:") or head.startswith("发牌姬："):
        return True
    if "加入了观战" in text or "退出了观战" in text or "正在读取用户信息" in text:
        return True
    compact = "".join(text.split())
    return compact in {"[Server]:观战中", "[Server]：观战中"}


def parse_player_enter(body: bytes) -> tuple[str, int]:
    name = decode_utf16z(body[:40])
    pos = body[40] if len(body) > 40 else -1
    return name, pos


def replay_files_from_payload(payload: bytes) -> list[bytes]:
    """STOC_REPLAY 正文就是 .yrp 文件。一个包里偶尔会粘上多份录像。"""
    files: list[bytes] = []
    buf = payload
    while len(buf) >= 32:
        magic = struct.unpack_from("<I", buf, 0)[0]
        if magic not in REPLAY_MAGICS:
            break
        datasize = struct.unpack_from("<I", buf, 16)[0]
        if datasize > 8_000_000:
            break
        candidates = []
        if magic == REPLAY_MAGIC_YRP2:
            candidates.append(80 + datasize)
        candidates.append(32 + datasize)
        taken = None
        for size in candidates:
            if size <= len(buf) and size >= 32:
                taken = size
                break
        if taken is None:
            files.append(buf)
            break
        files.append(buf[:taken])
        buf = buf[taken:]
    return files


def decompress_replay(blob: bytes) -> Optional[bytes]:
    """yrp2 的 datasize 是解压后的长度，压缩数据跟在 80 字节头后面。"""
    if len(blob) < 32:
        return None
    magic, _version, flag, _seed, datasize = struct.unpack_from("<IIIII", blob, 0)
    if magic not in REPLAY_MAGICS or datasize <= 0 or datasize > 0x80000:
        return None
    header_size = 80 if magic == REPLAY_MAGIC_YRP2 else 32
    if len(blob) < header_size:
        return None
    if not (flag & 0x1):
        return blob[header_size:header_size + datasize]
    props = blob[24:29]
    if len(props) < 5 or props[0] == 0:
        return None
    dictionary = struct.unpack_from("<I", props, 1)[0]
    if dictionary < 4096:
        return None
    encoded = props[0]
    pb = encoded // 45
    rest = encoded % 45
    lp = rest // 9
    lc = rest % 9
    try:
        decompressor = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[{"id": lzma.FILTER_LZMA1, "dict_size": dictionary, "lc": lc, "lp": lp, "pb": pb}],
        )
        data = decompressor.decompress(blob[header_size:])
    except (lzma.LZMAError, ValueError, OSError):
        return None
    if len(data) != datasize:
        return None
    return data


def decks_from_replay(blob: bytes) -> list[tuple[str, list[int], list[int]]]:
    """读出录像里双方本局实际使用的主卡组和额外卡组。"""
    if len(blob) < 32:
        return []
    flag = struct.unpack_from("<I", blob, 8)[0]
    if flag & 0x8:
        return []
    data = decompress_replay(blob)
    if not data:
        return []
    player_count = 4 if flag & 0x2 else 2
    need = player_count * 40 + 16
    if len(data) < need:
        return []
    names: list[str] = []
    for index in range(player_count):
        names.append(decode_utf16z(data[index * 40:(index + 1) * 40]))
    pos = player_count * 40 + 16
    decks: list[tuple[str, list[int], list[int]]] = []
    for name in names:
        if pos + 4 > len(data):
            return []
        main_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if main_count > 80 or pos + main_count * 4 > len(data):
            return []
        main = list(struct.unpack_from(f"<{main_count}I", data, pos)) if main_count else []
        pos += main_count * 4
        if pos + 4 > len(data):
            return []
        extra_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if extra_count > 15 or pos + extra_count * 4 > len(data):
            return []
        extra = list(struct.unpack_from(f"<{extra_count}I", data, pos)) if extra_count else []
        pos += extra_count * 4
        decks.append((name, list(reversed(main)), list(reversed(extra))))
    return decks


def sided_decks(
    games: list[tuple[int, list[tuple[str, list[int], list[int]]]]],
) -> dict[str, tuple[list[int], list[int], list[int], int, int]]:
    """第一局的主卡和额外原样保留。后几局多出来的张数记为 side。"""
    by_player: dict[str, list[tuple[int, list[int], list[int]]]] = {}
    for game_no, decks in games:
        for name, main, extra in decks:
            if name:
                by_player.setdefault(name, []).append((game_no, main, extra))
    result: dict[str, tuple[list[int], list[int], list[int], int, int]] = {}
    for name, entries in by_player.items():
        entries.sort(key=lambda item: item[0])
        base_no, base_main, base_extra = entries[0]
        base_counts = Counter(base_main) + Counter(base_extra)
        side_counts: dict[int, int] = {}
        for _game_no, main, extra in entries[1:]:
            seen = Counter(main) + Counter(extra)
            for code, count in seen.items():
                added = count - base_counts[code]
                if added > side_counts.get(code, 0):
                    side_counts[code] = added
        side = [code for code, count in side_counts.items() for _ in range(count)]
        result[name] = (base_main, base_extra, side, base_no, len(entries))
    return result


def render_ydk(main: Iterable[int], extra: Iterable[int], side: Iterable[int]) -> str:
    lines = ["#created by spectator", "#main"]
    lines.extend(str(code) for code in main)
    lines.append("#extra")
    lines.extend(str(code) for code in extra)
    lines.append("!side")
    lines.extend(str(code) for code in side)
    return "\n".join(lines) + "\n"


_CARD_NAMES: Optional[dict[int, str]] = None
_NAME_SPLIT = re.compile(r"[-－·・•\s/／]+")
_YGOCDB_MD5 = "https://ygocdb.com/api/v0/cards.zip.md5"
_YGOCDB_ZIP = "https://ygocdb.com/api/v0/cards.zip"


_FIELD_MIN = 8
# 光与暗的仪式、混沌之魔王-骸骨恶魔、黑色混沌之魔术师 黑混沌
_LIGHT_DARK_CARDS = frozenset({33599853, 24088928, 44001993})


def guess_deck_title(card_names: list[str]) -> str:
    """每个覆盖至少 8 张卡的卡名片段都算一个字段。同一段文字只保留更长的写法。"""
    names = [name.strip() for name in card_names if name and name.strip()]
    if len(names) < _FIELD_MIN:
        return ""
    candidates: set[str] = set()
    for name in names:
        parts = [part for part in _NAME_SPLIT.split(name) if len(part) >= 2]
        for part in parts:
            for length in range(2, min(len(part), 8) + 1):
                candidates.add(part[:length])
    ranked: list[tuple[int, str]] = []
    for candidate in candidates:
        if candidate.endswith(("的", "之")):
            continue
        score = sum(1 for name in names if candidate in name)
        if score >= _FIELD_MIN:
            ranked.append((score, candidate))
    if not ranked:
        return ""
    parent = {text: text for _, text in ranked}

    def find(text: str) -> str:
        while parent[text] != text:
            parent[text] = parent[parent[text]]
            text = parent[text]
        return text

    texts = [text for _, text in ranked]
    for index, left in enumerate(texts):
        for right in texts[index + 1 :]:
            if left in right or right in left:
                parent[find(right)] = find(left)
    groups: dict[str, list[tuple[int, str]]] = {}
    for score, text in ranked:
        groups.setdefault(find(text), []).append((score, text))
    labels: list[tuple[int, str]] = []
    for group in groups.values():
        group.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        best_score, best = group[0]
        floor = best_score - 3
        longer = [
            text
            for score, text in group
            if text.startswith(best) and score >= floor and score * 4 >= best_score * 3
        ]
        longer.sort(key=len, reverse=True)
        labels.append((best_score, longer[0]))
    labels.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
    return "·".join(text for _, text in labels)


def _names_from_cards_payload(payload: object) -> dict[int, str]:
    found: dict[int, str] = {}
    if not isinstance(payload, dict):
        return found
    for item in payload.values():
        if not isinstance(item, dict):
            continue
        try:
            card_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        text = item.get("cn_name") or item.get("sc_name") or item.get("md_name") or ""
        if text:
            found[card_id] = str(text)
    return found


def card_name_map() -> dict[int, str]:
    """百鸽全卡表。只在 cards.json 的 MD5 变化时重新下载。"""
    global _CARD_NAMES
    if _CARD_NAMES is not None:
        return _CARD_NAMES
    cache = CACHE_DIR
    cache.mkdir(exist_ok=True)
    json_path = cache / "cards.json"
    remote_md5 = ""
    try:
        with urllib.request.urlopen(_YGOCDB_MD5, timeout=15) as response:
            found = re.search(r"[0-9a-fA-F]{32}", response.read().decode("utf-8", "replace"))
            remote_md5 = found.group(0).lower() if found else ""
    except (urllib.error.URLError, TimeoutError, OSError):
        remote_md5 = ""
    fresh = json_path.is_file() and (
        not remote_md5 or hashlib.md5(json_path.read_bytes()).hexdigest() == remote_md5
    )
    if not fresh:
        try:
            with urllib.request.urlopen(_YGOCDB_ZIP, timeout=60) as response:
                blob = response.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                member = next(name for name in archive.namelist() if name.endswith("cards.json"))
                raw = archive.read(member)
            digest = hashlib.md5(raw).hexdigest()
            if remote_md5 and digest != remote_md5:
                raise RuntimeError("百鸽卡表校验不一致")
            json_path.write_bytes(raw)
            log("已更新百鸽卡表")
        except (urllib.error.URLError, TimeoutError, OSError, zipfile.BadZipFile, KeyError, StopIteration, RuntimeError) as exc:
            if not json_path.is_file():
                log(f"暂时读不到百鸽卡表，这次不猜测卡组名：{exc}")
                _CARD_NAMES = {}
                return _CARD_NAMES
            log(f"百鸽卡表更新失败，沿用本地缓存：{exc}")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"百鸽卡表无法读取，这次不猜测卡组名：{exc}")
        _CARD_NAMES = {}
        return _CARD_NAMES
    _CARD_NAMES = _names_from_cards_payload(payload)
    return _CARD_NAMES


def with_light_dark_field(title: str, card_ids: Iterable[int]) -> str:
    """三张核心卡各至少一张时，补上「光与暗」。"""
    fields = [part for part in title.split("·") if part]
    if _LIGHT_DARK_CARDS <= set(card_ids) and "光与暗" not in fields:
        fields.append("光与暗")
    return "·".join(fields)


_REFERENCE_DECKS: Optional[list[dict]] = None
_SIMILARITY_THRESHOLD = 0.60

LIVE_LINKS: dict[str, bool] = {}


def reconnect_delay(attempt: int) -> int:
    """2/4/8/16/30 秒退避；attempt 从 1 开始。"""
    return min(30, 2 ** max(1, int(attempt)))


def reconnect_limit(saved: bool) -> int:
    """保存过录像允许 3 次断开重连；还没保存首份录像允许 5 次，之后转 unstable。"""
    return 3 if saved else 5


def room_unavailable(received_any: bool, saved: bool) -> bool:
    """连上后没收到任何对局数据就被关闭：房间已结束/不可观战，不能无限重进。"""
    return not received_any and not saved




def reference_decks() -> list[dict]:
    """天梯前100参考卡组（含标注卡组名）。文件缺失时返回空列表，走字段法兜底。"""
    global _REFERENCE_DECKS
    if _REFERENCE_DECKS is not None:
        return _REFERENCE_DECKS
    decks: list[dict] = []
    path = REFERENCE_DECKS_PATH
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            for item in payload.get("decks") or []:
                if not isinstance(item, dict) or not item.get("label"):
                    continue
                try:
                    decks.append(
                        {
                            "label": str(item["label"]),
                            "main": [int(code) for code in (item.get("main") or [])],
                            "extra": [int(code) for code in (item.get("extra") or [])],
                        }
                    )
                except (TypeError, ValueError):
                    continue
    _REFERENCE_DECKS = decks
    return decks


def deck_similarity(observed: Counter, reference: Counter) -> float:
    """主卡+额外多重集的 Dice 相似度，范围 0 到 1。等大卡组时约等于共享卡牌占比。"""
    common = sum((observed & reference).values())
    total = sum(observed.values()) + sum(reference.values())
    if total <= 0:
        return 0.0
    return 2.0 * common / total


def reference_deck_title(main: Iterable[int], extra: Iterable[int]) -> str:
    """先算与参考卡组的相似度，超过阈值就返回标注卡组名，否则返回空串。"""
    observed = Counter(main) + Counter(extra)
    if not observed:
        return ""
    best_label = ""
    best_score = 0.0
    for ref in reference_decks():
        score = deck_similarity(observed, Counter(ref["main"]) + Counter(ref["extra"]))
        if score > best_score:
            best_score = score
            best_label = ref["label"]
    if best_label and best_score > _SIMILARITY_THRESHOLD:
        return best_label
    return ""


def deck_title_for(main: Iterable[int], extra: Iterable[int], side: Iterable[int]) -> str:
    """优先按相似度匹配参考卡组，否则按卡名字段猜测。"""
    main = list(main)
    extra = list(extra)
    side = list(side)
    reference = reference_deck_title(main, extra)
    if reference:
        return reference
    ids = [*main, *extra, *side]
    names = card_name_map()
    titled = [names[card_id] for card_id in ids if card_id in names]
    return with_light_dark_field(guess_deck_title(titled), ids)


def watch_read_timeout(duel_ended: bool, received_any: bool) -> float:
    """对战已结束就短等录像。还没收到任何数据时更快重连，避免空等十几分钟。"""
    if duel_ended:
        return 25
    return 120 if received_any else 30


def replay_game_number(text: str) -> Optional[int]:
    if "录像" not in text:
        return None
    found = re.search(r"第(\d+)局", text)
    if not found:
        return None
    return int(found.group(1))


@dataclass
class RoomInfo:
    key: str
    title: str
    join_id: str
    players: list[str] = field(default_factory=list)
    started: bool = False
    seats: dict[int, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        names = " / ".join(self.players) if self.players else "无人"
        state = "对战中" if self.started else "等待中"
        title = self.title or self.join_id or self.key
        return f"{title} [{state}] {names}"


def _as_int(value, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def seats_from_user_list(users) -> dict[int, str]:
    """决斗者座位。观战位不记入，聊天时要用座位号找回是谁说的。"""
    seats: dict[int, str] = {}
    if not isinstance(users, list):
        return seats
    fallback = 0
    for user in users:
        if isinstance(user, str):
            while fallback in seats and fallback < 4:
                fallback += 1
            if fallback <= 3 and user:
                seats[fallback] = user
                fallback += 1
            continue
        if not isinstance(user, dict):
            continue
        pos = _as_int(user.get("pos", user.get("position", 0)), 0)
        if pos < 0 or pos > 3:
            continue
        name = user.get("name") or user.get("username") or ""
        if name:
            seats[pos] = str(name)
    return seats


def players_from_user_list(users) -> list[str]:
    seats = seats_from_user_list(users)
    return [seats[pos] for pos in sorted(seats)]


def room_from_mapping(item: dict, started: Optional[bool] = None) -> Optional[RoomInfo]:
    join_id = str(item.get("roomname") or item.get("id") or item.get("name") or "")
    title = str(item.get("title") or item.get("roomtitle") or join_id)
    if join_id in ("", "0") or title == "密码错误":
        return None
    options = item.get("options")
    if isinstance(options, dict) and options.get("no_watch") is True:
        return None  # 房主关了观战：进去只会被静默关闭，直接跳过
    seats = seats_from_user_list(item.get("users") or item.get("players") or [])
    for index, key in enumerate(("player1", "player2", "player3", "player4")):
        extra = item.get(key)
        if not isinstance(extra, str) or not extra or extra in seats.values():
            continue
        if index not in seats:
            seats[index] = extra
            continue
        free = next((pos for pos in range(4) if pos not in seats), None)
        if free is not None:
            seats[free] = extra
    players = [seats[pos] for pos in sorted(seats)]
    if started is None:
        istart = str(item.get("istart") or item.get("status") or "")
        started = bool(istart) and istart != "wait" and "wait" not in istart.lower()
    key = join_id or title
    return RoomInfo(key=key, title=title, join_id=join_id, players=players, started=bool(started), seats=seats)


def room_list_denied(payload) -> bool:
    rooms = payload.get("rooms") if isinstance(payload, dict) else None
    if not isinstance(rooms, list) or len(rooms) != 1 or not isinstance(rooms[0], dict):
        return False
    return str(rooms[0].get("roomname") or "") == "密码错误"


def joinable_rooms(rooms, preexisting, direct_keys, allow_preexisting=False):
    """只跟随后续新开局；启动时已在列表里的旧房间默认不加入（手动直连房间例外）。"""
    if allow_preexisting:
        return list(rooms)
    return [room for room in rooms if room.key not in preexisting or room.key in direct_keys]


def rooms_from_json(payload, started: Optional[bool] = None) -> list[RoomInfo]:
    if isinstance(payload, dict):
        if "rooms" in payload:
            payload = payload["rooms"]
        elif "data" in payload and isinstance(payload["data"], list):
            payload = payload["data"]
        elif "id" in payload or "roomname" in payload:
            payload = [payload]
        else:
            return []
    rooms: list[RoomInfo] = []
    if not isinstance(payload, list):
        return rooms
    for item in payload:
        if isinstance(item, dict):
            room = room_from_mapping(item, started)
            if room:
                rooms.append(room)
    return rooms


class PacketStream:
    def __init__(self, reader: asyncio.StreamReader):
        self.reader = reader
        self.buf = bytearray()

    async def read_packet(self) -> Optional[tuple[int, bytes]]:
        while True:
            if len(self.buf) >= 2:
                length = struct.unpack_from("<H", self.buf, 0)[0]
                if length < 1:
                    del self.buf[:2]
                    continue
                total = 2 + length
                if len(self.buf) >= total:
                    proto = self.buf[2]
                    body = bytes(self.buf[3:total])
                    del self.buf[:total]
                    return proto, body
            chunk = await self.reader.read(65536)
            if not chunk:
                return None
            self.buf += chunk


_rank_cache: dict[str, Optional[int]] = {}
_official_ranks: dict[str, int] = {}
_official_rank_updated_at: Optional[float] = None
_official_rank_stale = True
_watch_target_reasons: dict[str, str] = {}
_observed_room_ranks = {}


def set_watch_target_reasons(reasons):
    global _watch_target_reasons
    _watch_target_reasons=dict(reasons)


def selection_reason(room, mode='exact'):
    reasons=[]
    for player in room.players:
        for name,reason in _watch_target_reasons.items():
            if name_matches(player,[name],mode):reasons.append(f'{player}：{reason}')
    return '；'.join(dict.fromkeys(reasons)) or '匹配既定观战规则'


def set_official_rank_snapshot(members, updated_at, stale):
    global _official_ranks, _official_rank_updated_at, _official_rank_stale
    _official_ranks={m['player']:int(m['rank']) for m in members}
    _official_rank_updated_at=updated_at
    _official_rank_stale=bool(stale)


def in_night_window(ts=None, start=NIGHT_START_HOUR, end=NIGHT_END_HOUR) -> bool:
    """本地时间是否落在夜间降档窗口（支持跨午夜）。"""
    hour = time.localtime(ts if ts is not None else time.time()).tm_hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def config_number(config, key, default, cast):
    value = config.get(key)
    if value is None:
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def config_flag(config, key, default):
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def default_local_ip() -> str:
    """当前默认出口的本地 IP（不联网、只做 UDP connect），用于发现换网。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def room_rank_sum(room) -> int:
    """双方排名之和（越小越优先）；未知排名按很大的数排到最后。不联网。"""
    total = 0
    for player in (getattr(room, 'players', None) or []):
        rank = _official_ranks.get(player)
        if rank is None:
            observed = _observed_room_ranks.get(player)
            if observed and 0 <= time.time() - observed[1] < 120:
                rank = observed[0]
        if rank is None:
            rank = _rank_cache.get(player)
        total += int(rank) if isinstance(rank, int) and rank > 0 else 100000
    return total


def arena_rank_number(name: str) -> Optional[int]:
    """萌卡竞技排名。正数是名次，0 是未上榜，None 是这次没查到。"""
    if name in _official_ranks:
        return _official_ranks[name]
    observed=_observed_room_ranks.get(name)
    if observed and 0<=time.time()-observed[1]<120:return observed[0]
    if name in _rank_cache:
        return _rank_cache[name]
    if os.environ.get("YGOPRO_SKIP_ARENA_RANK") == "1":
        return None
    url = "https://sapi.moecube.com:444/ygopro/arena/user?username=" + urllib.parse.quote(name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return None
    try:
        number = int(payload.get("arena_rank", payload.get("rank")))
    except (TypeError, ValueError):
        return None
    _rank_cache[name] = number
    return number


def arena_rank_label(name: str) -> str:
    number = arena_rank_number(name)
    if number is None:
        return "未知"
    if number <= 0:
        return "未上榜"
    return str(number)


def room_label_with_ranks(room: RoomInfo) -> str:
    """房间标题，每位玩家后面带竞技排名。"""
    if room.players:
        def label(name):
            if name in _official_ranks:
                stale=_official_rank_stale or _official_rank_updated_at is None or time.time()-_official_rank_updated_at>600
                return f"第{_official_ranks[name]}名"+('，榜单过期' if stale else '')
            value=arena_rank_label(name)
            return '排名暂缺' if value=='未知' else value
        names = " / ".join(f"{name}（{label(name)}）" for name in room.players)
    else:
        names = "无人"
    state = "对战中" if room.started else "等待中"
    title = room.title or room.join_id or room.key
    return f"{title} [{state}] {names}"


class GameClock:
    """录像头里没有开局时间时，才用观战过程中看到的时刻。"""

    def __init__(self, first_start: Optional[datetime] = None):
        self.times: dict[int, datetime] = {}
        self.next_game = 1
        if first_start is not None:
            self.times[1] = first_start
            self.next_game = 2

    def on_duel_start(self, when: datetime) -> None:
        self.times.setdefault(self.next_game, when)
        self.next_game += 1

    def on_replay(self, game_no: int, fallback: datetime) -> datetime:
        if game_no + 1 > self.next_game:
            self.next_game = game_no + 1
        return self.times.get(game_no, fallback)


def replay_recorded_time(blob: bytes) -> Optional[datetime]:
    """YGOPro 客户端保存录像时用的时刻。带 UNIFORM 标记时取头里的 start_time，否则取 seed。"""
    if len(blob) < 24:
        return None
    magic, _version, flag, seed, _datasize, start_time = struct.unpack_from("<IIIIII", blob, 0)
    if magic not in REPLAY_MAGICS:
        return None
    stamp = start_time if flag & REPLAY_UNIFORM else seed
    if not 1_500_000_000 <= stamp <= 2_100_000_000:
        return None
    return datetime.fromtimestamp(stamp)


def replay_folders(players: list[str], targets: list[str], mode: str) -> list[str]:
    """对局里命中的观战对象，用作录像子文件夹名。没有命中时返回空列表，录像留在根目录。"""
    folders: list[str] = []
    for target in targets:
        if any(name_matches(player, [target], mode) for player in players if player and player != "[Server]"):
            folder = safe_filename_part(target)
            if folder not in folders:
                folders.append(folder)
    return folders


def replay_basename(players: list[str], when: Optional[datetime] = None,
                    prefix: Optional[int] = None, deck_names: Optional[dict[str, str]] = None) -> str:
    """[编号] 06-26 “玩家名”（排名）【卡组】vs“玩家名”（排名）【卡组】。时分来自录像头里的开局时间。"""
    stamp = (when or datetime.now()).strftime("%H-%M")
    duelists = [name for name in players if name and name != "[Server]"][:4]
    deck_names = deck_names or {}

    def one(name: str) -> str:
        part = f"“{safe_filename_part(name)}”（{arena_rank_label(name)}）"
        title = deck_names.get(name)
        if title:
            part += f"【{safe_filename_part(title)}】"
        return part

    if not duelists:
        middle = ""
    elif len(duelists) == 1:
        middle = one(duelists[0])
    elif len(duelists) == 2:
        middle = f"{one(duelists[0])}vs{one(duelists[1])}"
    elif len(duelists) == 4:
        left = f"{one(duelists[0])}&{one(duelists[2])}"
        right = f"{one(duelists[1])}&{one(duelists[3])}"
        middle = f"{left}vs{right}"
    else:
        middle = f"{one(duelists[0])}vs" + "&".join(one(name) for name in duelists[1:])
    head = f"{prefix} {stamp}" if prefix else stamp
    return f"{head} {middle}".strip()


def ranks_from_filename(filename: str) -> dict[str, Optional[int]]:
    """从录像文件名解析各玩家名次。返回 {玩家名: 名次}，0 是未上榜，None 是未知。"""
    result: dict[str, Optional[int]] = {}
    for name, label in re.findall(r'“([^”]+)”[（(]([^）)]*)[）)]', filename):
        name = name.strip()
        if not name:
            continue
        label = label.strip()
        if label.isdigit():
            result[name] = int(label)
        elif label == '未上榜':
            result[name] = 0
        else:
            result[name] = None
    return result


def match_number_from_filename(filename: str) -> Optional[int]:
    """新命名格式的前缀编号：'1 06-26 …' 里的 1，表示当天第几场对战。旧格式（'06-26 …'）返回 None。"""
    match = re.match(r'^(\d+)\s+\d{2}-\d{2}\s', filename)
    return int(match.group(1)) if match else None


class ReplayWriter:
    def __init__(self, directory: Path, targets: Optional[list[str]] = None, mode: str = "exact"):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.targets = list(targets or [])
        self.mode = mode
        self._hashes: set[str] = set()
        self.on_saved = None
        self.on_session = None
        self._day_matches: dict[tuple[str, str], int] = {}
        self._current_prefixes: dict[str, int] = {}
        self.last_written: list[Path] = []
        self._metrics = {'saved_games': 0}

    def for_room(self, players: list[str]) -> 'ReplayWriter':
        """固定此房间的归档范围；以后名单变化只影响新房间。"""
        targets = [target for target in self.targets
                   if replay_folders(players, [target], self.mode)]
        child = ReplayWriter(self.directory, targets, self.mode)
        child._hashes = self._hashes
        child._day_matches = self._day_matches
        child._metrics = self._metrics
        child.on_saved = self.on_saved
        child.on_session = self.on_session
        return child

    def start_match(self) -> None:
        """新的一整局对战开始：为每个目标玩家分配“当天第几场被捕捉”的前缀编号。"""
        date = datetime.now().strftime('%Y-%m-%d')
        self._current_prefixes = {}
        for target in self.targets:
            folder = safe_filename_part(target)
            key = (folder, date)
            count = self._day_matches.get(key, 0) + 1
            self._day_matches[key] = count
            self._current_prefixes[folder] = count

    def save(
        self,
        payload: bytes,
        players: list[str],
        whens: Optional[list[datetime]] = None,
        result_evidence: Optional[dict] = None,
    ) -> list[Path]:
        """每份录像返回一个路径。命中多名观战对象时，同一份内容会写入每个玩家的子文件夹。"""
        saved: list[Path] = []
        self.last_written = []
        files = replay_files_from_payload(payload)
        if not files and payload:
            files = [payload]
        for index, blob in enumerate(files):
            digest = hashlib.sha256(blob).hexdigest()
            if digest in self._hashes:
                log("收到重复录像，已跳过")
                continue
            self._hashes.add(digest)
            when = replay_recorded_time(blob)
            if when is None and whens and index < len(whens):
                when = whens[index]
            deck_names = {}
            for name, main, extra in decks_from_replay(blob):
                if name and name != "[Server]":
                    title = deck_title_for(main, extra, [])
                    if title:
                        deck_names[name] = title
            folders = replay_folders(players, self.targets, self.mode) or [""]
            written: list[Path] = []
            for folder in folders:
                prefix = self._current_prefixes.get(folder)
                path = self._unique_path(players, when, folder, prefix, deck_names)
                path.write_bytes(blob)
                if result_evidence and len(files) == 1:
                    # 胜者编号与录像内核编号一致，不能用房间席位顺序替代录像顺序。
                    names = [name for name, _, _ in decks_from_replay(blob)]
                    if len(names) == 2:
                        evidence = dict(result_evidence, digest=digest, players=names)
                        metadata = Path(str(path) + '.evidence.json')
                        try:
                            metadata.write_text(json.dumps(evidence, ensure_ascii=False), encoding='utf-8')
                        except OSError as exc:
                            log(f'小局胜负留存失败：{exc}')
                written.append(path)
            self.last_written.extend(written)
            magic = struct.unpack_from("<I", blob, 0)[0] if len(blob) >= 4 else 0
            kind = {REPLAY_MAGIC_YRP1: "yrp1", REPLAY_MAGIC_YRP2: "yrp2"}.get(magic, "未知格式")
            shown = "、".join(str(path.relative_to(self.directory)) for path in written)
            log(f"已保存录像 {shown}（{len(blob)} 字节，{kind}）")
            if self.on_saved:
                try:
                    self.on_saved(list(written), list(players), when, digest)
                except Exception as exc:
                    log(f"录像写入后的索引失败：{exc}")
            saved.append(written[0])
            if any(path.is_file() for path in written):
                self._metrics['saved_games'] += 1
        return saved

    def _unique_path(self, players: list[str], when: Optional[datetime] = None, folder: str = "",
                     prefix: Optional[int] = None, deck_names: Optional[dict[str, str]] = None) -> Path:
        directory = self.directory / folder if folder else self.directory
        directory.mkdir(parents=True, exist_ok=True)
        base = replay_basename(players, when, prefix, deck_names)
        path = directory / f"{base}.yrp"
        index = 2
        while path.exists():
            path = directory / f"{base} ({index}).yrp"
            index += 1
        return path


class RoomBoard:
    """合并 HTTP 轮询和 WebSocket 推送得到的房间。"""

    def __init__(self):
        self._http: dict[str, RoomInfo] = {}
        self._ws: dict[str, dict[str, RoomInfo]] = {}
        self._lock = asyncio.Lock()
        self._started_at: dict[str, float] = {}
        self._preexisting: set[str] = set()

    async def replace_http(self, rooms: list[RoomInfo]) -> None:
        async with self._lock:
            self._http = {room.key: room for room in rooms}

    async def apply_ws(self, url: str, event: str, data) -> None:
        async with self._lock:
            bucket = self._ws.setdefault(url, {})
            started = "started" in url
            if event == "init":
                bucket.clear()
                for room in rooms_from_json(data, started):
                    bucket[room.key] = room
                    if room.started and room.key not in self._started_at:
                        self._preexisting.add(room.key)
            elif event in ("create", "update") and isinstance(data, dict):
                room = room_from_mapping(data, started)
                if room:
                    bucket[room.key] = room
                    if event == "create" and room.started:
                        self._preexisting.discard(room.key)
                    if (
                        event == "create"
                        and room.started
                        and room.key not in self._started_at
                    ):
                        self._started_at[room.key] = time.time()
            elif event == "delete":
                key = str(data)
                bucket.pop(key, None)
                self._started_at.pop(key, None)
                self._preexisting.discard(key)

    async def snapshot(self) -> list[RoomInfo]:
        async with self._lock:
            merged: dict[str, RoomInfo] = {}
            for bucket in self._ws.values():
                merged.update(bucket)
            merged.update(self._http)
            return list(merged.values())

    async def timing(self) -> tuple[dict[str, float], set[str]]:
        async with self._lock:
            return dict(self._started_at), set(self._preexisting)

    async def note_fresh_start(self, key: str) -> float:
        async with self._lock:
            self._preexisting.discard(key)
            self._started_at.setdefault(key, time.time())
            return self._started_at[key]


def load_session() -> dict:
    if not SESSION_PATH.is_file():
        return {}
    try:
        return json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_session(session: dict) -> None:
    SESSION_PATH.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_json(url: str, data: Optional[bytes] = None, headers: Optional[dict] = None) -> tuple[dict, int]:
    request = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "Mozilla/5.0"})
    last_error: Optional[BaseException] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else {}, response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            return payload if isinstance(payload, dict) else {}, exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5)
    raise RuntimeError(f"网络暂时中断：{last_error}")


def fetch_u16_secret(token: str) -> int:
    payload, status = http_json(
        ACCOUNTS_API + "authUser",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
    )
    if status == 401:
        raise PermissionError("登录已过期")
    if status != 200 or payload.get("u16Secret") is None:
        raise RuntimeError(f"刷新观战凭证失败（HTTP {status}）")
    return int(payload["u16Secret"])


def sign_in(account: str, password: str) -> tuple[int, str]:
    form = urllib.parse.urlencode({"account": account, "password": password}).encode()
    payload, status = http_json(
        ACCOUNTS_API + "signin",
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",
        },
    )
    if status == 400:
        raise RuntimeError("萌卡账号或密码不正确")
    token = payload.get("token")
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    if status != 200 or not token:
        raise RuntimeError(f"登录失败（HTTP {status}）")
    secret = fetch_u16_secret(token)
    username = str(user.get("username") or account)
    save_session(
        {
            "token": token,
            "username": username,
            "u16_secret": secret,
            "fetched_at": time.time(),
        }
    )
    return secret, username


_SECRET_STATE = {
    "ok_at": 0.0, "source": "", "reused": False,
    "error": "", "error_at": 0.0, "failures": 0, "refreshes": 0,
}


def secret_status() -> dict:
    """给 capture-status 用的凭据状态：年龄/来源/是否沿用旧值/最近错误。"""
    age = round(time.time() - _SECRET_STATE["ok_at"], 1) if _SECRET_STATE["ok_at"] else None
    return {
        "mycard_secret_age": age,
        "mycard_secret_source": _SECRET_STATE["source"],
        "mycard_secret_reused": bool(_SECRET_STATE["reused"]),
        "mycard_secret_error": _SECRET_STATE["error"],
        "mycard_secret_error_at": _SECRET_STATE["error_at"] or None,
        "mycard_secret_failures": _SECRET_STATE["failures"],
        "mycard_secret_refreshes": _SECRET_STATE["refreshes"],
    }


def _secret_ok(source: str) -> None:
    previous = _SECRET_STATE["error"]
    _SECRET_STATE.update(ok_at=time.time(), source=source, reused=False, error="", error_at=0.0)
    _SECRET_STATE["refreshes"] += 1
    if previous:
        log(f"萌卡凭据刷新恢复（来源 {source}；此前错误：{previous}）")


def _secret_failed(exc: BaseException, reused: bool, source: str = "") -> None:
    _SECRET_STATE.update(error=f"{type(exc).__name__}: {exc}", error_at=time.time(),
                         reused=reused, source=source)
    _SECRET_STATE["failures"] += 1


def refresh_secret(config: dict) -> None:
    """登录萌卡并刷新 u16Secret。凭证大约每 4 分钟轮换，加入房间前都要重新取。"""
    if config.get("join_mode") == "plain":
        return
    account = str(config.get("account") or "").strip()
    session = load_session()
    # config 是账号的唯一来源；session 里是上次登录的显示名，对不上就丢弃旧会话。
    if account and session.get("username") and str(session["username"]).strip() != account:
        log(f"萌卡账号已改为 {account}，丢弃旧会话 {session['username']}")
        session = {}
        try:
            SESSION_PATH.unlink()
        except OSError:
            pass
    token = session.get("token")
    if token:
        try:
            secret = fetch_u16_secret(token)
            session["u16_secret"] = secret
            session["fetched_at"] = time.time()
            save_session(session)
            config["mycard_secret"] = secret
            if session.get("username"):
                config["nickname"] = session["username"]
            _secret_ok("token")
            return
        except PermissionError:
            log("萌卡登录已过期，正在重新登录")
        except RuntimeError as exc:
            if session.get("u16_secret") not in (None, "", 0):
                log(f"{exc}，暂时沿用上一次凭证")
                config["mycard_secret"] = session["u16_secret"]
                if session.get("username"):
                    config["nickname"] = session["username"]
                _secret_failed(exc, True, "reused")
                return
            raise
    account = str(config.get("account") or "").strip()
    password = str(config.get("password") or "")
    if account and password:
        secret, username = sign_in(account, password)
        config["mycard_secret"] = secret
        config["nickname"] = username
        log(f"已登录萌卡账号 {username}")
        _secret_ok("sign_in")
        return
    if config.get("mycard_secret") not in (None, "", 0):
        _secret_ok("config")
        return
    raise RuntimeError("请在 data/watch-config.json 填写萌卡 account 和 password")


_SECRET_LOCK: Optional[asyncio.Lock] = None
_SECRET_REFRESHED_AT = 0.0
_SECRET_REFRESH_INTERVAL = 60.0


def invalidate_secret_cache() -> None:
    """下一次进场前必须重新取 u16Secret。

    服务器用 users/<name>.json 里的 u16Secret / u16SecretPrevious 解密我们 8 位口令块；
    解不开或取用户信息超时时不会下发任何对局数据，直接关连接（客户端只能看到 0 字节 + 连接被重置）。
    所以"连上即被关"要怀疑手里口令过旧，下次进场前强制刷新，而不是继续用 60 秒缓存。
    """
    global _SECRET_REFRESHED_AT
    _SECRET_REFRESHED_AT = 0.0


async def refresh_secret_shared(config: dict) -> None:
    """并发观战共用一次 u16Secret 刷新，避免多任务同时轮换互相失效。"""
    global _SECRET_LOCK, _SECRET_REFRESHED_AT
    if _SECRET_LOCK is None:
        _SECRET_LOCK = asyncio.Lock()
    async with _SECRET_LOCK:
        if time.time() - _SECRET_REFRESHED_AT < _SECRET_REFRESH_INTERVAL and config.get("mycard_secret"):
            return
        try:
            await asyncio.to_thread(refresh_secret, config)
        except Exception as exc:
            _secret_failed(exc, True, "failed")
            if config.get("mycard_secret") not in (None, "", 0):
                log(f"萌卡凭据刷新失败（{type(exc).__name__}: {exc}），沿用旧口令；"
                    "若随后出现 0 包关闭，先查这里")
                _SECRET_REFRESHED_AT = time.time()
                return
            raise
        _SECRET_REFRESHED_AT = time.time()


def build_join_password(config: dict, room: RoomInfo) -> str:
    mode = config.get("join_mode") or "auto"
    secret = config.get("mycard_secret")
    use_mycard = mode == "mycard" or (mode == "auto" and secret not in (None, "", 0))
    if not use_mycard:
        password = room.join_id or room.title
        if len(password) > 20:
            raise RuntimeError(f"房间名超过 20 字，无法加入：{password}")
        return password
    if secret in (None, "", 0):
        raise RuntimeError("这个服务器使用萌卡房间口令，请在 config.json 填写 mycard_secret（u16Secret）")
    suffix = room.join_id or room.title
    action = 3 if room.join_id else 5
    password = encode_mycard_password(int(secret), action, suffix)
    if len(password) > 20:
        raise RuntimeError(f"房间标识过长，无法放入 20 字的加入口令：{suffix}")
    return password


@dataclass
class RankRule:
    name: str
    top: int


def collect_watch_rules(config: dict, cli_names: list[str]) -> tuple[list[str], list[RankRule]]:
    always: list[str] = []
    for name in cli_names or list(config.get("watch_any") or []) + list(config.get("watch_names") or []):
        text = str(name).strip()
        if text and text not in always:
            always.append(text)
    rules: list[RankRule] = []
    for item in config.get("watch_vs_top") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        try:
            top = int(item.get("top") or 0)
        except (TypeError, ValueError):
            top = 0
        if name and top > 0:
            rules.append(RankRule(name, top))
    return always, rules


def watch_targets(names: list[str], rank_rules: list[RankRule]) -> list[str]:
    """录像子文件夹用的观战名单。无条件观战在前，排名条件里还没出现的名字补在后面。"""
    targets: list[str] = []
    for name in names:
        text = name.strip()
        if text and text not in targets:
            targets.append(text)
    for rule in rank_rules:
        if rule.name not in targets:
            targets.append(rule.name)
    return targets


def plan_join(
    now: float,
    started_at: Optional[float],
    scheduled: Optional[float],
    min_seconds: float,
    max_seconds: float,
    roll: float,
) -> tuple[str, Optional[float]]:
    """看到开局就按随机延迟进入；不知道开局时刻时立刻进入。roll 取 0 到 1。"""
    if started_at is None:
        return "join", now
    low = min(min_seconds, max_seconds)
    high = max(min_seconds, max_seconds)
    if scheduled is None:
        scheduled = started_at + low + (high - low) * min(max(roll, 0.0), 1.0)
    if now >= scheduled:
        return "join", scheduled
    return "wait", scheduled


def rank_is_within(rank: Optional[int], top: int) -> bool:
    """排名未知、查不到或未上榜都不算进入前 top 名。"""
    return rank is not None and rank > 0 and rank <= top


def select_rooms(
    rooms: list[RoomInfo],
    names: list[str],
    mode: str,
    watch_waiting: bool,
    rank_rules: Optional[list[RankRule]] = None,
) -> tuple[list[RoomInfo], list[str]]:
    """无条件名单直接观战。排名条件要逐条检查，不能因为第一条不满足就丢掉后面的订阅。"""
    chosen: list[RoomInfo] = []
    notes: list[str] = []
    rules = rank_rules or []
    for room in rooms:
        if not watch_waiting and not room.started:
            continue
        if any(name_matches(player, names, mode) for player in room.players):
            chosen.append(room)
            continue
        matched = False
        failures: list[str] = []
        for rule in rules:
            watched = [player for player in room.players if name_matches(player, [rule.name], mode)]
            if not watched:
                continue
            opponents = [player for player in room.players if player not in watched]
            if not opponents:
                continue
            described: list[str] = []
            qualified: list[str] = []
            for opponent in opponents:
                rank = arena_rank_number(opponent)
                if rank is None:
                    described.append(f"{opponent}（未知）")
                elif rank <= 0:
                    described.append(f"{opponent}（未上榜）")
                else:
                    described.append(f"{opponent}（{rank}）")
                    if rank_is_within(rank, rule.top):
                        qualified.append(opponent)
            if qualified:
                matched = True
                break
            failures.append(
                f"{rule.name} 的对手不在前 {rule.top} 名：" + "、".join(described)
            )
        if matched:
            chosen.append(room)
            continue
        if failures:
            notes.append(f"跳过 {' / '.join(room.players)}；" + "；".join(failures))
    return chosen, notes


async def read_http_json(url: str, timeout: float = 8) -> object:
    def _fetch():
        request = urllib.request.Request(url, headers={"User-Agent": "ygopro-spectator"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    return await asyncio.to_thread(_fetch)


def with_query(url: str, username: str, password: str) -> str:
    if not username:
        return url
    joiner = "&" if "?" in url else "?"
    from urllib.parse import quote

    return f"{url}{joiner}username={quote(username)}&pass={quote(password)}"


def arm_tcp_socket(sock: socket.socket) -> None:
    """空闲时发探测包，避免路由器掐掉观战连接后要等很久才报超时。"""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 20000, 5000))
        except OSError:
            pass


class WebSocketClient:
    def __init__(self, url: str):
        self.url = url
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def connect(self, headers: Optional[dict] = None) -> None:
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise RuntimeError(f"无法识别的房间列表地址：{redact_url(self.url)}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        ssl_context = ssl.create_default_context() if parsed.scheme == "wss" else None
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context, server_hostname=host if ssl_context else None),
            timeout=8,
        )
        sock = self.writer.get_extra_info("socket")
        if isinstance(sock, socket.socket):
            arm_tcp_socket(sock)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        extra = ""
        for name, value in (headers or {}).items():
            if any(char in name or char in value for char in "\r\n"):
                raise RuntimeError("请求头无效")
            extra += f"{name}: {value}\r\n"
        request = request.replace("\r\n\r\n", "\r\n" + extra + "\r\n", 1)
        self.writer.write(request.encode("ascii", "strict"))
        await self.writer.drain()
        header = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), timeout=8)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(header.split(b"\r\n", 1)[0].decode("latin1", "replace"))
        response_headers = {}
        for line in header.split(b"\r\n")[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                response_headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest())
        if (response_headers.get(b"sec-websocket-accept") != expected
                or response_headers.get(b"upgrade", b"").lower() != b"websocket"
                or b"upgrade" not in [part.strip() for part in response_headers.get(b"connection", b"").lower().split(b",")]):
            raise RuntimeError("WebSocket 握手验证失败")

    async def send_text(self, text: str) -> None:
        await self._send(0x1, text.encode("utf-8"))

    async def recv_text(self) -> Optional[str]:
        fragments: list[bytes] = []
        opcode = None
        while True:
            header = await self._read_exact(2)
            if header is None:
                return None
            b1, b2 = header
            frame_opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F
            if length == 126:
                raw = await self._read_exact(2)
                length = struct.unpack(">H", raw)[0]
            elif length == 127:
                raw = await self._read_exact(8)
                length = struct.unpack(">Q", raw)[0]
            mask = await self._read_exact(4) if masked else b""
            payload = await self._read_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if frame_opcode == 0x8:
                return None
            if frame_opcode == 0x9:
                await self._send(0xA, payload)
                continue
            if frame_opcode == 0xA:
                continue
            if frame_opcode in (0x1, 0x2):
                opcode = frame_opcode
                fragments = [payload]
            elif frame_opcode == 0x0:
                fragments.append(payload)
            else:
                continue
            if b1 & 0x80:
                data = b"".join(fragments)
                if opcode == 0x1:
                    return data.decode("utf-8", "replace")
                return None

    async def _send(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        header = bytes((0x80 | opcode,))
        length = len(payload)
        if length < 126:
            header += bytes((0x80 | length,))
        elif length < 65536:
            header += bytes((0x80 | 126,)) + struct.pack(">H", length)
        else:
            header += bytes((0x80 | 127,)) + struct.pack(">Q", length)
        self.writer.write(header + mask + masked)
        await self.writer.drain()

    async def _read_exact(self, size: int) -> Optional[bytes]:
        try:
            return await self.reader.readexactly(size)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


class Spectator:
    def __init__(
        self,
        config: dict,
        room: RoomInfo,
        writer: ReplayWriter,
        known_start: Optional[float] = None,
    ):
        self.config = config
        self.room = room
        self.replay_writer = writer
        first_start = datetime.fromtimestamp(known_start) if known_start else None
        self.clock = GameClock(first_start)
        self.version = parse_version(config.get("version") or DEFAULT_VERSION)
        self.seats: dict[int, str] = dict(room.seats) or {
            index: name for index, name in enumerate(room.players)
        }
        self.players: list[str] = [self.seats[pos] for pos in sorted(self.seats)] or list(room.players)
        self.duel_started = room.started
        self.duel_ended = False
        self.room_started_at = known_start
        self.joined_at = time.time()
        self.packets_received = 0
        self.replays_saved = 0
        self.observed_games: list[tuple[int, list[tuple[str, list[int], list[int]]]]] = []
        self.observed_sources: list[tuple[int, str]] = []
        self.deck_session_id = secrets.token_hex(16)
        self.pending_game: Optional[int] = None
        self.game_serial = 1
        self._match_numbered = False
        self._game_result = None
        self._capture_anchor = False
        self._capture_game_no = 0
        self._completed_games: dict[int, Optional[dict]] = {}
        self._delivered_games: dict[int, dict] = {}
        self._live_events = []
        self._live_from_start = False
        self._allow_complete_start = not room.started
        self._previous_game = None
        self.is_host = False
        self.fixed_password: Optional[str] = None  # 远程端由服务器下发入房口令，免登录
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def _connect(self, host: str, port: int, nickname: str, password: str) -> bool:
        self._game_result = None  # 重连不能把上一次连接的结果套到另一份录像。
        self._capture_anchor = False
        self._capture_game_no = 0
        self._completed_games = {}
        self._delivered_games = {}
        self._live_events = []
        self._live_from_start = False
        self._previous_game = None
        self._allow_complete_start = not self.duel_started
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10
            )
        except (asyncio.TimeoutError, OSError) as exc:
            log(f"连接失败：{exc}")
            return False
        sock = self.writer.get_extra_info("socket")
        if isinstance(sock, socket.socket):
            arm_tcp_socket(sock)
        try:
            self.writer.write(player_info_packet(nickname))
            self.writer.write(join_packet(self.version, password))
            await self.writer.drain()
        except (asyncio.TimeoutError, OSError) as exc:
            log(f"连接中断：{exc}")
            await self.close()
            return False
        return True

    async def _watch(self) -> tuple[str, bool]:
        """返回 (结果, 是否因网络中断需要重进)。"""
        stream = PacketStream(self.reader)
        outcome = "lost"
        self.received_any = False
        try:
            while True:
                timeout = watch_read_timeout(self.duel_ended, self.received_any)
                try:
                    packet = await asyncio.wait_for(stream.read_packet(), timeout)
                except asyncio.TimeoutError:
                    if self.duel_ended:
                        outcome = "saved" if self.replays_saved else "noreplay"
                        break
                    self._drop_reason = TimeoutError("长时间没有收到数据")
                    return "retry", True
                except OSError as exc:
                    self._drop_reason = exc
                    if self.duel_ended and self.replays_saved:
                        return "saved", False
                    return "retry", True
                if packet is None:
                    if self.replays_saved or self.duel_ended:
                        outcome = "saved" if self.replays_saved else "noreplay"
                        break
                    self._drop_reason = ConnectionError("连接被关掉")
                    return "retry", True
                self.received_any = True
                self.packets_received += 1
                try:
                    result = self.handle(*packet)
                except OSError as exc:
                    self._drop_reason = exc
                    if self.duel_ended and self.replays_saved:
                        return "saved", False
                    return "retry", True
                if result == "version":
                    outcome = "version"
                    break
                if result == "leave":
                    outcome = "leave"
                    break
                if result == "saved":
                    outcome = "saved"
        finally:
            await self.close()
        return outcome, False

    async def run(self) -> str:
        host = self.config["host"]
        port = int(self.config["port"])
        shown = await asyncio.to_thread(room_label_with_ranks, self.room)
        log(f"连接 {host}:{port}，观战 {shown}")
        outcome = "lost"
        self._drop_reason: BaseException = ConnectionError("连接中断")
        connect_failures = 0
        drop_failures = 0
        while True:
            if self.fixed_password:
                # 远程观战端：服务器已经算好口令，本地不保存账号凭据。
                nickname = str(self.config.get("nickname") or "Spectator")
                password = self.fixed_password
            else:
                try:
                    await refresh_secret_shared(self.config)
                    # 登录/续期可能校正账号名；入房名称必须与本次凭证使用同一身份。
                    nickname = str(self.config.get("nickname") or "Spectator")
                    password = build_join_password(self.config, self.room)
                except RuntimeError as exc:
                    log(str(exc))
                    outcome = "config"
                    break
            if not await self._connect(host, port, nickname, password):
                connect_failures += 1
                if connect_failures >= 3:
                    outcome = "saved" if self.replays_saved else "retry"
                    break
                delay = reconnect_delay(connect_failures)
                log(f"正在重新连接（第{connect_failures}次，{delay}秒后）")
                await asyncio.sleep(delay)
                continue
            connect_failures = 0
            LIVE_LINKS[self.room.key] = True
            outcome, dropped = await self._watch()
            LIVE_LINKS[self.room.key] = False
            if not dropped:
                break
            if room_unavailable(self.received_any, bool(self.replays_saved)):
                log(f"连接后未收到任何对局数据即被关闭（{self._drop_reason}），判定该房间已结束或不可观战，停止重试")
                outcome = "gone"
                break
            drop_failures += 1
            limit = reconnect_limit(self.replays_saved)
            if drop_failures >= limit:
                log(f"连续断开 {drop_failures} 次（{self._drop_reason}），本局停止重连，120秒后再评估")
                outcome = "unstable"
                break
            delay = reconnect_delay(drop_failures)
            log(f"{self._drop_reason}，{delay}秒后重试（第{drop_failures}/{limit}次）")
            await asyncio.sleep(delay)
        if self.replays_saved:
            log(f"本次观战结束，保存了 {self.replays_saved} 个录像")
        elif outcome == "noreplay":
            log("对战已结束，但服务器没有下发录像。请确认服务器开启了录像回传。")
        self.write_observed_decks()
        return outcome

    def handle(self, proto: int, body: bytes) -> Optional[str]:
        if proto == STOC_GAME_MSG:
            expected = {5: 3, 40: 2, 50: 17, 60: 9, 61: 1, 62: 9, 63: 1, 64: 9, 65: 1, 70: 17}
            draw = bool(body and body[0] == 90 and len(body) >= 3 and body[1] in (0, 1)
                        and len(body) == 3 + 4 * body[2])
            if body and (draw or len(body) == expected.get(body[0])):
                if len(self._live_events) < 100000:
                    self._live_events.append(body.hex())
                else:
                    self._live_from_start = False
            # 官方服务器逐条转发MSG_WIN（包括认输、超时），正文恰为3字节。
            if len(body) == 3 and body[0] == 5 and body[1] in (0, 1, 2):
                self._game_result = {'winner': body[1], 'reason': body[2],
                                     'raw': body.hex(), 'received_at': time.time(),
                                     'source': 'STOC_GAME_MSG/MSG_WIN'}
                if self._capture_anchor and 0 < self._capture_game_no <= 20:
                    snapshot = dict(
                        self._game_result, events=list(self._live_events),
                        captured_from_start=self._live_from_start,
                        draw_capture_version=1,
                        game_no=self._capture_game_no,
                        association='STOC_DUEL_START/MSG_START + replay game number')
                    if self._capture_game_no in self._completed_games:
                        old = self._completed_games[self._capture_game_no]
                        if old is None or old['winner'] != body[1]:
                            self._completed_games[self._capture_game_no] = None
                    else:
                        self._completed_games[self._capture_game_no] = snapshot
                    self._sync_replay_evidence()
                log('已收到小局胜负，等待关联本局录像')
            elif len(body) == 19 and body[0] == 4:
                observer_start = body[1] in (0x10, 0x11)
                if not observer_start:
                    self._capture_anchor = False
                    self._capture_game_no = 0
                    self._completed_games = {}
                    self._delivered_games = {}
                if self._capture_anchor:
                    self._capture_game_no += 1
                self._game_result = None
                self._live_events = []
                # MyCard观战流从STOC_DUEL_START开始回传；仅MSG_START不能证明完整。
                self._live_from_start = self._capture_anchor and observer_start
                if self._capture_anchor and self._capture_game_no == 1:
                    log('已进入观战，收到小局开局信息')
                self._allow_complete_start = False
            return None
        if proto == STOC_CHAT:
            player, text = parse_chat(body)
            game_no = replay_game_number(text)
            if game_no is not None:
                self.pending_game = game_no
            if text and not is_dealer_chat(text):
                log(f"房间消息：{format_room_chat(player, text, self.seats)}")
            if "读取用户信息失败" in text or "invalid_password" in text:
                return None
            return None
        if proto == STOC_ERROR_MSG:
            msg, code = parse_error(body)
            if msg == 4 and code:
                self.version = code & 0xFFFF
                self.config["version"] = hex(self.version)
                log(f"版本不一致，改用服务器版本 {self.version:#06x} 后重试")
                return "version"
            if msg == 1 and code == 1:
                log("加入失败：房间口令不正确")
            elif msg == 1 and code == 2:
                log("加入失败：房间拒绝观战")
            else:
                log(f"服务器返回错误 msg={msg} code={code}")
            return "leave"
        if proto == STOC_TYPE_CHANGE and body:
            player_type = body[0] & 0x0F
            self.is_host = (body[0] & 0x10) != 0
            if player_type != NETPLAYER_OBSERVER:
                self.writer.write(frame(CTOS_HS_TOOBSERVER))
            if self.is_host and not self.room.players:
                log("这个房间是刚被创建的空房，立即退出，避免占住房间")
                return "leave"
            return None
        if proto == STOC_HS_PLAYER_ENTER:
            name, pos = parse_player_enter(body)
            if name and 0 <= pos <= 3:
                if self.seats.get(pos) != name:
                    log(f"决斗者进入：{name}")
                self.seats[pos] = name
                self.players = [self.seats[i] for i in sorted(self.seats)]
            return None
        if proto == STOC_DUEL_START:
            if not self._capture_anchor:
                self._capture_anchor = True
                self._capture_game_no = 0
                self._completed_games = {}
            self._game_result = None
            self._live_events = []
            self._live_from_start = False
            if not self._match_numbered:
                self.replay_writer.start_match()
                self._match_numbered = True
            self.duel_started = True
            self.duel_ended = False
            self.clock.on_duel_start(datetime.now())
            return None
        if proto == STOC_DUEL_END:
            self.duel_ended = True
            log("决斗结束，等待录像")
            return None
        if proto == STOC_REPLAY:
            if not self._match_numbered:
                self.replay_writer.start_match()
                self._match_numbered = True
            files = replay_files_from_payload(body)
            if not files and body:
                files = [body]
            game_no = self.pending_game if self.pending_game is not None else self.game_serial
            fallback = datetime.now()
            whens = [self.clock.on_replay(game_no + index, fallback) for index in range(len(files))]
            saved = self.replay_writer.save(body, self.players, whens)
            if self._capture_anchor and len(files) == 1 and 0 < game_no <= 20:
                names = [name for name, _, _ in decks_from_replay(files[0])]
                digest = hashlib.sha256(files[0]).hexdigest()
                if len(names) == 2:
                    old = self._delivered_games.get(game_no)
                    if old is not None and old['digest'] != digest:
                        self._completed_games[game_no] = None
                    else:
                        self._delivered_games[game_no] = dict(
                            digest=digest, players=names,
                            paths=list(self.replay_writer.last_written) or (old['paths'] if old else []))
                    self._sync_replay_evidence()
            # 录像可先于最后一条MSG_WIN到达；完整观战流只在下一次MSG_START重置。
            self._game_result = None
            self._allow_complete_start = True  # 收完上一小局，下一次开局可完整留存。
            self.replays_saved += len(saved)
            self.note_replay_payload(body)
            return None
        if proto == STOC_JOIN_GAME:
            return None
        return None

    def _sync_replay_evidence(self) -> None:
        """按已锚定的小局编号双向配对，兼容录像/胜负任一种到达顺序。"""
        for number, replay in self._delivered_games.items():
            captured = self._completed_games.get(number)
            previous = None
            older = self._delivered_games.get(number - 1)
            older_result = self._completed_games.get(number - 1)
            if older and older_result and older_result['winner'] in (0, 1):
                previous = dict(digest=older['digest'], game_no=number - 1,
                                loser=older['players'][1 - older_result['winner']])
            for path in replay['paths']:
                try:
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != replay['digest']:
                        continue
                    metadata = Path(str(path) + '.evidence.json')
                    if captured is None:
                        if metadata.exists():
                            metadata.unlink()
                            Path(str(path) + '.analysis.json').unlink(missing_ok=True)
                        continue
                    evidence = dict(captured, digest=replay['digest'], players=replay['players'],
                                    previous_game=previous, session_id=self.deck_session_id)
                    content = json.dumps(evidence, ensure_ascii=False)
                    if metadata.is_file() and metadata.read_text(encoding='utf-8') == content:
                        continue
                    temporary = Path(str(metadata) + '.tmp')
                    temporary.write_text(content, encoding='utf-8')
                    temporary.replace(metadata)
                    Path(str(path) + '.analysis.json').unlink(missing_ok=True)
                except OSError as exc:
                    log(f'小局胜负留存失败：{exc}')

    def note_replay_payload(self, payload: bytes) -> None:
        files = replay_files_from_payload(payload)
        if not files and payload:
            files = [payload]
        game_no = self.pending_game if self.pending_game is not None else self.game_serial
        self.pending_game = None
        recorded = 0
        for offset, blob in enumerate(files):
            digest = hashlib.sha256(blob).hexdigest()
            if any(old_digest == digest for _, old_digest in self.observed_sources):
                continue
            number = game_no + offset
            self.observed_sources.append((number, digest))
            decks = decks_from_replay(blob)
            if not decks:
                log("这场录像里没有读出卡组")
                continue
            self.observed_games.append((number, decks))
            recorded += 1
            names = " / ".join(name for name, _main, _extra in decks)
            log(f"第 {number} 局卡组已记录：{names}")
        self.game_serial = game_no + max(len(files), 1)
        if self.observed_sources and self.replay_writer.on_session:
            try:
                self.replay_writer.on_session(self.deck_session_id, list(self.observed_sources))
            except Exception as exc:
                log(f"记录整场录像索引失败：{exc}")

    def write_observed_decks(self) -> None:
        if not self.observed_games:
            return
        directory = Path(str(self.config.get("deck_dir") or (DATA_DIR / "decks")))
        directory.mkdir(parents=True, exist_ok=True)
        when = datetime.now()
        for name, (main, extra, side, base_no, game_count) in sided_decks(self.observed_games).items():
            title = deck_title_for(main, extra, side)
            base = f"{when.strftime('%H-%M')} {safe_filename_part(name)}"
            if title:
                base = f"{base} {safe_filename_part(title)}"
            path = directory / f"{base}.ydk"
            index = 2
            while path.exists():
                path = directory / f"{base} ({index}).ydk"
                index += 1
            path.write_text(render_ydk(main, extra, side), encoding="utf-8")
            if game_count == 1:
                note = "只收到 1 局，side 为空"
            elif base_no == 1:
                note = f"主卡取第 1 局，后 {game_count - 1} 局换入的记为 side"
            else:
                note = f"没有第 1 局，主卡改用第 {base_no} 局"
            called = title or "未能从卡名判断"
            log(f"已生成卡组 {path.name}（猜测 {called}，主卡 {len(main)}，额外 {len(extra)}，side {len(side)}，{note}）")

    async def close(self) -> None:
        writer = self.writer
        if not writer:
            return
        self.writer = None
        self.reader = None
        try:
            writer.write(frame(CTOS_LEAVE_GAME))
            await asyncio.wait_for(writer.drain(), timeout=2)
        except Exception:
            pass
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        except Exception:
            pass


def redact_url(url: str) -> str:
    return url.split("?", 1)[0]


def candidate_sources(config: dict) -> tuple[list[str], list[str]]:
    http_urls = list(config.get("room_list_urls") or [])
    ws_urls = list(config.get("websocket_urls") or [])
    if not http_urls and not ws_urls:
        ws_urls = [DEFAULT_DUEL_LIST]
    username = str(config.get("room_list_username") or "")
    password = str(config.get("room_list_password") or "")
    http_urls = [with_query(url, username, password) for url in http_urls]
    return http_urls, ws_urls


async def http_poll_loop(urls: list[str], board: RoomBoard, stop: asyncio.Event, interval: float) -> None:
    disabled_until: dict[str, float] = {}
    announced: set[str] = set()
    last_count: dict[str, int] = {}
    while not stop.is_set():
        now = time.monotonic()
        for url in urls:
            if disabled_until.get(url, 0) > now:
                continue
            try:
                payload = await read_http_json(url)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
                disabled_until[url] = now + 60
                if url not in announced:
                    announced.add(url)
                    log(f"房间列表暂时不可用：{redact_url(url)}")
                continue
            if room_list_denied(payload):
                disabled_until[url] = now + 300
                log("房间列表需要后台账号。可在 config.json 填写 room_list_username / room_list_password，或改用 websocket_urls。")
                continue
            rooms = rooms_from_json(payload)
            await board.replace_http(rooms)
            if last_count.get(url) != len(rooms):
                last_count[url] = len(rooms)
                announced.discard(url)
                log(f"已连接房间列表，当前 {len(rooms)} 个房间：{redact_url(url)}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def websocket_loop(url: str, board: RoomBoard, stop: asyncio.Event) -> None:
    announced = None
    while not stop.is_set():
        client = WebSocketClient(url)
        try:
            await client.connect()
            if announced != 'connected':
                announced = 'connected'
                log(f"已连接房间推送：{redact_url(url)}")
            while not stop.is_set():
                text = await asyncio.wait_for(client.recv_text(), timeout=120)
                if text is None:
                    raise ConnectionError('room_push_closed')
                message = json.loads(text)
                if isinstance(message, dict) and "event" in message:
                    await board.apply_ws(url, str(message.get("event")), message.get("data"))
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError, ConnectionError) as error:
            if announced != 'failed':
                announced = 'failed'
                log(f"房间推送暂时不可用：{redact_url(url)}（{type(error).__name__}，20秒后重连）")
        finally:
            await client.close()
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except asyncio.TimeoutError:
            pass


def direct_rooms(config: dict) -> list[RoomInfo]:
    rooms: list[RoomInfo] = []
    for item in config.get("direct_rooms") or []:
        if isinstance(item, str):
            rooms.append(RoomInfo(key=item, title=item, join_id=item, started=True))
        elif isinstance(item, dict):
            join_id = str(item.get("id") or "")
            title = str(item.get("title") or join_id)
            key = join_id or title
            if key:
                rooms.append(RoomInfo(key=key, title=title, join_id=join_id, players=[], started=True))
    return rooms


def apply_watch_rules(writer: ReplayWriter, config: dict, names: list[str], rank_rules: list[RankRule]) -> None:
    writer.targets = watch_targets(names, rank_rules)
    writer.mode = str(config.get("name_match") or "exact")


class AdaptiveConcurrency:
    """默认 12；60 秒内集中失败达到阈值降到 8；稳定 30 分钟后回到 12。"""
    def __init__(self, config: dict):
        self.high = max(1, int(config.get('max_concurrent') or 12))
        self.low = max(1, min(self.high, int(config.get('fallback_concurrent') or 8)))
        self.window = float(config.get('adaptive_error_window_seconds') or 60)
        self.threshold = max(1, int(config.get('adaptive_error_threshold') or 6))
        self.restore = float(config.get('adaptive_restore_seconds') or 1800)
        self.current = self.high
        self.errors: list[float] = []
        self.reduced_at: Optional[float] = None

    def failed(self, now: float) -> Optional[str]:
        self.errors = [t for t in self.errors if now - t <= self.window]
        self.errors.append(now)
        if self.current > self.low and len(self.errors) >= self.threshold:
            self.current = self.low
            self.reduced_at = now
            self.errors.clear()
            return f'并发自适应：{self.high}→{self.low}（{self.threshold} 次/ {int(self.window)} 秒）'
        return None

    def tick(self, now: float) -> Optional[str]:
        if self.current < self.high and self.reduced_at is not None and now - self.reduced_at >= self.restore:
            recent = [t for t in self.errors if now - t <= self.window]
            if not recent:
                self.current = self.high
                self.reduced_at = None
                return f'并发自适应：{self.low}→{self.high}（{int(self.restore / 60)} 分钟无集中失败）'
        return None

    def value(self) -> int:
        return self.current

    def state(self) -> dict:
        return dict(current_concurrency=self.current, base_concurrency=self.high,
                    fallback_concurrency=self.low, errors_in_window=len(self.errors))


class RollingJoinStats:
    """最近 N 秒的进场统计：次数、成功、0 包静默关闭、峰值并发。"""

    def __init__(self, window=STATS_WINDOW_SECONDS):
        self.window = float(window)
        self.events = []   # (t, ok, silent)
        self.samples = []  # (t, in_flight)

    def _prune(self, now):
        limit = now - self.window
        while self.events and self.events[0][0] < limit:
            self.events.pop(0)
        while self.samples and self.samples[0][0] < limit:
            self.samples.pop(0)

    def note_outcome(self, now, ok, silent=False):
        self.events.append((float(now), bool(ok), bool(silent)))
        self._prune(float(now))

    def note_in_flight(self, now, value):
        self.samples.append((float(now), int(value)))
        self._prune(float(now))

    def success_rate(self, now):
        self._prune(float(now))
        total = len(self.events)
        ok = sum(1 for _, good, _ in self.events if good)
        return (ok / total if total else 1.0), total

    def snapshot(self, now):
        self._prune(float(now))
        return {
            "join_window": len(self.events),
            "success_window": sum(1 for _, ok, _ in self.events if ok),
            "silent_window": sum(1 for _, _, silent in self.events if silent),
            "peak_in_flight_window": max((value for _, value in self.samples), default=0),
            "window_seconds": int(self.window),
        }


class SilentWindowTracker:
    """连续 0 包静默关闭的窗口统计（区分"IP 级静默"与普通房间结束）。"""

    def __init__(self):
        self.start = None
        self.total = 0.0
        self.windows = 0
        self.day = None

    def _roll_day(self, now):
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.day and day != self.day:
            self.start = None
            self.total = 0.0
            self.windows = 0
        self.day = day

    def note(self, now, silent):
        self._roll_day(now)
        if silent:
            if self.start is None:
                self.start = now
                self.windows += 1
        elif self.start is not None:
            self.total += now - self.start
            self.start = None

    def snapshot(self, now):
        self._roll_day(now)
        active = (now - self.start) if self.start is not None else 0.0
        return dict(silent_ip_windows=self.windows,
                    silent_ip_seconds=round(self.total + active, 1),
                    silent_ip_active=bool(self.start is not None))


class CaptureMonitor:
    """有界排队、最早发现优先；计数只描述当前进程实际看到的房间。"""
    def __init__(self, limit=200):
        self.limit = max(1, min(2000, int(limit)))
        self.rooms = {}
        self.counts = dict(discovered_rooms=0, joined_observations=0,
                           disappeared_without_join=0, completed_observations=0, failed_observations=0,
                           noreplay_observations=0, gone_silent=0, gone_stale=0)
        self.waiting = self.overflow = self.cooling = 0

    def candidates(self, rooms, matched, active, cooldown, now, starts=None):
        """排队顺序：先按开局时间早的分组；同一 5 分钟窗口内的，按双方排名之和更小优先。"""
        matched = list({room.key:room for room in matched}.values())
        current = {r.key for r in rooms}
        for key in list(self.rooms):
            if key not in current:
                old = self.rooms.pop(key)
                if old['started'] and not old['joined']:
                    self.counts['disappeared_without_join'] += 1
        for room in matched:
            if room.key not in self.rooms:
                self.rooms[room.key] = dict(first_seen=now, started=room.started, joined=False)
                self.counts['discovered_rooms'] += 1
            self.rooms[room.key]['started'] |= room.started
        pending = [r for r in matched if r.key not in active]
        self.cooling = sum(cooldown.get(r.key, 0) > now for r in pending)
        pending = [r for r in pending if cooldown.get(r.key, 0) <= now]
        starts = starts or {}

        def start_of(room):
            info = self.rooms.get(room.key) or {}
            started = starts.get(room.key)
            if started is None:
                started = info.get('first_seen')
            return float(started or now)

        # 开局时间差 ≤5 分钟视为同一批：批内按排名和；
        # 跨批按开始时间早优先（先按时间排，再按 5 分钟窗口切组）。
        pending.sort(key=lambda r: (start_of(r), r.key))
        ordered = []
        index = 0
        while index < len(pending):
            base = start_of(pending[index])
            end = index + 1
            while end < len(pending) and start_of(pending[end]) - base <= JOIN_PRIORITY_WINDOW_SECONDS:
                end += 1
            group = pending[index:end]
            group.sort(key=lambda r: (room_rank_sum(r), start_of(r), r.key))
            ordered.extend(group)
            index = end
        pending = ordered
        self.waiting = len(pending)
        self.pending_keys = {r.key for r in pending}
        self.overflow = max(0, len(pending) - self.limit)
        return pending[:self.limit]

    def joined(self, key):
        self.rooms[key]['joined'] = True
        self.pending_keys.discard(key)
        self.waiting = len(self.pending_keys)
        self.overflow = max(0,self.waiting-self.limit)
        self.counts['joined_observations'] += 1

    def finished(self, outcome, detail=None):
        """按结果计数。detail 里带进场诊断（收包数、房间是否还在列表等），用于拆 gone 的原因。"""
        detail = detail or {}
        if outcome == 'saved':
            self.counts['completed_observations'] += 1
        elif outcome in ('retry', 'config', 'unstable'):
            self.counts['failed_observations'] += 1
        elif outcome == 'noreplay':
            # 对局结束但服务器没下发录像：以前完全没计数，现在单列。
            self.counts['noreplay_observations'] += 1
        elif outcome == 'gone':
            self.counts['unavailable_rooms'] = self.counts.get('unavailable_rooms', 0) + 1
            if detail.get('in_board') is False:
                # 进场时房间已从列表消失：多半是排期太晚/房间被关（stale）。
                self.counts['gone_stale'] += 1
            else:
                # 房间还在列表里，但连上后 0 包被关：口令/会话/局间状态（silent）。
                self.counts['gone_silent'] += 1

    def snapshot(self, active, writer):
        connected = sum(1 for value in LIVE_LINKS.values() if value)
        return dict(self.counts, active_observations=active, connected_observations=connected,
                    reconnecting_observations=max(0, active - connected), waiting_rooms=self.waiting,
                    queue_limit=self.limit, queue_overflow=self.overflow, cooldown_rooms=self.cooling,
                    saved_games=writer._metrics['saved_games'], updated_at=time.time())


def cleanup_old_files(replay_dir: Path, deck_dir: Path, now: float, max_age: float = RETENTION_SECONDS,
                      replay_times: Optional[dict[str, float]] = None) -> int:
    """删除超过保留时长的旧录像(.yrp)和旧卡组(.ydk)，返回删除的文件数。"""
    removed = 0
    for directory, suffix in ((Path(replay_dir), ".yrp"), (Path(deck_dir), ".ydk")):
        if not directory.is_dir():
            continue
        for path in directory.rglob(f"*{suffix}"):
            try:
                path.resolve().relative_to(directory.resolve())
                stamp = (replay_times or {}).get(str(path.resolve()), path.stat().st_mtime)
                if now - stamp >= max_age:
                    path.unlink()
                    if suffix == '.yrp':
                        for kind in ('analysis', 'evidence'):
                            Path(str(path) + '.' + kind + '.json').unlink(missing_ok=True)
                    removed += 1
            except (OSError, ValueError):
                continue
    # 索引去重或人工删除录像后，也不留下孤立的解析结果。
    for kind in ('analysis', 'evidence'):
        for metadata in Path(replay_dir).rglob('*.yrp.' + kind + '.json'):
            try:
                metadata.resolve().relative_to(Path(replay_dir).resolve())
                original = Path(str(metadata)[:-len('.' + kind + '.json')])
                if not original.is_file(): metadata.unlink()
            except (OSError, ValueError):
                continue
    return removed


def join_gate_from_config(config: dict) -> JoinGate:
    """按 watch 配置建"进场节流"闸门；缺省或非法值回落 watch 侧策略默认 8/10/3/60。

    bot/join_throttle.py 自身默认仍是 2/5/3/60（那里保持通用）。框架 owner 的 service.py 目前会把
    "config 没配"的键按旧缺省 2/5/3/60 传进来，这种值等同于未配置（见 SERVICE_*_FALLBACK），
    否则用户定的 8/10 会被悄悄盖回 2/5。
    """
    def read(key, default, fallback, cast):
        value = config.get(key)
        if value is None:
            return default
        try:
            value = cast(value)
        except (TypeError, ValueError):
            log(f"进场节流配置 {key}={value!r} 不是数字，改用默认 {default}")
            return default
        if fallback is not None and value == fallback:
            return default
        return value
    values = dict(
        max_concurrent=read('max_join_concurrent', JOIN_CONCURRENT_DEFAULT,
                            SERVICE_JOIN_CONCURRENT_FALLBACK, int),
        min_interval=read('join_min_interval_seconds', JOIN_MIN_INTERVAL_DEFAULT,
                          SERVICE_JOIN_INTERVAL_FALLBACK, float),
        fail_threshold=read('join_fail_threshold', DEFAULT_FAIL_THRESHOLD, None, int),
        pause_seconds=read('join_pause_seconds', DEFAULT_PAUSE, None, float),
        jitter=read('join_interval_jitter_seconds', JOIN_INTERVAL_JITTER_DEFAULT, None, float),
        max_per_hour=read('max_joins_per_hour', JOIN_MAX_PER_HOUR_DEFAULT, None, int),
    )
    try:
        return JoinGate(**values)
    except ValueError:
        log(f"进场节流配置超出范围（{values}），改用默认 {JOIN_CONCURRENT_DEFAULT}/"
            f"{JOIN_MIN_INTERVAL_DEFAULT:g}/{DEFAULT_FAIL_THRESHOLD}/{DEFAULT_PAUSE:g}")
        return JoinGate(JOIN_CONCURRENT_DEFAULT, JOIN_MIN_INTERVAL_DEFAULT)


def preexisting_join_window(config: dict) -> tuple[float, float]:
    """启动前已在打的对局：到期窗口（秒）。缺省 20—40 秒错峰，避免启动瞬间一起扑进去。

    service.py 目前在没有配置时传 1/10（旧缺省），按未配置处理；配置其他值以配置为准。
    """
    def number(key, default, fallback):
        value = config.get(key)
        if value is None:
            return default
        try:
            value = max(0.0, float(value))
        except (TypeError, ValueError):
            log(f"进屋错峰配置 {key}={value!r} 不是数字，改用默认 {default}")
            return default
        if fallback is not None and value == fallback:
            return default
        return value
    low = number('preexisting_join_min_seconds', PREEXISTING_JOIN_MIN_DEFAULT,
                 SERVICE_PREEXISTING_MIN_FALLBACK)
    high = number('preexisting_join_max_seconds', PREEXISTING_JOIN_MAX_DEFAULT,
                  SERVICE_PREEXISTING_MAX_FALLBACK)
    return low, max(low, high)


def plan_gap_after(cursor: list, when: float, gap: float) -> float:
    """把"计划进入时刻"推到上一次计划之后至少 gap 秒，避免多局排期扎堆。

    cursor 是单元素列表，记录下一次可用的计划时刻（墙钟）；每次真正排期时由这里推进。
    闸门仍在进入那一刻兜底（allow/started），这里只是让计划和它对同一个节奏。
    """
    earliest = cursor[0] if cursor else 0.0
    if when < earliest:
        when = earliest
    if cursor:
        cursor[0] = when + max(0.0, gap)
    return when


def pause_for_level(base: float, level: int, cap: float = 300.0) -> float:
    """连续失败时的暂停时长：第 1 次用基础值，之后逐级翻倍，最多 cap 秒。

    平台进入"进房 0 字节被关"的时段时，60 秒一次地反复重试只会白撞；逐级拉长能把压力降下来，
    中间只要成功一次就回到基础值（见 serve 里的 pause_success_mark）。
    """
    if base <= 0:
        return 0.0
    return min(base * (2 ** max(0, level - 1)), cap)


class JoinConcurrencyTuner:
    """按"排队是否吃完 + 是否被拒"自动调并发上限：稳就加、被拒就退，自己找到平台能接受的水位。

    只看窗口内的增量（进场数/失败数/暂停数），不做全局统计；改动只落在闸门的 max_concurrent 上，
    间隔、失败暂停、系统自适应并发都不受影响。
    """

    def __init__(self, gate, floor=JOIN_CONCURRENT_FLOOR, ceiling=JOIN_CONCURRENT_CEILING,
                 step=JOIN_TUNE_STEP, interval=JOIN_TUNE_INTERVAL):
        self.gate = gate
        self.floor = max(1, min(int(floor), int(ceiling)))
        self.ceiling = max(self.floor, int(ceiling))
        self.step = max(1, int(step))
        self.interval = float(interval)
        self.last_at = None
        self.mark = dict(gate.stats)
        self.cooldown_until = 0.0
        self.changes: list[tuple] = []

    def observe(self, now: float, waiting: int, in_flight: int):
        """上限变化时返回一行日志，否则 None。"""
        if self.last_at is None:
            self.last_at = now
            self.mark = dict(self.gate.stats)
            return None
        if now - self.last_at < self.interval:
            return None
        attempts = self.gate.stats['started'] - self.mark['started']
        failures = self.gate.stats['failed'] - self.mark['failed']
        pauses = self.gate.stats['pauses'] - self.mark['pauses']
        self.mark = dict(self.gate.stats)
        self.last_at = now
        current = self.gate.max_concurrent
        target, reason = current, ''
        if pauses > 0 or (attempts >= 3 and failures / attempts >= 0.3):
            target = max(self.floor, current - self.step)
            reason = f'同期被拒 {failures}/{attempts}、暂停 {pauses} 次'
            self.cooldown_until = now + 2 * self.interval
        elif (waiting >= 2 and in_flight >= current and failures == 0
              and now >= self.cooldown_until):
            target = min(self.ceiling, current + self.step)
            reason = f'排队 {waiting} 个、已占用 {in_flight} 场、同期 0 失败'
        if target == current:
            return None
        self.gate.max_concurrent = target
        self.changes.append((round(now, 1), current, target))
        return f'进场节流：并发上限 {current} → {target}（{reason}）'


def remote_payload(config: dict, room: RoomInfo, writer: ReplayWriter,
                   known_start, reason: str) -> dict:
    """把房间整理成远程端可直接使用的任务。口令由服务器算好，远程端不碰账号凭据。"""
    password = build_join_password(config, room)
    focus = replay_folders(room.players, writer.targets, writer.mode)
    return dict(
        room=dict(key=room.key, title=room.title, join_id=room.join_id,
                  players=list(room.players),
                  seats={str(pos): name for pos, name in room.seats.items()},
                  started=bool(room.started)),
        server=dict(host=str(config["host"]), port=int(config["port"]),
                    version=int(parse_version(config.get("version") or DEFAULT_VERSION))),
        password=password,
        nickname=str(config.get("nickname") or "Spectator"),
        focus=focus,
        started_at=known_start,
        reason=reason,
    )


async def dispatch_remote(pool: dict, config: dict, room: RoomInfo, writer: ReplayWriter,
                          known_start, shown: str, reason: str) -> str:
    """把房间交给远程观战端；返回认领者名字（空串表示没人接，转本地观战）。"""
    try:
        payload = remote_payload(config, room, writer, known_start, reason)
    except RuntimeError as exc:
        log(f"远程派发跳过：{exc}")
        return ""
    try:
        worker = await asyncio.get_running_loop().run_in_executor(
            None, pool["jobs"].offer_sync, payload, float(pool.get("wait") or 3.0))
    except Exception as exc:  # 远程通道异常不能影响本地观战
        log(f"远程派发失败：{exc}")
        return ""
    if worker:
        log(f"已派发远程观战：{shown} → {worker}")
    return worker


async def serve(
    config: dict,
    names: list[str],
    rank_rules: list[RankRule],
    rules_provider=None,
    on_saved=None,
    on_session=None,
    on_status=None,
) -> None:
    writer = ReplayWriter(
        Path(config["replay_dir"]),
        watch_targets(names, rank_rules),
        str(config.get("name_match") or "exact"),
    )
    writer.on_saved = on_saved
    writer.on_session = on_session
    remote = None
    remote_cfg = config.get("remote") if isinstance(config.get("remote"), dict) else {}
    if remote_cfg.get("enabled"):
        from bot.watch_jobs import RemoteJobs
        from bot.watch_api import WatchApiServer
        jobs = RemoteJobs(lease=float(remote_cfg.get("lease_seconds") or 120),
                          job_ttl=float(remote_cfg.get("job_ttl_seconds") or 180))
        api = WatchApiServer(jobs, Path(config["replay_dir"]), str(remote_cfg.get("token") or ""),
                             log=log, bind=(str(remote_cfg.get("bind_host") or "127.0.0.1"),
                                            int(remote_cfg.get("bind_port") or 8791)))
        if api.start():
            remote = dict(jobs=jobs, api=api, wait=float(remote_cfg.get("claim_wait_seconds") or 3.0))
            log(f"远程观战：已启用，等待间隔 {remote['wait']:g} 秒，任务租期 {jobs.lease:g} 秒")
    board = RoomBoard()
    stop = asyncio.Event()
    http_urls, ws_urls = candidate_sources(config)
    tasks = [
        asyncio.create_task(http_poll_loop(http_urls, board, stop, float(config.get("poll_seconds") or 5))),
    ]
    for url in ws_urls:
        tasks.append(asyncio.create_task(websocket_loop(url, board, stop)))
    active: dict[str, asyncio.Task] = {}
    sessions: dict[str, Spectator] = {}
    startup_keys: Optional[set[str]] = None  # 首次快照里"启动前就在打"的房间
    startup_logged: set[str] = set()
    rolling = RollingJoinStats()
    silent_streak = 0
    last_rate_check = 0.0
    monitor = CaptureMonitor(config.get('max_queued_rooms') or 200)
    gate = join_gate_from_config(config)
    log(f"进场节流：并发上限从 {gate.max_concurrent} 起自动调，两次进场至少间隔 {gate.min_interval:g} 秒，"
        f"连续失败 {gate.fail_threshold} 次暂停 {gate.pause_seconds:g} 秒。")
    concurrency_ceiling = min(JOIN_CONCURRENT_CEILING,
                              max(gate.max_concurrent, int(config.get('max_concurrent') or JOIN_CONCURRENT_CEILING)))
    tuner = JoinConcurrencyTuner(gate, ceiling=concurrency_ceiling)
    log(f"进场节流：并发在 {tuner.floor}—{tuner.ceiling} 之间自动调（每 {tuner.interval / 60:g} 分钟评估一次，"
        f"有排队且无失败 +{tuner.step}，被拒或暂停 -{tuner.step}）。")
    last_metrics_at = 0.0
    last_summary_at = time.time()
    last_summary_mark = dict(started=0, failed=0, pauses=0, saved=0, silent=0)
    watch_event("startup", pid=os.getpid(), cap=gate.max_concurrent, interval=gate.min_interval,
                jitter=gate.jitter, max_per_hour=gate.max_per_hour,
                fail_threshold=gate.fail_threshold, pause_seconds=gate.pause_seconds,
                floor=tuner.floor, ceiling=tuner.ceiling)
    night_enabled = config_flag(config, "night_mode_enabled", True) and gate.min_interval > 0
    night_start = config_number(config, "night_start_hour", NIGHT_START_HOUR, int)
    night_end = config_number(config, "night_end_hour", NIGHT_END_HOUR, int)
    night_floor = config_number(config, "night_min_concurrent", NIGHT_MIN_CONCURRENT, int)
    night_ceiling = config_number(config, "night_max_concurrent", NIGHT_MAX_CONCURRENT, int)
    night_interval = config_number(config, "night_min_interval_seconds", NIGHT_MIN_INTERVAL, float)
    night_jitter = config_number(config, "night_jitter_seconds", NIGHT_JITTER, float)
    day_jitter = gate.jitter
    day_interval = gate.min_interval
    day_cap = gate.max_concurrent
    night_mode = False
    catchup_limit = config_number(config, "startup_catchup_limit", STARTUP_CATCHUP_LIMIT, int)
    catchup_joined = 0
    catchup_skipped = set()
    retry_zero = {}
    remote_dispatched = 0
    remote_reported = 0
    silent_tracker = SilentWindowTracker()
    probation_enabled = config_flag(config, "probation_enabled", True) and gate.min_interval > 0
    probation_until = time.monotonic() + PROBATION_SECONDS if probation_enabled else 0.0
    probation_fail_mark = gate.stats["failed"]
    if probation_enabled:
        gate.start_probation(time.monotonic(), PROBATION_SECONDS,
                             PROBATION_MAX_CONCURRENT, PROBATION_MIN_INTERVAL)
        log(f"观察期：前 {PROBATION_SECONDS / 60:g} 分钟 {PROBATION_MAX_CONCURRENT} 路、"
            f"{PROBATION_MIN_INTERVAL:g} 秒；0 失败后恢复。重启后最多追 {catchup_limit} 个旧局。")
        watch_event("probation_start", seconds=PROBATION_SECONDS, cap=PROBATION_MAX_CONCURRENT,
                    interval=PROBATION_MIN_INTERVAL, catchup_limit=catchup_limit)
    last_local_ip = default_local_ip()
    adaptive = AdaptiveConcurrency(config)
    rank_window=None
    if config.get('automatic_rank_limit',100)>100:
        from bot.watch_rank_window import RankWindow
        rank_window=RankWindow(int(config['automatic_rank_limit']))
    previous_rank_error=None
    cooldown_until: dict[str, float] = {}
    dead_rooms: set[str] = set()
    parts = []
    if names:
        parts.append("只要在场就观战：" + "、".join(names))
    if rank_rules:
        parts.append("仅对手进前 N 名才观战：" + "、".join(f"{rule.name}（前{rule.top}）" for rule in rank_rules))
    folder_note = "，按观战玩家分子文件夹" if writer.targets else ""
    log("；".join(parts) + f"。服务器 {config['host']}:{config['port']}，录像目录 {writer.directory}{folder_note}")
    noted: set[str] = set()
    join_at: dict[str, float] = {}
    announced_wait: set[str] = set()
    gate_blocked: dict[str, tuple[str, float]] = {}
    gate_pauses = 0
    pause_level = 0
    pause_success_mark = 0
    stale_skipped: set[str] = set()
    plan_gap = gate.min_interval
    plan_cursor = [0.0]
    preexisting_min, preexisting_max = preexisting_join_window(config)
    min_delay = float(config.get("join_after_min_seconds") if config.get("join_after_min_seconds") is not None else 120)
    max_delay = float(config.get("join_after_max_seconds") if config.get("join_after_max_seconds") is not None else 240)
    _max_age = config.get("join_max_room_age_seconds")
    max_room_age = JOIN_MAX_ROOM_AGE if _max_age is None else float(_max_age)
    log(
        f"进入时间：看到开局的对局，在开局后 {min_delay / 60:g}–{max_delay / 60:g} 分钟内随机进入。"
        "连接前已经开始的对局，只要符合条件就立刻进入。"
    )
    if config.get("join_mode") != "plain" and not config.get("account") and not load_session().get("token"):
        log("还没有萌卡账号。请在 config.json 填写 account 和 password，否则只能看到对局、无法加入。")
    next_cleanup = 0.0
    try:
        while True:
            now_wall = time.time()
            if night_enabled:
                night = in_night_window(now_wall, night_start, night_end)
                if night and not night_mode:
                    night_mode = True
                    day_interval = gate.min_interval
                    day_cap = gate.max_concurrent
                    day_jitter = gate.jitter
                    gate.min_interval = night_interval
                    gate.jitter = night_jitter
                    gate.max_concurrent = min(max(gate.max_concurrent, night_floor), night_ceiling)
                    log(f"夜间降档（{night_start:02d}:00–{night_end:02d}:00）："
                        f"并发 {gate.max_concurrent}、间隔 {gate.min_interval:g}–{gate.min_interval + gate.jitter:g} 秒")
                    watch_event("night_mode", on=True, cap=gate.max_concurrent,
                                interval=gate.min_interval, jitter=gate.jitter)
                elif not night and night_mode:
                    night_mode = False
                    gate.min_interval = day_interval
                    gate.jitter = day_jitter
                    gate.max_concurrent = max(gate.max_concurrent, day_cap)
                    log(f"夜间降档结束：恢复并发 {gate.max_concurrent}、"
                        f"间隔 {gate.min_interval:g}–{gate.min_interval + gate.jitter:g} 秒")
                    watch_event("night_mode", on=False, cap=gate.max_concurrent,
                                interval=gate.min_interval, jitter=gate.jitter)
                if night_mode:
                    gate.max_concurrent = min(max(gate.max_concurrent, night_floor), night_ceiling)
            ip_now = default_local_ip()
            if last_local_ip and ip_now and ip_now != last_local_ip:
                last_local_ip = ip_now
                if probation_enabled:
                    probation_until = time.monotonic() + PROBATION_SECONDS
                    probation_fail_mark = gate.stats["failed"]
                    gate.start_probation(time.monotonic(), PROBATION_SECONDS,
                                         PROBATION_MAX_CONCURRENT, PROBATION_MIN_INTERVAL)
                    log(f"网络变化（本地 IP {ip_now}）：重新进入 {PROBATION_SECONDS / 60:g} 分钟观察期")
                    watch_event("network_change", local_ip=ip_now, probation_seconds=PROBATION_SECONDS)
            if probation_until and time.monotonic() >= probation_until:
                if gate.stats["failed"] <= probation_fail_mark:
                    probation_until = 0.0
                    log("观察期结束：10 分钟 0 失败，恢复常规并发/间隔")
                    watch_event("probation_end", failed=0)
                else:
                    probation_fail_mark = gate.stats["failed"]
                    probation_until = time.monotonic() + PROBATION_SECONDS
                    gate.start_probation(time.monotonic(), PROBATION_SECONDS,
                                         PROBATION_MAX_CONCURRENT, PROBATION_MIN_INTERVAL)
                    log("观察期延长：最近 10 分钟有失败，再观察 10 分钟")
                    watch_event("probation_extend", failed=gate.stats["failed"])
            if config.get("cleanup_files", True) and now_wall >= next_cleanup:
                removed = await asyncio.to_thread(cleanup_old_files, Path(config["replay_dir"]),
                                                  Path(config.get("deck_dir") or DATA_DIR / "decks"), now_wall)
                if removed:
                    log(f"已清理 {removed} 个超过14天的旧录像/卡组")
                next_cleanup = now_wall + next_cleanup_delay(
                    now_wall, Path(config["replay_dir"]),
                    Path(config.get("deck_dir") or DATA_DIR / "decks"))
            if rules_provider is not None:
                # 只更新下一轮选房，新房间固定归档范围；不取消已经在观战的房间。
                names, rank_rules = rules_provider()
                apply_watch_rules(writer, config, names, rank_rules)
            rooms = await board.snapshot()
            rooms.extend(direct_rooms(config))
            selection_names=names
            if rank_window is not None:
                observed=rank_window.update({p for room in rooms for p in room.players if p},_official_ranks,_official_rank_updated_at,_official_rank_stale)
                _observed_room_ranks.clear();_observed_room_ranks.update(observed)
                selection_names=list(dict.fromkeys([*names,*observed]))
                for name,(rank,checked_at) in observed.items():
                    _watch_target_reasons[name]=f'official per-player rank {rank}/200; checked_at={checked_at:.0f}; roster vtime={_official_rank_updated_at}'
                apply_watch_rules(writer,config,selection_names,rank_rules)
                current_error=rank_window.last_error
                if current_error!=previous_rank_error:
                    log(('Top200 rank lookup: '+current_error+'; unknown/stale players skipped') if current_error else 'Top200 official rank lookup recovered')
                    previous_rank_error=current_error
            matched, notes = await asyncio.to_thread(
                select_rooms,
                rooms,
                selection_names,
                str(config.get("name_match") or "exact"),
                bool(config.get("watch_waiting_rooms", True)),
                rank_rules,
            )
            fresh_notes = set(notes)
            for note in notes:
                if note not in noted:
                    log(note)
            noted.clear()
            noted.update(fresh_notes)
            starts, preexisting = await board.timing()
            if startup_keys is None:
                startup_keys = set(preexisting)
            direct_keys = {room.key for room in direct_rooms(config)}
            allow_preexisting = bool(config.get("join_preexisting", True))
            if not allow_preexisting:
                matched = joinable_rooms(matched, preexisting, direct_keys, False)
            for room in matched:
                if room.key in direct_keys and room.key not in starts and room.key not in preexisting:
                    starts[room.key] = await board.note_fresh_start(room.key)
            for extra in direct_rooms(config):
                if extra.key not in {room.key for room in matched}:
                    matched.append(extra)
                    if extra.key not in starts and extra.key not in preexisting:
                        starts[extra.key] = await board.note_fresh_start(extra.key)
            current_keys = {room.key for room in rooms}
            for key in list(join_at):
                if key not in current_keys:
                    join_at.pop(key, None)
                    announced_wait.discard(key)
            done_keys = [key for key, task in active.items() if task.done()]
            now = time.monotonic()
            for key in done_keys:
                task = active.pop(key)
                session = sessions.pop(key, None)
                try:
                    outcome = task.result()
                except Exception as exc:
                    log(f"观战任务异常：{exc}")
                    outcome = "retry"
                detail = {}
                if session is not None:
                    try:
                        room_info = getattr(session, 'room', None)
                        room_name = (getattr(room_info, 'title', None) or getattr(room_info, 'join_id', None)
                                     or key)
                        started_at = getattr(session, 'room_started_at', None)
                        joined_at = getattr(session, 'joined_at', 0) or 0
                        age = (joined_at - started_at) if started_at and joined_at else None
                        detail = dict(key=key, room=room_name,
                                      packets=int(getattr(session, 'packets_received', 0) or 0),
                                      saved=int(getattr(session, 'replays_saved', 0) or 0),
                                      room_age=round(age, 1) if age is not None else None,
                                      in_board=key in current_keys, outcome=outcome,
                                      silent_ip=bool(outcome == 'gone' and
                                                     int(getattr(session, 'packets_received', 0) or 0) == 0),
                                      drop=str(getattr(session, '_drop_reason', '') or ''))
                        write_join_diag(detail)
                    except Exception:
                        detail = {}
                    if detail and outcome != 'saved':
                        log(f"[进场诊断] {detail['room']}｜房龄 {detail['room_age']}s｜收包 {detail['packets']}｜"
                            f"结果 {outcome}｜{detail['drop']}")
                pause = {"saved": 12, "noreplay": 20, "leave": 30, "retry": 3, "unstable": 120, "gone": 300, "config": 120}.get(outcome, 5)
                monitor.finished(outcome, detail)
                packets_zero = (detail.get('packets') or 0) == 0
                silent_room = bool(outcome in ('gone', 'retry', 'unstable') and packets_zero)
                silent_tracker.note(time.time(), silent_room)
                rolling.note_outcome(time.time(), outcome == 'saved', silent_room)
                if outcome == 'saved':
                    silent_streak = 0
                elif silent_room:
                    silent_streak += 1
                startup_room = bool(startup_keys and key in startup_keys and outcome == 'gone')
                gate.finished(now, outcome, startup=startup_room)
                if silent_streak >= SILENT_STREAK_LIMIT:
                    # 连续 0 包静默关闭：几乎可以确定是出口 IP / 账号会话被平台拒绝，继续撞只会更糟。
                    silent_streak = 0
                    blocked_until = now + SYSTEMIC_PAUSE_SECONDS
                    gate.paused_until = max(gate.paused_until, blocked_until)
                    message = (f"连续 {SILENT_STREAK_LIMIT} 次 0 包静默关闭（疑似出口 IP 被平台限制或账号被顶），"
                               f"暂停进场 {SYSTEMIC_PAUSE_SECONDS / 60:g} 分钟；建议换网络或检查同账号是否在别处登录")
                    log(message)
                    watch_event("breaker_silent", streak=SILENT_STREAK_LIMIT,
                                pause_seconds=SYSTEMIC_PAUSE_SECONDS, cap=gate.max_concurrent)
                if startup_room and key not in startup_logged:
                    startup_logged.add(key)
                    log(f"启动前已在打的对局已结束/不可观战：{key}（追中途局，不计失败）")
                if gate.stats['pauses'] > gate_pauses:
                    gate_pauses = gate.stats['pauses']
                    if gate.stats['succeeded'] > pause_success_mark:
                        pause_success_mark = gate.stats['succeeded']
                        pause_level = 0
                    pause_level += 1
                    seconds = pause_for_level(gate.pause_seconds, pause_level)
                    seconds = round(seconds * random.uniform(0.9, 1.1), 1)
                    gate.paused_until = max(gate.paused_until, now + seconds)
                    log(f"进场节流：连续失败 {gate.fail_threshold} 次，暂停进场 {seconds:g} 秒"
                        f"（连续第 {pause_level} 次暂停；成功一次后回到 {gate.pause_seconds:g} 秒）。")
                    watch_event("pause", seconds=seconds, level=pause_level, cap=gate.max_concurrent)
                    # 暂停会把所有排期一起推迟：暂停结束后必然超过房龄上限的房间直接放弃，
                    # 免得一放行就把积压的老房间一起撞进去（那只会"0 字节被关"再来一轮暂停）。
                    resume_at = max(gate.paused_until, now)
                    resume_wall = time.time() + (resume_at - now)
                    dropped = 0
                    for stale_key, planned in list(join_at.items()):
                        started_wall = starts.get(stale_key)
                        if started_wall is None or max_room_age <= 0:
                            continue
                        if max(planned, resume_wall) - started_wall > max_room_age:
                            join_at.pop(stale_key, None)
                            cooldown_until[stale_key] = resume_at + 600
                            dropped += 1
                    if dropped:
                        log(f"进场节流：暂停期间放弃 {dropped} 个会拖到局末的排期（已开 {max_room_age / 60:g} 分钟以上）。")
                if outcome == "gone":
                    dead_rooms.add(key)
                    # 0 字节被关也可能是口令/身份被拒（服务器 u16Secret 解密失败），下次进场重取口令。
                    invalidate_secret_cache()
                if outcome in ('retry', 'unstable'):
                    message = adaptive.failed(time.monotonic())
                    if message: log(message)
                cooldown_until[key] = now + pause
                if outcome == 'saved':
                    retry_zero.pop(key, None)
                elif outcome == 'retry' and packets_zero:
                    retry_zero[key] = retry_zero.get(key, 0) + 1
                    if retry_zero[key] >= 3:
                        retry_zero.pop(key, None)
                        dead_rooms.add(key)
                        cooldown_until[key] = now + 1800
                        log(f"同一房间连续 3 次 0 包重试，判定不可观战：{key}（冷却 30 分钟）")
            # 最近一小时成功率熔断：低于 50% 且样本够，直接停 15 分钟，不等全灭。
            if time.time() - last_rate_check >= SUCCESS_BREAKER_CHECK_SECONDS:
                last_rate_check = time.time()
                if time.monotonic() < gate.paused_until:
                    rate, seen = 1.0, 0
                else:
                    rate, seen = rolling.success_rate(last_rate_check)
                if seen >= SUCCESS_BREAKER_MIN_SAMPLES and rate < SUCCESS_BREAKER_RATE:
                    gate.paused_until = max(gate.paused_until,
                                            time.monotonic() + SUCCESS_BREAKER_PAUSE_SECONDS)
                    log(f"最近 1 小时进场 {seen} 次、成功率 {rate:.0%}（低于 {SUCCESS_BREAKER_RATE:.0%}），"
                        f"暂停进场 {SUCCESS_BREAKER_PAUSE_SECONDS / 60:g} 分钟；"
                        "疑似出口 IP 被限流，建议换网络或检查同账号登录")
                    watch_event("breaker_rate", rate=round(rate, 3), seen=seen,
                                pause_seconds=SUCCESS_BREAKER_PAUSE_SECONDS, cap=gate.max_concurrent)
            limit = adaptive.value()
            if night_mode:
                limit = min(limit, gate.max_concurrent)
            if probation_until:
                limit = min(limit, PROBATION_MAX_CONCURRENT)
            current_keys = {room.key for room in rooms}
            dead_rooms.intersection_update(current_keys)
            matched = monitor.candidates(rooms, matched, active, cooldown_until, time.monotonic(), starts)
            matched = [room for room in matched if room.key not in dead_rooms]
            now = time.time()
            for room in matched:
                if room.key in active or cooldown_until.get(room.key, 0) > time.monotonic():
                    continue
                if len(active) >= limit:
                    break
                if room.key in preexisting and room.key not in direct_keys:
                    if room.key not in join_at:
                        join_at[room.key] = now + preexisting_min + (preexisting_max - preexisting_min) * random.random()
                    if join_at[room.key] > now:
                        continue
                known_start = starts.get(room.key)
                scheduled_before = join_at.get(room.key)
                action, when = plan_join(
                    now,
                    known_start,
                    scheduled_before,
                    min_delay,
                    max_delay,
                    random.random(),
                )
                if when is not None:
                    if known_start is not None and scheduled_before is None:
                        # 第一次给这局定进入时刻：和上一个计划至少隔 plan_gap，计划行不再扎堆。
                        when = plan_gap_after(plan_cursor, when, plan_gap)
                    join_at[room.key] = when
                if action == "wait":
                    if room.key not in announced_wait and when is not None:
                        announced_wait.add(room.key)
                        clock = datetime.fromtimestamp(when).strftime("%H:%M:%S")
                        waited = max(0, int(when - now))
                        shown = await asyncio.to_thread(room_label_with_ranks, room)
                        log(f"已看到开局，将在 {clock} 进入（约 {waited // 60} 分 {waited % 60} 秒后）：{shown}")
                    continue
                if known_start is not None and max_room_age > 0:
                    age = now - known_start
                    if age > max_room_age:
                        # 拖太久才排到：多半是暂停把排期推到了局末。放弃这间，不发起连接、不算失败，
                        # 否则只会"连上即 0 字节被关"，再触发新一轮暂停。
                        join_at.pop(room.key, None)
                        cooldown_until[room.key] = time.monotonic() + 600
                        if room.key not in stale_skipped:
                            stale_skipped.add(room.key)
                            log(f"跳过已开 {age / 60:.1f} 分钟的对局（超过 {max_room_age / 60:g} 分钟，多半已到局末）："
                                f"{room.title or room.join_id or room.key}")
                        continue
                gate_now = time.monotonic()
                allowed, wait_seconds = gate.allow(gate_now)
                if not allowed:
                    # 并发满/间隔未到/失败暂停：本轮不发起连接，只留等待日志（同一房间同一原因最多每 60 秒一行）。
                    if wait_seconds is None:
                        kind = 'concurrent'
                    else:
                        kind = 'paused' if gate_now < gate.paused_until else 'interval'
                    last = gate_blocked.get(room.key)
                    if last is None or last[0] != kind or gate_now - last[1] >= 60:
                        gate_blocked[room.key] = (kind, gate_now)
                        title = room.title or room.join_id or room.key
                        if kind == 'concurrent':
                            why = f"正在观战 {gate.in_flight}/{gate.max_concurrent} 个"
                        elif kind == 'paused':
                            why = f"失败暂停中，还需等 {wait_seconds:g} 秒"
                        else:
                            why = f"距上次进场还需等 {wait_seconds:g} 秒"
                        log(f"进场节流：暂不进入「{title}」（{why}）")
                    continue
                gate_blocked.pop(room.key, None)
                shown = await asyncio.to_thread(room_label_with_ranks, room)
                reason=selection_reason(room,str(config.get('name_match') or 'exact'))
                if known_start is None:
                    log(f"对局已在进行，开始观战：{shown}；收录依据：{reason}")
                else:
                    log(f"到达进入时间，开始观战：{shown}；收录依据：{reason}")
                if (room.key in preexisting and room.key not in direct_keys
                        and catchup_joined >= catchup_limit):
                    if room.key not in catchup_skipped:
                        catchup_skipped.add(room.key)
                        log(f"追旧局已达上限 {catchup_limit}，放弃积压房间：{room.key}")
                    continue
                if remote is not None and remote["jobs"].online_workers() > 0:
                    # 远程端有在线且有余量时优先派发；派不出去再走本地观战。
                    worker = await dispatch_remote(remote, config, room, writer, known_start, shown, reason)
                    if worker:
                        remote_dispatched += 1
                        monitor.joined(room.key)
                        continue
                gate.started(time.monotonic())
                if room.key in preexisting and room.key not in direct_keys:
                    catchup_joined += 1
                session = Spectator(config, room, writer.for_room(room.players), known_start)
                sessions[room.key] = session
                active[room.key] = asyncio.create_task(session.run())
                monitor.joined(room.key)
            message = adaptive.tick(time.monotonic())
            if message: log(message)
            message = tuner.observe(time.monotonic(), monitor.waiting, len(active))
            if message: log(message)
            rolling.note_in_flight(time.time(), len(active))
            if remote is not None:
                for item in remote["jobs"].drain_ingest():
                    if writer.on_saved:
                        try:
                            writer.on_saved([Path(item["path"])], list(item["players"]),
                                            datetime.fromtimestamp(float(item["recorded_at"])),
                                            str(item["digest"]))
                        except Exception as exc:
                            log(f"远程录像入库失败：{exc}")
                for report in remote["jobs"].drain_reports():
                    remote_reported += 1
                    detail = report.get("detail") or {}
                    extra = f"，录像 {detail.get('saved')} 个" if detail.get("saved") else ""
                    log(f"远程观战结束：{report.get('room') or report.get('job')} ← "
                        f"{report.get('worker')}：{report.get('outcome') or 'unknown'}{extra}")
            wall_now = time.time()
            if wall_now - last_metrics_at >= METRICS_INTERVAL:
                last_metrics_at = wall_now
                counts = monitor.counts
                write_metrics(dict(
                    pid=os.getpid(), cap=gate.max_concurrent, interval=gate.min_interval,
                    jitter=gate.jitter, block_reason=gate.block_reason,
                    in_flight=gate.in_flight, active=len(active), waiting=monitor.waiting,
                    started=gate.stats['started'], failed=gate.stats['failed'],
                    pauses=gate.stats['pauses'], discovered=counts.get('discovered_rooms'),
                    joined=counts.get('joined_observations'),
                    saved=writer._metrics.get('saved_games'),
                    silent=counts.get('gone_silent'), noreplay=counts.get('noreplay_observations'),
                    catchup_joined=catchup_joined, catchup_limit=catchup_limit,
                    remote_workers=(remote["jobs"].online_workers() if remote is not None else 0),
                    remote_dispatched=remote_dispatched, remote_reported=remote_reported,
                    probation_left=gate.probation_left(time.monotonic()),
                    **secret_status(), **rolling.snapshot(wall_now),
                    **silent_tracker.snapshot(wall_now)))
            if wall_now - last_summary_at >= SUMMARY_INTERVAL:
                last_summary_at = wall_now
                counts = monitor.counts
                mark = last_summary_mark or {}
                def delta(key, value):
                    return int(value or 0) - int(mark.get(key) or 0)
                started = delta('started', gate.stats['started'])
                failed = delta('failed', gate.stats['failed'])
                watch_event(
                    "hourly", cap=gate.max_concurrent, active=len(active), waiting=monitor.waiting,
                    started=started, failed=failed,
                    success_rate=round((started - failed) / started, 3) if started else None,
                    pauses=delta('pauses', gate.stats['pauses']),
                    silent=delta('silent', counts.get('gone_silent')),
                    saved=delta('saved', writer._metrics.get('saved_games')),
                    peak=rolling.snapshot(wall_now)['peak_in_flight_window'],
                    silent_ip_seconds=silent_tracker.snapshot(wall_now)['silent_ip_seconds'])
                last_summary_mark = dict(started=gate.stats['started'], failed=gate.stats['failed'],
                                         pauses=gate.stats['pauses'],
                                         saved=writer._metrics.get('saved_games'),
                                         silent=counts.get('gone_silent'))
            if on_status:
                try:on_status(dict(monitor.snapshot(len(active), writer), concurrent_limit=limit, join_throttle=gate.state(), auto_concurrency=dict(floor=tuner.floor, ceiling=tuner.ceiling, changes=len(tuner.changes)), **secret_status(), **rolling.snapshot(time.time()), **(adaptive.state()), **(rank_window.status() if rank_window else {})))
                except Exception:log('采集状态暂未写入，观战继续')
            await asyncio.sleep(float(config.get("poll_seconds") or 5))
    finally:
        if remote is not None:
            remote["api"].stop()
        if rank_window is not None:rank_window.close()
        _observed_room_ranks.clear()
        watch_event("shutdown", pid=os.getpid(), started=gate.stats['started'],
                    failed=gate.stats['failed'], pauses=gate.stats['pauses'],
                    saved=writer._metrics.get('saved_games'), cap=gate.max_concurrent)
        stop.set()
        for task in tasks:
            task.cancel()
        for task in active.values():
            task.cancel()
        await asyncio.gather(*tasks, *active.values(), return_exceptions=True)


def self_test() -> None:
    # 自检的虚构玩家不查询竞技接口，避免离线环境或接口波动拖慢协议检查。
    _rank_cache.update({name: None for name in ('幻影', '翻斗', '路人甲', '路人乙')})
    secret = 123456
    token = encode_mycard_password(secret, 3, "")
    block = decode_mycard_block(secret, token[:8] if len(token) >= 8 else token)
    # token 只有 8 个 base64 字符
    block = decode_mycard_block(secret, encode_mycard_password(secret, 3, "ROOMID1234")[:8])
    assert sum(block) & 0xFF == 0, block.hex()
    assert (block[1] >> 4) == 3

    packet = join_packet(0x1362, "abc")
    assert struct.unpack_from("<H", packet, 0)[0] == 1 + 48
    assert packet[2] == CTOS_JOIN_GAME

    header = struct.pack("<IIIIII", REPLAY_MAGIC_YRP1, 0x1362, 1, 1, 4, 0) + b"\x00" * 8 + b"data"
    files = replay_files_from_payload(header)
    assert files == [header]

    def pack_replay(names: list[str], decks: list[tuple[list[int], list[int]]]) -> bytes:
        body = b""
        for name in names:
            body += name.encode("utf-16le")[:40].ljust(40, b"\x00")
        body += struct.pack("<iiii", 8000, 5, 1, 0)
        for main, extra in decks:
            stored_main = list(reversed(main))
            stored_extra = list(reversed(extra))
            body += struct.pack("<I", len(stored_main))
            if stored_main:
                body += struct.pack(f"<{len(stored_main)}I", *stored_main)
            body += struct.pack("<I", len(stored_extra))
            if stored_extra:
                body += struct.pack(f"<{len(stored_extra)}I", *stored_extra)
        head = struct.pack("<IIIIII", REPLAY_MAGIC_YRP2, 0x1362, 0x10, 0, len(body), 0)
        return head.ljust(80, b"\x00") + body

    game1 = pack_replay(["甲", "乙"], [([1, 1, 2], [10]), ([3], [20])])
    game2 = pack_replay(["甲", "乙"], [([1, 4, 4], [10, 11]), ([3, 5], [20])])
    assert decks_from_replay(game1)[0] == ("甲", [1, 1, 2], [10])
    built = sided_decks([(1, decks_from_replay(game1)), (2, decks_from_replay(game2))])
    assert built["甲"][:3] == ([1, 1, 2], [10], [4, 4, 11])
    assert built["乙"][2] == [5]
    assert replay_game_number("正在发送第2局决斗的录像。") == 2
    assert watch_read_timeout(True, True) == 25
    assert watch_read_timeout(False, False) == 30
    assert watch_read_timeout(False, True) == 120
    assert replay_game_number("冰冷羽毛 加入了观战") is None
    assert "!side\n4\n" in render_ydk([1], [10], [4])
    assert guess_deck_title(
        ["闪刀姬-露世"] * 3 + ["闪刀姬-零衣"] * 2 + ["闪刀起动"] * 3 + ["灰流丽"] * 3
    ) == "闪刀"
    assert guess_deck_title(
        ["闪刀姬-露世"] * 5 + ["闪刀起动"] * 4 + ["神碑之牙"] * 4 + ["神碑之怒"] * 4
    ) == "闪刀·神碑"
    assert guess_deck_title(["魔女术工匠"] * 6 + ["魔女术的"] * 8 + ["魔女的圣夜行"] * 3) == "魔女术"
    assert guess_deck_title(["救援ACE队"] * 16 + ["R救援！"]) == "救援ACE队"
    assert guess_deck_title(["闪刀起动"] * 7) == ""
    assert guess_deck_title(["灰流丽", "增殖的G", "无限泡影"]) == ""
    light_dark = [33599853, 24088928, 44001993]
    assert with_light_dark_field("魔女术", light_dark) == "魔女术·光与暗"
    assert with_light_dark_field("", [*light_dark, 33599853]) == "光与暗"
    assert with_light_dark_field("光与暗", light_dark) == "光与暗"
    assert with_light_dark_field("魔女术", [33599853, 24088928]) == "魔女术"

    _rank_cache["Alpha"] = 12
    _rank_cache["Beta"] = 34
    assert replay_basename(["Alpha", "Beta"], datetime(2026, 9, 30, 10, 41)) == (
        "10-41 “Alpha”（12）vs“Beta”（34）"
    )
    assert replay_folders(["幻影", "路人"], ["幻影", "翻斗"], "exact") == ["幻影"]
    assert replay_folders(["幻影", "翻斗"], ["幻影", "翻斗"], "exact") == ["幻影", "翻斗"]
    assert replay_folders(["路人", "对手"], ["幻影", "翻斗"], "exact") == []
    assert replay_folders(["某某幻影某某"], ["幻影"], "contains") == ["幻影"]
    assert watch_targets(["幻影"], [RankRule("翻斗", 100), RankRule("幻影", 50)]) == ["幻影", "翻斗"]
    sample_replay = struct.pack("<IIIIII", REPLAY_MAGIC_YRP1, 0x1362, 0, 1_700_000_000, 4, 0) + b"data"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        writer = ReplayWriter(root, ["幻影", "翻斗"], "exact")
        when = datetime.fromtimestamp(1_700_000_000)
        saved = writer.save(sample_replay, ["幻影", "翻斗"], [when])
        assert len(saved) == 1
        assert len(list((root / "幻影").glob("*.yrp"))) == 1
        assert len(list((root / "翻斗").glob("*.yrp"))) == 1
        assert writer.save(sample_replay, ["幻影", "翻斗"], [when]) == []
        other = ReplayWriter(root / "plain", ["幻影"], "exact")
        plain = other.save(sample_replay + b"x", ["路人甲", "路人乙"], [when])
        assert len(plain) == 1 and plain[0].parent == root / "plain"
    clock = GameClock(datetime(2026, 10, 11, 10, 0))
    end = datetime(2026, 10, 11, 10, 55)
    assert clock.on_replay(1, end) == datetime(2026, 10, 11, 10, 0)
    clock.on_duel_start(datetime(2026, 10, 11, 10, 21))
    clock.on_duel_start(datetime(2026, 10, 11, 10, 40))
    assert clock.on_replay(2, end) == datetime(2026, 10, 11, 10, 21)
    assert clock.on_replay(3, end) == datetime(2026, 10, 11, 10, 40)
    recorded = datetime(2026, 10, 11, 9, 15, 39)
    header = struct.pack("<IIIIII", REPLAY_MAGIC_YRP2, 0x1362, 0x11, 0, 0, int(recorded.timestamp()))
    assert replay_recorded_time(header) == recorded
    plain = struct.pack("<IIIIII", REPLAY_MAGIC_YRP1, 0x1362, 0, int(recorded.timestamp()), 0, 0)
    assert replay_recorded_time(plain) == recorded
    sample = RoomInfo("k", "M#abc", "M#abc", ["Alpha", "Beta"], True)
    assert room_label_with_ranks(sample) == "M#abc [对战中] Alpha（12） / Beta（34）"

    action, when = plan_join(1000, 1000, None, 120, 240, 0)
    assert action == "wait" and when == 1120
    action, when = plan_join(1000, 1000, None, 120, 240, 1)
    assert action == "wait" and when == 1240
    action, when = plan_join(1240, 1000, 1240, 120, 240, 0.2)
    assert action == "join" and when == 1240
    action, when = plan_join(1500, None, None, 120, 240, 0.5)
    assert action == "join" and when == 1500

    assert is_dealer_chat("发牌姬: 在过去一个月，发牌姬开启了74次歼灭模式。")
    assert is_dealer_chat("冰冷羽毛 加入了观战")
    assert is_dealer_chat("雨云山川 退出了观战")
    assert is_dealer_chat("[Server]: 正在读取用户信息...")
    assert is_dealer_chat("[Server]: 观战中")
    assert not is_dealer_chat("这局怎么打")
    assert name_matches("Alpha", ["Alpha"], "exact")
    assert not name_matches("Alpha", ["Al"], "exact")
    assert name_matches("Alpha", ["lp"], "contains")

    rooms = rooms_from_json(
        {
            "rooms": [
                {
                    "roomname": "Abcd123456",
                    "istart": "Duel:1 Turn:3",
                    "users": [
                        {"name": "目标玩家", "pos": 0},
                        {"name": "对手", "pos": 1},
                        {"name": "看客", "pos": 7},
                    ],
                }
            ]
        }
    )
    assert len(rooms) == 1
    assert rooms[0].players == ["目标玩家", "对手"]
    assert rooms[0].seats == {0: "目标玩家", 1: "对手"}
    assert parse_chat(struct.pack("<H", 1) + "1\x00".encode("utf-16le")) == (1, "1")
    assert format_room_chat(1, "1", rooms[0].seats) == "对手: 1"
    assert format_room_chat(8, "冰冷羽毛 加入了观战", rooms[0].seats) == "冰冷羽毛 加入了观战"
    assert rooms[0].started
    chosen, _notes = select_rooms(rooms, ["目标玩家"], "exact", True)
    assert len(chosen) == 1
    _rank_cache["高手"] = 8
    _rank_cache["路人"] = 500
    ranked_rooms = rooms_from_json(
        {
            "rooms": [
                {"roomname": "high", "istart": "start", "users": [{"name": "目标玩家", "pos": 0}, {"name": "高手", "pos": 1}]},
                {"roomname": "low", "istart": "start", "users": [{"name": "目标玩家", "pos": 0}, {"name": "路人", "pos": 1}]},
            ]
        }
    )
    picked, skipped = select_rooms(ranked_rooms, [], "exact", True, [RankRule("目标玩家", 100)])
    assert [room.key for room in picked] == ["high"]
    assert skipped and "路人" in skipped[0]
    _rank_cache["甲"] = 8
    _rank_cache["乙"] = 500
    both = rooms_from_json(
        {"rooms": [{"roomname": "mix", "istart": "start", "users": [{"name": "甲", "pos": 0}, {"name": "乙", "pos": 1}]}]}
    )
    picked, skipped = select_rooms(
        both, [], "exact", True, [RankRule("甲", 10), RankRule("乙", 100)]
    )
    assert [room.key for room in picked] == ["mix"]
    assert skipped == []
    assert rank_is_within(None, 100) is False
    assert rank_is_within(0, 100) is False
    assert rank_is_within(100, 100) is True
    assert rank_is_within(101, 100) is False
    password = encode_mycard_password(secret, 3, chosen[0].join_id)
    assert len(password) <= 20
    probe = socket.socket()
    try:
        arm_tcp_socket(probe)
    finally:
        probe.close()
    print("self-test ok")
    assert deck_similarity(Counter([1, 2, 3]), Counter([1, 2, 3])) == 1.0
    assert deck_similarity(Counter([1, 2, 3]), Counter([4, 5, 6])) == 0.0
    assert abs(deck_similarity(Counter([1, 1, 2]), Counter([1, 2, 2])) - 2 / 3) < 1e-9
    with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replay_dir = root / "replays"
            deck_dir = root / "decks"
            (replay_dir / "甲").mkdir(parents=True)
            deck_dir.mkdir()
            old = time.time() - RETENTION_SECONDS - 60
            fresh = time.time() - 60
            for path, stamp in (
                (replay_dir / "甲" / "old.yrp", old),
                (replay_dir / "甲" / "new.yrp", fresh),
                (deck_dir / "old.ydk", old),
                (deck_dir / "new.ydk", fresh),
            ):
                path.write_bytes(b"x")
                os.utime(path, (stamp, stamp))
            assert cleanup_old_files(replay_dir, deck_dir, time.time()) == 2
            assert not (replay_dir / "甲" / "old.yrp").exists()
            assert (replay_dir / "甲" / "new.yrp").exists()
            assert not (deck_dir / "old.ydk").exists()
            assert (deck_dir / "new.ydk").exists()


def main() -> None:
    """普通启动由PyCharm控制；显式诊断参数仍支持离线检查。"""
    if '--unified' in sys.argv:
        run_pycharm_watch()
        return
    diagnostic = any(value in sys.argv for value in ('--config', '--name', '--self-test', '--check-config'))
    if not diagnostic:
        if os.environ.get('PYCHARM_HOSTED') == '1' or os.environ.get('QQ_WATCH_IN_PYCHARM') == '1':
            run_pycharm_watch()
        else:
            from bot.pycharm_link import main as launch_pycharm
            launch_pycharm()
        return
    global CONFIG_PATH
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="独立启动统一的YGOPro观战后端")
    parser.add_argument("--name", action="append", default=[], help="要追踪的玩家，可重复")
    parser.add_argument("--config", type=Path, help="独立观战配置，默认data/watch-config.json")
    parser.add_argument("--self-test", action="store_true", help="仅运行本地协议与卡组自检")
    parser.add_argument("--check-config", action="store_true", help="只检查配置和路径，不登录或连接服务器")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    chosen = args.config.resolve() if args.config else CONFIG_PATH
    CONFIG_PATH = chosen
    config = load_config()
    configure_paths(Path(config.get("ygopro_dir") or DEFAULT_YGOPRO_DIR),
                    Path(config.get("data_dir") or DATA_DIR))
    CONFIG_PATH = chosen
    config["replay_dir"] = str(config.get("replay_dir") or DATA_DIR / "replays")
    config["deck_dir"] = str(config.get("deck_dir") or DATA_DIR / "decks")
    names, rules = collect_watch_rules(config, [name.strip() for name in args.name if name.strip()])
    targets = watch_targets(names, rules)
    if args.check_config:
        print("统一后端：", Path(__file__).resolve())
        print("独立配置：", CONFIG_PATH)
        print("录像目录：", config["replay_dir"])
        print("卡组目录：", config["deck_dir"])
        print("追踪玩家数量：", len(targets))
        print("萌卡账号密码：", "已填写" if config.get("account") and config.get("password") else "未完整填写")
        return
    if not targets and not config.get("direct_rooms"):
        log("还没有指定玩家，请在data/watch-config.json填写watch_names/watch_vs_top，或使用--name。")
        return
    log("独立观战后端：追踪玩家出现即观战，保证卡组观测及时；QQ排名条件由前端筛选推送和录像链接。")
    try:
        asyncio.run(serve(config, targets, []))
    except KeyboardInterrupt:
        log("已停止")


def run_pycharm_watch() -> None:
    if os.environ.get('PYCHARM_HOSTED') != '1' and os.environ.get('QQ_WATCH_IN_PYCHARM') != '1':
        raise RuntimeError('全部观战必须从PyCharm的watch运行配置启动。')
    # 作为脚本运行时与bot.watch_core共享同一个模块和所有全局状态。
    sys.modules['watch'] = sys.modules[__name__]
    from bot.launcher import startup_lock
    from bot.main import run_main
    with startup_lock(HERE, 'bot-runtime.lock') as acquired:
        if not acquired:
            log('已有watch控制的观战实例运行，请使用原来的PyCharm Run窗口。')
            return
        log('全部观战由当前PyCharm watch.py窗口控制；按停止后全部观战结束。')
        run_main(controlled_by_pycharm=True)


if __name__ == "__main__":
    main()
