# -*- coding: utf-8 -*-
"""订阅、录像索引、通知进度和分享链接都放在本项目的 SQLite 里。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


def _sub_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "player": row["player"],
        "rank_top": row["rank_top"],
        "notify": bool(row["notify"]),
        "created_at": row["created_at"],
    }


def _replay_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "digest": row["digest"],
        "focus_player": row["focus_player"],
        "rel_path": row["rel_path"],
        "players": json.loads(row["players_json"]),
        "ranks": json.loads(row["ranks_json"]),
        "recorded_at": row["recorded_at"],
        "saved_at": row["saved_at"] if row["saved_at"] is not None else row["recorded_at"],
    }


class Store:
    def __init__(self, path: Path, readonly: bool = False, migrate: bool = True):
        """readonly=True 只开只读连接（mode=ro + query_only），migrate=False 不跑建表/ALTER。

        非 collector 进程（独立 share、只读统计）用 Store(path, readonly=True, migrate=False)：
        不产生任何 DDL，schema 由 collector 唯一升级（见 决策与阻塞 D26/D27）。
        """
        self.path = Path(path)
        self.readonly = bool(readonly)
        self.migrate = bool(migrate) and not self.readonly
        self._lock = threading.Lock()
        if self.readonly:
            uri = self.path.resolve().as_uri() + '?mode=ro'
            self._db = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
            self._db.row_factory = sqlite3.Row
            self._db.execute('PRAGMA query_only=ON')
            self._db.execute('PRAGMA busy_timeout=5000')
            self._db.execute('BEGIN')
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA foreign_keys=ON')
        # 多进程共用同一个库：collector 与 QQ 各开连接，不是单写者。
        # WAL 允许读写并行，busy_timeout 让偶发写锁最多等 5 秒；M3 才把 QQ 的写请求收敛到 collector。
        self._db.execute("PRAGMA journal_mode=WAL")
        # 2026-11-06：统计扫描会长时间占写锁，5 秒超时会让 top100 刷新抛 database is locked 并打死 collector；放宽到 30 秒。
        self._db.execute("PRAGMA busy_timeout=30000")
        # WAL 曾被长读事务拖到 10.5GB；reset 时把文件截到 128MB，避免长期占盘。
        self._db.execute("PRAGMA journal_size_limit=134217728")
        if not self.migrate:
            self._commit()
            return
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                player TEXT NOT NULL,
                rank_top INTEGER,
                notify INTEGER NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(user_id, player)
            );
            CREATE TABLE IF NOT EXISTS qq_bindings (
                user_id TEXT PRIMARY KEY,
                player TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS replays (
                id INTEGER PRIMARY KEY,
                digest TEXT NOT NULL,
                focus_player TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                players_json TEXT NOT NULL,
                ranks_json TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                saved_at REAL,
                UNIQUE(digest, focus_player)
            );
            CREATE TABLE IF NOT EXISTS notifications (
                user_id TEXT NOT NULL,
                replay_id INTEGER NOT NULL,
                player TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, replay_id)
            );
            CREATE TABLE IF NOT EXISTS replay_sessions (
                replay_id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                game_no INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS share_links (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                player TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                replay_ids_json TEXT NOT NULL,
                range_start REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stats_links (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
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
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(replays)")}
        if "saved_at" not in columns:
            self._db.execute("ALTER TABLE replays ADD COLUMN saved_at REAL")
        link_columns = {row[1] for row in self._db.execute('PRAGMA table_info(share_links)')}
        if 'deck_replay_ids_json' not in link_columns:
            self._db.execute('ALTER TABLE share_links ADD COLUMN deck_replay_ids_json TEXT')
        if 'revoked_at' not in link_columns:
            self._db.execute('ALTER TABLE share_links ADD COLUMN revoked_at REAL')
        stats_columns = {row[1] for row in self._db.execute('PRAGMA table_info(stats_links)')}
        if 'revoked_at' not in stats_columns:
            self._db.execute('ALTER TABLE stats_links ADD COLUMN revoked_at REAL')
        self._commit()

        from .statistics_facts import initialize
        initialize(self._db)
        from .top100 import initialize as initialize_top100
        initialize_top100(self._db)
        from .build_classification import initialize as initialize_classifications
        initialize_classifications(self._db)
        self._db.execute('CREATE INDEX IF NOT EXISTS stat_card_lookup ON stat_card_facts(code,partition,digest,player)')
        self._db.execute('CREATE INDEX IF NOT EXISTS stat_build_card_lookup ON stat_build_cards(code,partition,build)')
        self._commit()

    def _commit(self) -> None:
        """写方法统一的提交口：事务内（atomic）只延后，不各自 commit。"""
        if getattr(self, '_defer_commit', False):
            return
        self._db.commit()

    @contextmanager
    def atomic(self):
        """同一连接上的显式写事务：业务写 + 幂等记录一起提交/回滚（M3 collector 用）。

        只读连接或嵌套调用直接报错；退出时异常→rollback，正常→一次 commit。
        """
        if self.readonly:
            raise RuntimeError('readonly_store')
        if getattr(self, '_defer_commit', False):
            raise RuntimeError('nested_atomic')
        self._db.execute('BEGIN IMMEDIATE')
        self._defer_commit = True
        try:
            yield self
        except Exception:
            self._defer_commit = False
            self._db.rollback()
            raise
        else:
            self._defer_commit = False
            self._db.commit()

    def schema_version(self) -> int:
        """schema 版本（PRAGMA user_version）：collector 唯一升级，只读进程用它做版本门。"""
        return int(self._db.execute('PRAGMA user_version').fetchone()[0])

    def set_schema_version(self, version: int) -> None:
        if self.readonly:
            raise RuntimeError('readonly_store')
        self._db.execute(f'PRAGMA user_version={int(version)}')
        self._commit()

    def card_statistics(self, replay_root, code, partition, now, filters=None, **options):
        from .card_statistics import query
        with self._lock:
            self._db.execute('BEGIN')
            try:
                return query(self._db,replay_root,code,partition,now,filters,**options)
            finally:self._db.rollback()

    def player_card_statistics(self,replay_root,code,now,deck_types=(),series=()):
        from .player_statistics import query
        with self._lock:
            self._db.execute('BEGIN')
            try:return query(self._db,replay_root,code,now,deck_types,series)
            finally:self._db.rollback()

    def set_fact_exclusion(self, digest, player='', scope='default', reason=None):
        with self._lock,self._db:
            if reason is None:
                self._db.execute('DELETE FROM stat_exclusions WHERE digest=? AND player=? AND scope=?',(digest,player,scope))
            else:
                self._db.execute('INSERT INTO stat_exclusions VALUES(?,?,?,?) ON CONFLICT(digest,player,scope) DO UPDATE SET reason=excluded.reason',(digest,player,scope,str(reason)))

    def classify_build(self, build, deck_type, series, version):
        from .build_classification import assign
        with self._lock,self._db:
            return assign(self._db,build,deck_type,series,version)

    def refresh_classifications(self, reference_path, overrides_path=None):
        raise RuntimeError('legacy_similarity_naming_removed; use refresh_series_names')

    def refresh_series_names(self, config, now, replay_root=None, scope='latest'):
        """按组合规则增量命名；scop='latest' 用每人最新一场的 game1 构筑。"""
        from .series_naming import apply_names
        with self._lock,self._db:
            return apply_names(self._db,now,config,scope=scope,replay_root=replay_root,only_pending=True)

    def pending_series_count(self, config, now, replay_root=None):
        from .series_naming import pending_latest_main,version_for
        with self._lock:
            return len(pending_latest_main(self._db,replay_root,now,
                                           version_for(config),config['respect_manual']))

    def unknown_builds(self, now):
        # 维护队列，不是按人的卡组使用人数或胜率统计。
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT b.* FROM stat_builds b WHERE b.classification_status='unknown' AND EXISTS(SELECT 1 FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE v.build=b.fingerprint AND g.expires_at>?) ORDER BY b.fingerprint",(float(now),))]

    def replace_top100(self, payload, now):
        from .top100 import replace
        with self._lock, self._db:
            return replace(self._db,payload,now)

    def top100_failed(self, now, error):
        from .top100 import failed
        with self._lock, self._db:
            failed(self._db,now,error)

    def top100_snapshot(self, now):
        from .top100 import snapshot
        with self._lock:
            return snapshot(self._db,now)

    def current_top100_views(self, replay_root, now, internal_only=False, scope='default'):
        from .top100 import eligible_views
        with self._lock:
            # 同一次查询固定成功名单版本，外部连接刷新不能造成半新半旧结果。
            self._db.execute('BEGIN')
            try:
                return eligible_views(self._db,replay_root,now,internal_only,scope)
            finally:
                self._db.rollback()

    def import_replay_facts(self, replay: dict, path: Path, report=None, now=None) -> bool:
        from .statistics_facts import import_replay
        with self._lock, self._db:
            return import_replay(self._db, replay, path, report, now)

    def fact_games(self, replay_root: Path, now: float) -> list[dict]:
        from .statistics_facts import visible_games
        with self._lock:
            return visible_games(self._db, replay_root, now)

    def fact_details(self, digest: str, replay_root: Path, now: float):
        with self._lock:
            from .share import safe_replay_file
            game = self._db.execute('SELECT * FROM stat_games WHERE digest=? AND expires_at>?', (digest, float(now))).fetchone()
            if game is None:
                return None
            sources = self._db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f ON r.id=f.replay_id WHERE f.digest=?', (digest,)).fetchall()
            if not any(safe_replay_file(replay_root, r['rel_path']) for r in sources):
                return None
            views = [dict(r) for r in self._db.execute('SELECT * FROM stat_views WHERE digest=? ORDER BY player_index', (digest,))]
            cards = [dict(r) for r in self._db.execute('SELECT * FROM stat_card_facts WHERE digest=? ORDER BY player,partition,code', (digest,))]
            evidence = [dict(r) for r in self._db.execute('SELECT * FROM stat_evidence WHERE digest=?', (digest,))]
            return dict(digest=digest, views=views, cards=cards, evidence=evidence)

    def purge_expired_facts(self, now: float) -> int:
        from .statistics_facts import purge
        with self._lock, self._db:
            return purge(self._db, now)

    def close(self) -> None:
        with self._lock:
            if self._db is None:
                return
            self._db.close()
            self._db = None

    def session_map(self, player: str) -> dict:
        with self._lock:
            rows = self._db.execute('SELECT s.* FROM replay_sessions s JOIN replays r ON r.id=s.replay_id '
                                    'WHERE r.focus_player=?', (player,)).fetchall()
        return {row['replay_id']: dict(row) for row in rows}

    def record_session(self, session_id: str, games: list[tuple[int, str]]) -> None:
        """录像哈希在多位玩家目录中各有索引；重连时沿用已知的整场编号。"""
        with self._lock:
            resolved = {}
            for game_no, digest in games:
                rows = self._db.execute('SELECT r.id,r.focus_player,s.session_id FROM replays r LEFT JOIN replay_sessions s '
                                        'ON s.replay_id=r.id WHERE r.digest=?', (digest,)).fetchall()
                for row in rows:
                    resolved.setdefault(row['focus_player'], []).append((row['id'], int(game_no), row['session_id']))
            for entries in resolved.values():
                known = {existing for _, _, existing in entries if existing}
                if len(known) > 1:
                    continue  # 不因一次不明重连把同一玩家的两场比赛混为一场。
                chosen = next(iter(known)) if known else session_id
                self._db.executemany('INSERT OR IGNORE INTO replay_sessions(replay_id,session_id,game_no) VALUES (?,?,?)',
                                     [(replay_id, chosen, number) for replay_id, number, _ in entries])
            self._commit()

    def recover_session(self, session_id: str, games: list[tuple[int, int]]) -> None:
        with self._lock:
            self._db.executemany('INSERT OR IGNORE INTO replay_sessions(replay_id,session_id,game_no) VALUES (?,?,?)',
                                 [(replay_id, session_id, number) for replay_id, number in games])
            self._commit()

    def upsert_subscription(
        self,
        user_id: str,
        player: str,
        rank_top: Optional[int],
        notify: bool,
        created_at: float,
    ) -> dict:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO subscriptions (user_id, player, rank_top, notify, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, player) DO UPDATE SET
                    rank_top = excluded.rank_top,
                    notify = excluded.notify
                """,
                (str(user_id), player, rank_top, 1 if notify else 0, float(created_at)),
            )
            self._commit()
        return self.get_subscription(user_id, player)

    def get_subscription(self, user_id: str, player: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM subscriptions WHERE user_id = ? AND player = ?",
                (str(user_id), player),
            ).fetchone()
        return _sub_row(row) if row else None

    def list_subscriptions(self, user_id: Optional[str] = None) -> list[dict]:
        with self._lock:
            if user_id is None:
                rows = self._db.execute(
                    "SELECT * FROM subscriptions ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY created_at, id",
                    (str(user_id),),
                ).fetchall()
        return [_sub_row(row) for row in rows]

    def delete_subscription(self, user_id: str, player: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND player = ?",
                (str(user_id), player),
            )
            self._commit()
            return cur.rowcount > 0

    def set_notify(self, user_id: str, player: str, notify: bool) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE subscriptions SET notify = ? WHERE user_id = ? AND player = ?",
                (1 if notify else 0, str(user_id), player),
            )
            self._commit()
            return cur.rowcount > 0

    def get_binding(self, user_id: str) -> Optional[str]:
        """QQ 号绑定的 MC 玩家名；未绑定返回 None。"""
        with self._lock:
            row = self._db.execute(
                "SELECT player FROM qq_bindings WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        return row["player"] if row else None

    def set_binding(self, user_id: str, player: str, updated_at: float) -> None:
        """绑定/改绑；一个 QQ 号只保留一个当前玩家名。"""
        with self._lock:
            self._db.execute(
                """
                INSERT INTO qq_bindings (user_id, player, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    player = excluded.player,
                    updated_at = excluded.updated_at
                """,
                (str(user_id), player, float(updated_at)),
            )
            self._commit()

    def delete_binding(self, user_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM qq_bindings WHERE user_id = ?",
                (str(user_id),),
            )
            self._commit()
            return cur.rowcount > 0

    def has_player(self, player: str) -> bool:
        """最近收录的统计里是否有该玩家名；只用于绑定后的提示，不作为拒绝依据。"""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM stat_views WHERE player = ? OR TRIM(player) = ? LIMIT 1",
                (player, player),
            ).fetchone()
        return row is not None

    def player_names(self) -> list[str]:
        """最近录像/统计/已有订阅里出现过的玩家名，用于空格写法归一。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT player FROM stat_views "
                "UNION ALL SELECT focus_player FROM replays "
                "UNION ALL SELECT player FROM subscriptions"
            ).fetchall()
        return sorted({str(row[0]) for row in rows if row[0]})

    def add_replay(
        self,
        digest: str,
        focus_player: str,
        rel_path: str,
        players: list[str],
        ranks: dict,
        recorded_at: float,
        saved_at: Optional[float] = None,
    ) -> tuple[dict, bool]:
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM replays WHERE digest = ? AND focus_player = ?",
                (digest, focus_player),
            ).fetchone()
            if existing:
                return _replay_row(existing), False
            cur = self._db.execute(
                """
                INSERT INTO replays
                    (digest, focus_player, rel_path, players_json, ranks_json, recorded_at, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    digest,
                    focus_player,
                    rel_path,
                    json.dumps(players, ensure_ascii=False),
                    json.dumps(ranks, ensure_ascii=False),
                    float(recorded_at),
                    float(recorded_at if saved_at is None else saved_at),
                ),
            )
            self._commit()
            row = self._db.execute("SELECT * FROM replays WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _replay_row(row), True

    def get_replay(self, replay_id: int) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM replays WHERE id = ?", (int(replay_id),)).fetchone()
        return _replay_row(row) if row else None

    def replays_for_player(self, player: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM replays WHERE focus_player = ? ORDER BY recorded_at, id",
                (player,),
            ).fetchall()
        return [_replay_row(row) for row in rows]

    def all_replays(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM replays ORDER BY recorded_at, id").fetchall()
        return [_replay_row(row) for row in rows]

    def legacy_replays(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, rel_path, recorded_at FROM replays WHERE saved_at IS NULL"
            ).fetchall()
        return [dict(row) for row in rows]

    def backfill_saved_times(self, rows: list[tuple[float, int]]) -> None:
        with self._lock:
            self._db.executemany(
                "UPDATE replays SET saved_at = ? WHERE id = ? AND saved_at IS NULL", rows
            )
            self._commit()

    def replays_older_than(self, before: float) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM replays WHERE saved_at <= ? ORDER BY saved_at, id",
                (float(before),),
            ).fetchall()
        return [_replay_row(row) for row in rows]

    def delete_replay(self, replay_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM replay_sessions WHERE replay_id = ?", (int(replay_id),))
            self._db.execute("DELETE FROM notifications WHERE replay_id = ?", (int(replay_id),))
            self._db.execute("DELETE FROM replays WHERE id = ?", (int(replay_id),))
            self._commit()

    def enqueue_notification(self, user_id: str, replay_id: int, player: str) -> bool:
        with self._lock:
            existing = self._db.execute(
                "SELECT status FROM notifications WHERE user_id = ? AND replay_id = ?",
                (str(user_id), int(replay_id)),
            ).fetchone()
            if existing:
                return False
            self._db.execute(
                """
                INSERT INTO notifications (user_id, replay_id, player, status, attempts)
                VALUES (?, ?, ?, 'pending', 0)
                """,
                (str(user_id), int(replay_id), player),
            )
            self._commit()
            return True

    def pending_notifications(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT user_id, replay_id, player, status, attempts
                FROM notifications
                WHERE status = 'pending'
                ORDER BY replay_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, user_id: str, replay_id: int) -> None:
        with self._lock:
            self._db.execute(
                """
                UPDATE notifications
                SET status = 'sent', attempts = attempts + 1
                WHERE user_id = ? AND replay_id = ?
                """,
                (str(user_id), int(replay_id)),
            )
            self._commit()

    def mark_skipped(self, user_id: str, replay_id: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE notifications SET status = 'skipped' WHERE user_id = ? AND replay_id = ?",
                (str(user_id), int(replay_id)),
            )
            self._commit()

    def mark_retry(self, user_id: str, replay_id: int, limit: int = 3) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT attempts FROM notifications WHERE user_id = ? AND replay_id = ?",
                (str(user_id), int(replay_id)),
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            status = "failed" if attempts >= limit else "pending"
            self._db.execute(
                """
                UPDATE notifications SET attempts = ?, status = ?
                WHERE user_id = ? AND replay_id = ?
                """,
                (attempts, status, str(user_id), int(replay_id)),
            )
            self._commit()
            return status

    def notification_status(self, user_id: str, replay_id: int) -> Optional[str]:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM notifications WHERE user_id = ? AND replay_id = ?",
                (str(user_id), int(replay_id)),
            ).fetchone()
        return row["status"] if row else None

    def insert_link(
        self,
        token: str,
        user_id: str,
        player: str,
        created_at: float,
        expires_at: float,
        replay_ids: list[int],
        range_start: float,
        deck_replay_ids: Optional[list[int]] = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO share_links
                    (token, user_id, player, created_at, expires_at, replay_ids_json, range_start, deck_replay_ids_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    str(user_id),
                    player,
                    float(created_at),
                    float(expires_at),
                    json.dumps([int(item) for item in replay_ids]),
                    float(range_start),
                    json.dumps([int(item) for item in (replay_ids if deck_replay_ids is None else deck_replay_ids)]),
                ),
            )
            self._commit()

    def get_link(self, token: str) -> Optional[dict]:
        with self._lock:
            if self.readonly:
                # 独立 share 进程启动时 BEGIN 固定了快照；QQ 之后写入的新链接必须重新可见。
                try:
                    self._db.execute('COMMIT')
                except sqlite3.OperationalError:
                    pass
                self._db.execute('BEGIN')
            row = self._db.execute(
                "SELECT * FROM share_links WHERE token = ?",
                (token,),
            ).fetchone()
        if not row:
            return None
        return {
            "token": row["token"],
            "user_id": row["user_id"],
            "player": row["player"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "replay_ids": json.loads(row["replay_ids_json"]),
            "deck_replay_ids": json.loads(row['deck_replay_ids_json'] if row['deck_replay_ids_json'] is not None
                                          else row['replay_ids_json']),
            "range_start": row["range_start"],
            "revoked_at": row["revoked_at"] if "revoked_at" in row.keys() else None,
        }

    def find_reusable_link(self, user_id: str, player: str, replay_ids: list[int],
                           range_start: float, deck_replay_ids: list[int],
                           min_expires_at: float) -> Optional[dict]:
        """复用同一用户/同一分享范围且剩余有效期足够的链接。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM share_links WHERE user_id=? AND player=? AND revoked_at IS NULL "
                "AND expires_at>=? ORDER BY created_at DESC LIMIT 20",
                (str(user_id), player, float(min_expires_at))).fetchall()
        wanted = json.dumps([int(item) for item in replay_ids])
        wanted_deck = json.dumps([int(item) for item in deck_replay_ids])
        for row in rows:
            try:
                if (row['replay_ids_json'] == wanted
                        and (row['deck_replay_ids_json'] or row['replay_ids_json']) == wanted_deck
                        and float(row['range_start']) == float(range_start)):
                    return {
                        "token": row['token'], "user_id": row['user_id'], "player": row['player'],
                        "created_at": row['created_at'], "expires_at": row['expires_at'],
                        "replay_ids": json.loads(row['replay_ids_json']),
                        "deck_replay_ids": json.loads(row['deck_replay_ids_json'] or row['replay_ids_json']),
                        "range_start": row['range_start'],
                        "revoked_at": row['revoked_at'],
                    }
            except (KeyError, ValueError, TypeError):
                continue
        return None

    def find_reusable_stats_link(self, user_id: str, min_expires_at: float) -> Optional[dict]:
        with self._lock:
            if self.readonly:
                try:
                    self._db.execute('COMMIT')
                except sqlite3.OperationalError:
                    pass
                self._db.execute('BEGIN')
            row = self._db.execute(
                "SELECT * FROM stats_links WHERE user_id=? AND revoked_at IS NULL "
                "AND expires_at>=? ORDER BY created_at DESC LIMIT 1",
                (str(user_id), float(min_expires_at))).fetchone()
        return dict(row) if row else None

    def revoke_link(self, token: str, revoked_at: float) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "UPDATE share_links SET revoked_at=? WHERE token=? AND revoked_at IS NULL",
                (float(revoked_at), token))
            self._commit()
        return cursor.rowcount > 0

    def revoke_stats_link(self, token: str, revoked_at: float) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "UPDATE stats_links SET revoked_at=? WHERE token=? AND revoked_at IS NULL",
                (float(revoked_at), token))
            self._commit()
        return cursor.rowcount > 0

    def insert_stats_link(self, token: str, user_id: str, created_at: float, expires_at: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO stats_links (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (token, str(user_id), float(created_at), float(expires_at)),
            )
            self._commit()

    def get_stats_link(self, token: str) -> Optional[dict]:
        with self._lock:
            if self.readonly:
                # 与 get_link 同理：QQ 进程刚写入的统计链接必须刷新只读快照才可见。
                try:
                    self._db.execute('COMMIT')
                except sqlite3.OperationalError:
                    pass
                self._db.execute('BEGIN')
            row = self._db.execute("SELECT * FROM stats_links WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        return dict(row)

    def purge_stats_links(self, now: float) -> None:
        with self._lock:
            self._db.execute("DELETE FROM stats_links WHERE expires_at<=?", (float(now),))
            self._commit()
