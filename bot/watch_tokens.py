# -*- coding: utf-8 -*-
"""每设备观战接口令牌。

只把 SHA-256 哈希写进 SQLite，明文令牌只在签发时返回一次。令牌可以绑定
worker（客户端名称），支持过期和吊销；旧版 config.json 的 remote.token
仍然由 watch_api 作为兼容总令牌接受。
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

TOKEN_PREFIX = "wt_"
DEFAULT_HOURS = 720.0  # 30 天；remote.device_token_hours 可覆盖
LONG_TERM_HOURS = 24 * 365 * 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    token_hint TEXT NOT NULL,
    worker TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    revoked_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS watch_tokens_worker ON watch_tokens(worker);
CREATE INDEX IF NOT EXISTS watch_tokens_owner ON watch_tokens(owner);
CREATE INDEX IF NOT EXISTS watch_tokens_expires ON watch_tokens(expires_at);
"""


def hash_token(token: str) -> str:
    """令牌只以这个摘要入库；数据库泄露不能直接还原 Bearer 令牌。"""
    return hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()


def token_hint(token: str) -> str:
    """给管理员列表看的短标识，不包含完整令牌。"""
    text = str(token or "")
    if len(text) <= 12:
        return (text[:4] + "…") if text else ""
    return text[:7] + "…" + text[-4:]


class WatchTokenStore:
    """watch_tokens 表的唯一读写入口。

    collector（watch.py）用它验证令牌，QQ 进程用它签发/吊销。两个进程各开
    一条 SQLite 连接，WAL + busy_timeout 处理并发；所有方法都可跨线程调用。
    """

    def __init__(self, path, *, migrate: bool = False, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        # 自己控制事务，避免 sqlite3 的隐式 BEGIN 和 BEGIN IMMEDIATE 冲突。
        self._db.isolation_level = None
        self._db.execute("PRAGMA busy_timeout=5000")
        if migrate:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._lock:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- QQ 侧：签发/吊销 ------------------------------------------------
    def issue(self, worker: str, owner: str, *, hours=None, label: str = "",
              now=None, revoke_existing: bool = True) -> dict:
        """签发一个绑定 worker 的令牌；同一 worker 的旧活动令牌先吊销。

        返回的 dict 含明文 token，调用方只应把它发给请求者，不要再落盘/写日志。
        """
        worker = str(worker or "").strip()[:64]
        owner = str(owner or "").strip()[:64]
        if not worker:
            raise ValueError("worker_required")
        moment = float(self.clock() if now is None else now)
        try:
            duration = DEFAULT_HOURS if hours is None else float(hours)
        except (TypeError, ValueError):
            duration = DEFAULT_HOURS
        expires = moment + (LONG_TERM_HOURS if duration <= 0 else duration) * 3600.0
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if revoke_existing:
                    self._db.execute(
                        "UPDATE watch_tokens SET revoked_at=?, revoked_by='reissue' "
                        "WHERE worker=? AND revoked_at IS NULL AND expires_at>?",
                        (moment, worker, moment),
                    )
                token = ""
                for _ in range(5):
                    candidate = TOKEN_PREFIX + secrets.token_urlsafe(32)
                    try:
                        self._db.execute(
                            "INSERT INTO watch_tokens"
                            "(token_hash, token_hint, worker, owner, label, created_at, expires_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (hash_token(candidate), token_hint(candidate), worker, owner,
                             str(label or "")[:200], moment, expires),
                        )
                        token = candidate
                        break
                    except sqlite3.IntegrityError:
                        continue
                if not token:
                    raise RuntimeError("token_collision")
                self._db.execute("COMMIT")
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return dict(token=token, worker=worker, owner=owner, label=str(label or ""),
                    created_at=moment, expires_at=expires)

    def revoke_worker(self, worker: str, *, by: str = "", now=None) -> int:
        return self._revoke("worker=?", str(worker or "").strip()[:64], by=by, now=now)

    def revoke_owner(self, owner: str, *, by: str = "", now=None) -> int:
        return self._revoke("owner=?", str(owner or "").strip()[:64], by=by, now=now)

    def revoke_id(self, token_id: int, *, by: str = "", now=None) -> int:
        try:
            number = int(token_id)
        except (TypeError, ValueError):
            return 0
        return self._revoke("id=?", number, by=by, now=now)

    def _revoke(self, where: str, value, *, by: str = "", now=None) -> int:
        moment = float(self.clock() if now is None else now)
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                cur = self._db.execute(
                    "UPDATE watch_tokens SET revoked_at=?, revoked_by=? "
                    f"WHERE revoked_at IS NULL AND expires_at>? AND {where}",
                    (moment, str(by or "")[:64], moment, value),
                )
                count = int(cur.rowcount or 0)
                self._db.execute("COMMIT")
            except Exception:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return count

    # ---- collector 侧：验证 ---------------------------------------------
    def verify(self, token: str):
        """有效返回 {id, worker, owner, expires_at}；过期/吊销/未知返回 None。"""
        text = str(token or "").strip()
        if not text:
            return None
        moment = float(self.clock())
        with self._lock:
            row = self._db.execute(
                "SELECT id, worker, owner, expires_at FROM watch_tokens "
                "WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",
                (hash_token(text), moment),
            ).fetchone()
        if row is None:
            return None
        return dict(id=row["id"], worker=row["worker"], owner=row["owner"],
                    expires_at=row["expires_at"])

    # ---- 管理列表 -------------------------------------------------------
    def list_active(self, *, now=None, limit: int = 50) -> list[dict]:
        moment = float(self.clock() if now is None else now)
        try:
            size = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            size = 50
        with self._lock:
            rows = self._db.execute(
                "SELECT id, token_hint, worker, owner, label, created_at, expires_at "
                "FROM watch_tokens WHERE revoked_at IS NULL AND expires_at>? "
                "ORDER BY created_at DESC LIMIT ?",
                (moment, size),
            ).fetchall()
        return [dict(row) for row in rows]
