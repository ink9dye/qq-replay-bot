# -*- coding: utf-8 -*-
"""卡价预定：与 price-watch 共用 SQLite（只写 reservations 表，不碰其他表）。"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(os.environ.get(
    "PRICE_WATCH_DB",
    r"C:\Users\12783\Documents\ChatGPT\qq机器人\price-watch\data\prices.db",
))

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    rarity      TEXT NOT NULL DEFAULT '',
    language    TEXT NOT NULL DEFAULT '日文',
    target      REAL NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    notified_at TEXT,
    last_price  REAL,
    UNIQUE(user_id, name, rarity, language)
);
"""

LANGUAGES = ("日文", "简中")
RARITY_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{1,12}")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class PriceReservations:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path or DEFAULT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path, timeout=5)) as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(reservations)")}
            if "language" not in columns:
                conn.execute("ALTER TABLE reservations ADD COLUMN language TEXT NOT NULL DEFAULT '日文'")
            conn.commit()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, user_id, name: str, rarity: str, target, language: str = "日文") -> int:
        user_id = str(user_id).strip()
        name = (name or "").strip()
        rarity = (rarity or "").strip().upper()
        language = (language or "日文").strip()
        if not user_id.isdigit():
            raise ValueError("用户标识无效")
        if not name or len(name) > 40:
            raise ValueError("卡名不能为空，且不超过 40 字")
        if rarity and not RARITY_RE.fullmatch(rarity):
            raise ValueError("罕贵格式不对，例如 UTR、UR、SR")
        if language not in LANGUAGES:
            raise ValueError("语言只能写 日文 或 简中")
        try:
            target = float(target)
        except (TypeError, ValueError):
            raise ValueError("预期价格要写数字，例如 160")
        if not (0 < target <= 10000000):
            raise ValueError("预期价格要在 0 到 1000 万之间")
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT id FROM reservations WHERE user_id=? AND name=? AND rarity=? AND language=?",
                (user_id, name, rarity, language),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE reservations SET target=?, active=1, notified_at=NULL,"
                    " last_price=NULL WHERE id=?",
                    (target, row["id"]),
                )
                conn.commit()
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO reservations (user_id, name, rarity, language, target, active,"
                " created_at) VALUES (?,?,?,?,?,1,?)",
                (user_id, name, rarity, language, target, _now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_for(self, user_id):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT * FROM reservations WHERE user_id=? AND active=1 ORDER BY id",
                (str(user_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    def deactivate_others(self, user_id, keep_id: int) -> int:
        """只保留一条 active；用于普通用户的“一张卡”表面行为。"""
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "UPDATE reservations SET active=0 WHERE user_id=? AND active=1 AND id<>?",
                (str(user_id), int(keep_id)),
            )
            conn.commit()
            return int(cur.rowcount)

    def cancel(self, user_id, name: str, rarity: str = "", language: str = "") -> int:
        user_id = str(user_id).strip()
        name = (name or "").strip()
        rarity = (rarity or "").strip().upper()
        language = (language or "").strip()
        if not name:
            raise ValueError("卡名不能为空")
        if language and language not in LANGUAGES:
            raise ValueError("语言只能写 日文 或 简中")
        sql = "UPDATE reservations SET active=0 WHERE user_id=? AND name=? AND active=1"
        args = [user_id, name]
        if rarity:
            sql += " AND rarity=?"
            args.append(rarity)
        if language:
            sql += " AND language=?"
            args.append(language)
        with closing(self._conn()) as conn:
            cur = conn.execute(sql, args)
            conn.commit()
            return int(cur.rowcount)
