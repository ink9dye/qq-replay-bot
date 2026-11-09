# -*- coding: utf-8 -*-
"""Side 推断缓存（SQLite 单条读写）。

每个构筑一行：build(主键) -> inferred_side JSON。批处理只 `get()` 需要的构筑，
不再把整份 171 MB JSON 载入内存（原先峰值 ~668 MB，13.9 s）。

对外接口保持与旧 JSON 版一致：load / stamp / commit / get / put / save。
旧 JSON 迁移见 tools/migrate_side_cache.py。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  build TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated REAL NOT NULL
)
"""


def db_path(data_dir):
    return Path(data_dir) / 'cache' / 'side-inference.sqlite3'


def json_path(data_dir):
    return Path(data_dir) / 'cache' / 'side-inference.json'


def connect(data_dir):
    path = db_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=5000')
    db.execute(SCHEMA)
    return db


def stamp(data_dir):
    """缓存文件的 (mtime_ns, size)；不存在返回 None（保留旧接口）。"""
    try:
        stat = db_path(data_dir).stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


class _View:
    """按需读取的只读视图：兼容旧 dict 的 .get() / len()。"""

    def __init__(self, data_dir):
        self._dir = data_dir

    def get(self, build):
        try:
            with connect(self._dir) as db:
                row = db.execute('SELECT payload FROM entries WHERE build=?',
                                 (str(build),)).fetchone()
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError, OSError):
            return None

    def __contains__(self, build):
        return self.get(build) is not None

    def __len__(self):
        try:
            with connect(self._dir) as db:
                return db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
        except (sqlite3.Error, OSError):
            return 0


def load(data_dir):
    return _View(data_dir)


def get(data_dir, build):
    return _View(data_dir).get(build)


def commit(data_dir, added, base=None, base_stamp=None, attempts=3):
    """把 added 合并进缓存（已有键不覆盖），返回 dict(status, inserted, entries)。"""
    added = added or {}
    if not added:
        return dict(status='nochange', inserted=0, entries=len(_View(data_dir)))
    now = time.time()
    try:
        rows = [(str(key), json.dumps(value, ensure_ascii=False), now)
                for key, value in added.items()]
        with connect(data_dir) as db:
            before = db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
            db.executemany('INSERT OR IGNORE INTO entries(build,payload,updated) VALUES(?,?,?)', rows)
            after = db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
        inserted = after - before
        return dict(status='ok' if inserted else 'nochange', inserted=inserted, entries=after)
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return dict(status='error', inserted=0, entries=None)


def put(data_dir, build, inferred):
    return commit(data_dir, {str(build): inferred})


def save(data_dir, cache):
    """整份写入（仅供迁移/测试）；生产走 commit()。"""
    return commit(data_dir, cache)
