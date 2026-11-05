# -*- coding: utf-8 -*-
"""查询平台账号体系：一次性激活码 + 双令牌 + 管理操作。

设计定稿见交接文档 v5（qq-query-android-handoff-v5.md）：
- 激活码 = 一次性入网凭证，不是密码：用一次即报废，未使用 24h 过期，
  每用户至多一条待用码（重发即作废旧码）。全库不存任何密码。
- 双令牌：access 短（默认 24h）+ refresh 长（默认 180 天），每次刷新轮换
  （旧 refresh 作废）；refresh 只认未过期的 refresh 令牌，过期即重新登录。
- 登录失败统一报 bad_credentials（不区分码错/已用/过期，防用户名枚举）；
  失败锁定按「账号+IP」两个维度分别记。
- QQ 号即登录账号：query_users 以 qq_user_id 为主键，无 username。

线程模型：与 watch_api 的 ThreadingHTTPServer 共存，这里每次操作独立开
SQLite 连接（低频管理/登录操作，可接受），token 哈希用 SHA-256——码和令牌
都是 secrets.token_urlsafe(32) 级别的高熵随机值，防暴力应靠限流与锁定，
不需要慢哈希（慢哈希也无法按哈希反查，会迫保全表扫描）。
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# 错误约定：(error, http_status)。统一登录失败文案由 watch_api 层给出。
ERR_BAD_CREDENTIALS = ("bad_credentials", 401)
ERR_LOCKED = ("locked", 429)
ERR_REVOKED = ("revoked", 403)
ERR_EXISTS = ("already_provisioned", 409)
ERR_NOT_FOUND = ("not_found", 404)

# 登录按 IP 限流（内存即可，重启清零无妨）：窗口内最多 N 次。
IP_WINDOW_SECONDS = 300.0
IP_WINDOW_MAX = 10


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class QueryAuth:
    """查询账号/令牌的签发、校验与管理。collector 进程是本库唯一写者。"""

    def __init__(self, db_path, *, token_hours: float = 24.0, refresh_days: float = 180.0,
                 code_hours: float = 24.0, lock_after_failures: int = 5, lock_minutes: float = 15.0,
                 clock=time.time, log=None):
        self.db_path = Path(db_path)
        self.token_hours = float(token_hours)
        self.refresh_days = float(refresh_days)
        self.code_hours = float(code_hours)
        self.lock_after_failures = max(1, int(lock_after_failures))
        self.lock_minutes = float(lock_minutes)
        if any(not math.isfinite(v) or v <= 0 for v in (self.token_hours, self.refresh_days, self.code_hours, self.lock_minutes)):
            raise ValueError("invalid_query_lifetime")
        self.clock = clock
        self.log = log or (lambda *_: None)
        self._ip_events: dict[str, list[float]] = {}  # 登录按 IP 限流的滑动窗口
        self._ip_lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ---- 连接与表 -------------------------------------------------------
    @contextmanager
    def _db(self, write=False):
        """每次操作独立连接：事务提交/回滚后必须关闭，否则 Windows 下文件锁
        会让临时目录清理失败（sqlite3 的 `with connection` 只管事务不关连接）。"""
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def _ensure_schema(self) -> None:
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS query_users (
                qq_user_id TEXT PRIMARY KEY,
                role       TEXT NOT NULL DEFAULT 'user',
                writes     INTEGER NOT NULL DEFAULT 0,
                status     TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                revoked_at REAL
            );
            CREATE TABLE IF NOT EXISTS query_codes (
                qq_user_id TEXT PRIMARY KEY,
                code_hash  TEXT NOT NULL,
                issued_at  REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS query_tokens (
                token_hash         TEXT PRIMARY KEY,
                refresh_hash       TEXT NOT NULL UNIQUE,
                qq_user_id         TEXT NOT NULL,
                device             TEXT NOT NULL DEFAULT '',
                issued_at          REAL NOT NULL,
                access_expires_at  REAL NOT NULL,
                refresh_expires_at REAL NOT NULL,
                revoked_at         REAL
            );
            CREATE TABLE IF NOT EXISTS query_failures (
                key          TEXT PRIMARY KEY,
                count        INTEGER NOT NULL DEFAULT 0,
                locked_until REAL NOT NULL DEFAULT 0,
                last_at      REAL NOT NULL DEFAULT 0
            );
            """)

    # ---- 内部工具 -------------------------------------------------------
    def _mark_failure(self, db, qq_user_id: str, ip: str, now: float) -> None:
        """登录失败：账号+IP组合与来源IP两个维度分别累计，达到阈值各自锁定。"""
        for key in (f"user-ip:{qq_user_id}:{ip or 'unknown'}", f"ip:{ip or 'unknown'}"):
            db.execute(
                "INSERT INTO query_failures(key,count,locked_until,last_at) VALUES(?,1,0,?) "
                "ON CONFLICT(key) DO UPDATE SET count=count+1, last_at=excluded.last_at",
                (key, now))
            row = db.execute("SELECT count FROM query_failures WHERE key=?", (key,)).fetchone()
            if row and row["count"] >= self.lock_after_failures:
                db.execute("UPDATE query_failures SET locked_until=? WHERE key=?",
                           (now + self.lock_minutes * 60, key))

    def _locked(self, db, qq_user_id: str, ip: str, now: float) -> bool:
        for key in (f"user-ip:{qq_user_id}:{ip or 'unknown'}", f"ip:{ip or 'unknown'}"):
            row = db.execute("SELECT locked_until FROM query_failures WHERE key=?", (key,)).fetchone()
            if row and now < row["locked_until"]:
                return True
        return False

    def _clear_failures(self, db, qq_user_id: str, ip: str) -> None:
        db.execute("DELETE FROM query_failures WHERE key IN (?, ?)",
                   (f"user-ip:{qq_user_id}:{ip or 'unknown'}", f"ip:{ip or 'unknown'}"))

    def _ip_window_allow(self, now: float, ip: str = "") -> bool:
        with self._ip_lock:
            self._ip_events = {key: [t for t in times if now-t <= IP_WINDOW_SECONDS]
                               for key, times in self._ip_events.items()
                               if times and now-times[-1] <= IP_WINDOW_SECONDS}
            key = str(ip or "unknown")
            if key not in self._ip_events and len(self._ip_events) >= 1024:
                return False
            events = self._ip_events.setdefault(key, [])
            if len(events) >= IP_WINDOW_MAX:
                return False
            events.append(now)
            return True

    def _issue_tokens(self, db, qq_user_id: str, device: str, now: float) -> dict:
        """生成一对新令牌并入库（只存哈希）。"""
        token, refresh_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        access_exp = now + self.token_hours * 3600
        refresh_exp = now + self.refresh_days * 86400
        db.execute(
            "INSERT INTO query_tokens(token_hash,refresh_hash,qq_user_id,device,issued_at,"
            "access_expires_at,refresh_expires_at) VALUES(?,?,?,?,?,?,?)",
            (_sha256(token), _sha256(refresh_token), qq_user_id, str(device or "")[:64], now,
             access_exp, refresh_exp))
        return dict(token=token, refresh_token=refresh_token,
                    access_expires_at=int(access_exp), refresh_expires_at=int(refresh_exp))

    @staticmethod
    def _user_payload(row) -> dict:
        return dict(identity=row["qq_user_id"], role=row["role"], writes=bool(row["writes"]))

    def _live_user(self, db, qq_user_id: str):
        return db.execute(
            "SELECT * FROM query_users WHERE qq_user_id=? AND status='active' AND revoked_at IS NULL",
            (str(qq_user_id),)).fetchone()

    # ---- 发放（QQ「开通查询」与管理界面共用） ----------------------------
    def provision(self, qq_user_id: str, device: str = "") -> dict:
        """白名单自助开通：新账号 + 一次性码。已开通返回 ERR_EXISTS。"""
        now, qq_user_id = self.clock(), str(qq_user_id)
        if not re.fullmatch(r"[0-9]{5,20}", qq_user_id):
            return dict(ok=False, error="bad_identity", status=400)
        code = secrets.token_urlsafe(16)
        with self._db(write=True) as db:
            if db.execute("SELECT 1 FROM query_users WHERE qq_user_id=?", (qq_user_id,)).fetchone():
                return dict(ok=False, error=ERR_EXISTS[0], status=ERR_EXISTS[1])
            db.execute("INSERT INTO query_users(qq_user_id,created_at) VALUES(?,?)", (qq_user_id, now))
            db.execute("INSERT INTO query_codes(qq_user_id,code_hash,issued_at,expires_at) VALUES(?,?,?,?)",
                       (qq_user_id, _sha256(code), now, now + self.code_hours * 3600))
        self.log(f"query-auth: 开通账号 identity={qq_user_id}")
        return dict(ok=True, identity=qq_user_id, code=code, code_expires_at=int(now + self.code_hours * 3600))

    def reissue_code(self, qq_user_id: str) -> dict:
        """管理员给已有账号重发一次性码（作废旧待用码，不动已有令牌）。"""
        now, qq_user_id = self.clock(), str(qq_user_id)
        code = secrets.token_urlsafe(16)
        with self._db(write=True) as db:
            if not self._live_user(db, qq_user_id):
                return dict(ok=False, error=ERR_NOT_FOUND[0], status=ERR_NOT_FOUND[1])
            db.execute("INSERT INTO query_codes(qq_user_id,code_hash,issued_at,expires_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(qq_user_id) DO UPDATE SET code_hash=excluded.code_hash,"
                       "issued_at=excluded.issued_at, expires_at=excluded.expires_at",
                       (qq_user_id, _sha256(code), now, now + self.code_hours * 3600))
        self.log(f"query-auth: 重发激活码 identity={qq_user_id}")
        return dict(ok=True, identity=qq_user_id, code=code, code_expires_at=int(now + self.code_hours * 3600))

    # ---- 登录 / 刷新 / 登出 ---------------------------------------------
    def login(self, qq_user_id: str, code: str, ip: str = "") -> dict:
        """一次性码换双令牌。码错/已用/过期一律 bad_credentials；命中锁定报 locked。"""
        now, qq_user_id = self.clock(), str(qq_user_id)
        if not re.fullmatch(r"[0-9]{5,20}", qq_user_id):
            return dict(ok=False, error=ERR_BAD_CREDENTIALS[0], status=ERR_BAD_CREDENTIALS[1])
        if not self._ip_window_allow(now, ip):
            return dict(ok=False, error=ERR_LOCKED[0], status=ERR_LOCKED[1])
        with self._db(write=True) as db:
            if self._locked(db, qq_user_id, ip, now):
                return dict(ok=False, error=ERR_LOCKED[0], status=ERR_LOCKED[1])
            user = self._live_user(db, qq_user_id)
            row = db.execute("SELECT * FROM query_codes WHERE qq_user_id=?", (qq_user_id,)).fetchone()
            if (user is None or row is None or now >= row["expires_at"]
                    or not hmac.compare_digest(row["code_hash"], _sha256(str(code or "")))):
                self._mark_failure(db, qq_user_id, ip, now)
                return dict(ok=False, error=ERR_BAD_CREDENTIALS[0], status=ERR_BAD_CREDENTIALS[1])
            # 码用一次即报废（v5 定稿）；登录成功清除该账号失败计数。
            db.execute("DELETE FROM query_codes WHERE qq_user_id=?", (qq_user_id,))
            self._clear_failures(db, qq_user_id, ip)
            tokens = self._issue_tokens(db, qq_user_id, ip, now)
        self.log(f"query-auth: 登录成功 identity={qq_user_id}")
        return dict(ok=True, **tokens, **self._user_payload(user))

    def refresh(self, refresh_token: str, ip: str = "") -> dict:
        """用 refresh 令牌换新的一对（轮换：旧令牌立即作废）。"""
        now = self.clock()
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM query_tokens WHERE refresh_hash=?",
                             (_sha256(str(refresh_token or "")),)).fetchone()
            if row is None or row["revoked_at"] is not None or now >= row["refresh_expires_at"]:
                return dict(ok=False, error=ERR_BAD_CREDENTIALS[0], status=ERR_BAD_CREDENTIALS[1])
            user = self._live_user(db, row["qq_user_id"])
            if user is None:
                return dict(ok=False, error=ERR_REVOKED[0], status=ERR_REVOKED[1])
            db.execute("UPDATE query_tokens SET revoked_at=? WHERE token_hash=?",
                       (now, row["token_hash"]))  # 轮换：旧的一对作废
            tokens = self._issue_tokens(db, row["qq_user_id"], row["device"], now)
        return dict(ok=True, **tokens, **self._user_payload(user))

    def authenticate(self, token: str) -> dict | None:
        """校验 access 令牌，返回身份信息；无效/过期/吊销/账号停用返回 None。"""
        now = self.clock()
        with self._db() as db:
            row = db.execute(
                "SELECT t.*, u.role, u.writes, u.status, u.revoked_at AS user_revoked "
                "FROM query_tokens t JOIN query_users u ON u.qq_user_id=t.qq_user_id "
                "WHERE t.token_hash=?", (_sha256(str(token or "")),)).fetchone()
            if row is None or row["revoked_at"] is not None or now >= row["access_expires_at"]:
                return None
            if row["status"] != "active" or row["user_revoked"] is not None:
                return None
            return dict(identity=row["qq_user_id"], role=row["role"], writes=bool(row["writes"]),
                        device=row["device"])

    def logout(self, token: str) -> bool:
        """吊销该设备的一对令牌（不动其他设备）。"""
        now = self.clock()
        with self._db(write=True) as db:
            cur = db.execute("UPDATE query_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                             (now, _sha256(str(token or ""))))
            return cur.rowcount > 0

    # ---- 管理操作（管理界面 / CLI 共用） ---------------------------------
    def reset_user(self, qq_user_id: str) -> dict:
        """重置：作废该账号全部令牌 + 发新一次性码（旧码作废）。"""
        now, qq_user_id = self.clock(), str(qq_user_id)
        code = secrets.token_urlsafe(16)
        with self._db(write=True) as db:
            if not self._live_user(db, qq_user_id):
                return dict(ok=False, error=ERR_NOT_FOUND[0], status=ERR_NOT_FOUND[1])
            db.execute("UPDATE query_tokens SET revoked_at=? WHERE qq_user_id=? AND revoked_at IS NULL",
                       (now, qq_user_id))
            db.execute("INSERT INTO query_codes(qq_user_id,code_hash,issued_at,expires_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(qq_user_id) DO UPDATE SET code_hash=excluded.code_hash,"
                       "issued_at=excluded.issued_at, expires_at=excluded.expires_at",
                       (qq_user_id, _sha256(code), now, now + self.code_hours * 3600))
        self.log(f"query-auth: 重置账号 identity={qq_user_id}")
        return dict(ok=True, identity=qq_user_id, code=code, code_expires_at=int(now + self.code_hours * 3600))

    def disable_user(self, qq_user_id: str) -> dict:
        """注销：停用账号并作废全部令牌。"""
        now, qq_user_id = self.clock(), str(qq_user_id)
        with self._db(write=True) as db:
            cur = db.execute("UPDATE query_users SET status='disabled', revoked_at=? "
                             "WHERE qq_user_id=? AND revoked_at IS NULL", (now, qq_user_id))
            if not cur.rowcount:
                return dict(ok=False, error=ERR_NOT_FOUND[0], status=ERR_NOT_FOUND[1])
            db.execute("UPDATE query_tokens SET revoked_at=? WHERE qq_user_id=? AND revoked_at IS NULL",
                       (now, qq_user_id))
        self.log(f"query-auth: 注销账号 identity={qq_user_id}")
        return dict(ok=True, identity=qq_user_id)

    def set_writes(self, qq_user_id: str, enabled: bool) -> dict:
        """写权限开关。权限每请求查库，改完即时生效，无需换令牌。"""
        qq_user_id = str(qq_user_id)
        with self._db(write=True) as db:
            cur = db.execute("UPDATE query_users SET writes=? WHERE qq_user_id=? AND status='active'",
                             (1 if enabled else 0, qq_user_id))
            if not cur.rowcount:
                return dict(ok=False, error=ERR_NOT_FOUND[0], status=ERR_NOT_FOUND[1])
        self.log(f"query-auth: 写权限 identity={qq_user_id} -> {'开' if enabled else '关'}")
        return dict(ok=True, identity=qq_user_id, writes=bool(enabled))

    def revoke_device(self, token_hash_prefix: str) -> dict:
        """按令牌哈希前 8 位吊销单个设备会话（管理界面列表里给的就是前 8 位）。"""
        prefix = str(token_hash_prefix or "").strip()
        if not prefix.isalnum() or not 8 <= len(prefix) <= 64:
            return dict(ok=False, error="bad_token_id", status=400)
        now = self.clock()
        with self._db(write=True) as db:
            rows = db.execute("SELECT token_hash FROM query_tokens WHERE revoked_at IS NULL").fetchall()
            hit = [r["token_hash"] for r in rows if r["token_hash"].startswith(prefix)]
            if len(hit) != 1:
                return dict(ok=False, error=ERR_NOT_FOUND[0], status=ERR_NOT_FOUND[1])
            db.execute("UPDATE query_tokens SET revoked_at=? WHERE token_hash=?", (now, hit[0]))
        return dict(ok=True)

    def unlock(self, key: str) -> dict:
        """清除失败锁定（key 形如 user:12345 或 ip:1.2.3.4）。"""
        with self._db(write=True) as db:
            db.execute("DELETE FROM query_failures WHERE key=?", (str(key or "").strip(),))
        return dict(ok=True)

    def list_users(self) -> list[dict]:
        """管理界面账号列表。绝不含令牌/激活码明文。"""
        now = self.clock()
        with self._db() as db:
            users = db.execute("SELECT * FROM query_users ORDER BY created_at").fetchall()
            out = []
            for u in users:
                tokens = db.execute(
                    "SELECT token_hash,device,issued_at,access_expires_at,refresh_expires_at "
                    "FROM query_tokens WHERE qq_user_id=? AND revoked_at IS NULL", (u["qq_user_id"],)).fetchall()
                out.append(dict(
                    identity=u["qq_user_id"], role=u["role"], writes=bool(u["writes"]),
                    status=u["status"], created_at=int(u["created_at"]),
                    pending_code=bool(db.execute("SELECT 1 FROM query_codes WHERE qq_user_id=? AND expires_at>?",
                                                 (u["qq_user_id"], now)).fetchone()),
                    devices=[dict(token_id=r["token_hash"][:8], device=r["device"], issued_at=int(r["issued_at"]),
                                  access_expires_at=int(r["access_expires_at"]),
                                  refresh_expires_at=int(r["refresh_expires_at"])) for r in tokens]))
            return out

    def list_locks(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM query_failures WHERE locked_until>? ORDER BY locked_until",
                              (self.clock(),)).fetchall()
            return [dict(key=r["key"], count=r["count"], locked_until=int(r["locked_until"])) for r in rows]
