# -*- coding: utf-8 -*-
"""查询层磁盘预计算缓存（当前只用于构筑 build）。

设计：一个玩家一个 JSON 文件，原子写、权限 600；查询侧只读，脚本侧低峰写。
文件名用玩家名哈希，避免中文/长度/路径问题。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

CACHE_VERSION = 1
MISS = object()


def cache_root(data_dir) -> Path:
    return Path(data_dir) / "query-cache"


def cache_path(data_dir, player: str) -> Path:
    digest = hashlib.sha256(str(player).strip().encode("utf-8")).hexdigest()[:32]
    return cache_root(data_dir) / "build" / (digest + ".json")


def read_precomputed(data_dir, player: str, latest_replay_at: float = 0):
    """返回缓存 payload；不可用/过期/有更新录像时返回 MISS。"""
    if not data_dir:
        return MISS
    path = cache_path(data_dir, player)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MISS
    if not isinstance(doc, dict):
        return MISS
    if doc.get("version") != CACHE_VERSION or doc.get("player") != str(player):
        return MISS
    if "payload" not in doc:
        return MISS
    try:
        expires = float(doc.get("expires_at") or 0)
        cached_latest = float(doc.get("latest_replay_at") or 0)
    except (TypeError, ValueError):
        return MISS
    if expires <= time.time():
        return MISS
    if cached_latest < float(latest_replay_at or 0):
        return MISS
    return doc["payload"]


def write_precomputed(data_dir, player: str, payload, latest_replay_at: float = 0,
                      ttl_seconds: int = 86400) -> Path:
    path = cache_path(data_dir, player)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    doc = dict(
        version=CACHE_VERSION,
        group="build",
        player=str(player),
        fetched_at=now,
        expires_at=now + max(60, int(ttl_seconds)),
        latest_replay_at=float(latest_replay_at or 0),
        payload=payload,
    )
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def cleanup(data_dir, days: int = 7) -> int:
    """删除超过 days 天未更新的缓存文件；返回删除数。"""
    root = cache_root(data_dir) / "build"
    cutoff = time.time() - max(1, int(days)) * 86400
    removed = 0
    try:
        for path in root.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
