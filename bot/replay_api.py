# -*- coding: utf-8 -*-
"""录像库只读接口 v2（给观战客户端按“对局”检索和下载）。

和 v1（按单份录像）的区别：
- 一条记录 = 一场对局（session），一场里有多局；每局带双方玩家、胜负、卡组名、先后手；
- 下载单位 = 整场，zip 内是一个以“日期_玩家(卡组)_vs_玩家(卡组)”命名的文件夹，
  含每局 .yrp、对应的 .yrp.evidence.json（若有）和 match.json；
- 全局限速：所有下载共享一个令牌桶，默认 200 KiB/s，避免 3 Mbps 出口被占满；
- 只读：数据库 mode=ro，不写库、不写录像目录、不领观战任务。

鉴权：Authorization: Bearer wt_...（watch_tokens，只存 SHA-256）。

接口：
    GET /replay-api/ping
    GET /replay-api/matches?player=&from=&to=&page=&page_size=
    GET /replay-api/match?key=<key>
    GET /replay-api/match.zip?key=<key>
    GET /replay-api/download?id=<replay-id>        （v1 兼容，单份 .yrp）
    GET /replay-api/search?...                     （v1 兼容，按单份录像）

对局 key：session 有效时为 "s:<session_id>"，没有 session 的老录像退回 "d:<digest>"。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

API_PREFIX = "/replay-api"
CHUNK = 64 * 1024
MAX_PAGE = 100
DEFAULT_PAGE = 30
SEARCH_CONCURRENCY = 2
DOWNLOAD_CONCURRENCY = 4
DOWNLOAD_RATE_BYTES = 200 * 1024      # 全局限速约 1.6 Mbps，给 3 Mbps 出口留余量
DOWNLOAD_BURST_BYTES = 256 * 1024

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d")
_BAD_PART = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SESSION_RE = re.compile(r'"session_id"\s*:\s*"([0-9A-Za-z:_\-]{8,64})"')
_GAME_RE = re.compile(r'"game_no"\s*:\s*(\d{1,3})')


def parse_day(text: str, *, end: bool = False) -> float:
    """把 2026-11-06 解析成当天本地 0 点（end=True 时是当天 23:59:59.999999）。"""
    text = str(text or "").strip()
    if not text:
        return 0.0
    for fmt in _DATE_FORMATS:
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end:
            moment = moment.replace(hour=23, minute=59, second=59, microsecond=999999)
        return moment.timestamp()
    raise ValueError("bad_date")


def hash_token(token: str) -> str:
    """与 bot/watch_tokens.py 相同的摘要算法。"""
    return hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()


def safe_part(text: str, fallback: str = "未知") -> str:
    cleaned = _BAD_PART.sub("_", str(text or "")).strip(" .")
    return cleaned[:60] or fallback


def _like_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class BusyError(RuntimeError):
    pass


class RateLimiter:
    """全局限速令牌桶：所有录像下载共享，避免占满出口带宽。"""

    def __init__(self, rate: float = DOWNLOAD_RATE_BYTES, burst: float = DOWNLOAD_BURST_BYTES,
                 clock=time.monotonic):
        self.rate = max(1.0, float(rate))
        self.capacity = max(1024.0, float(burst))
        self.tokens = self.capacity
        self.updated = clock()
        self.clock = clock
        self.lock = threading.Lock()

    def take(self, size: int, *, stop=None) -> None:
        remaining = max(1, int(size))
        while remaining > 0:
            if stop is not None and stop.is_set():
                raise BusyError("stopped")
            with self.lock:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    chunk = min(float(remaining), self.tokens)
                    self.tokens -= chunk
                    remaining -= int(chunk)
                    continue
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(0.2, max(0.01, wait)))


class ReplayStore:
    """对 replays / replay_sessions / stat_views / stat_builds 的只读查询。"""

    log_tag = "replay-api"

    def __init__(self, db_path, replays_dir, *, clock=None, index_ttl: float = 120.0,
                 ledger_path=None):
        self.db_path = Path(db_path)
        self.replays_dir = Path(replays_dir).resolve()
        self.ledger_path = Path(ledger_path) if ledger_path else self.db_path.parent / \
            "watch-upload-sessions.sqlite3"
        self.clock = clock or (lambda: datetime.now().timestamp())
        self.index_ttl = float(index_ttl)
        self._lock = threading.Lock()
        self._matches: list[dict] | None = None
        self._built_at = 0.0
        self._fingerprint: tuple = ()
        self._refreshing = False
        self._ledger_at = 0.0
        self._ledger: dict[str, tuple] = {}
        self._evidence_cache: dict[str, tuple] = {}
        self._top100: frozenset = frozenset()

    # ---- 连接 ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        uri = "file:%s?mode=ro" % quote(self.db_path.as_posix(), safe="/:")
        con = sqlite3.connect(uri, uri=True, timeout=10.0, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        return con

    # ---- 鉴权 ----------------------------------------------------------
    def verify_token(self, token: str) -> dict | None:
        text = str(token or "").strip()
        if not text:
            return None
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, worker, owner, expires_at FROM watch_tokens "
                "WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",
                (hash_token(text), float(self.clock())),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return dict(id=row["id"], worker=str(row["worker"] or ""),
                    owner=str(row["owner"] or ""), expires_at=float(row["expires_at"]))

    # ---- 路径安全 ------------------------------------------------------
    def safe_path(self, rel_path: str) -> Path | None:
        raw = str(rel_path or "").strip().replace("\\", "/")
        if not raw:
            return None
        try:
            path = (self.replays_dir / raw).resolve()
        except (OSError, RuntimeError):
            return None
        if path == self.replays_dir or not path.is_relative_to(self.replays_dir):
            return None
        if path.suffix.lower() != ".yrp":
            return None
        return path

    def evidence_path(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        sidecar = Path(str(path) + ".evidence.json")
        return sidecar if sidecar.is_file() else None

    # ---- 对局索引 ------------------------------------------------------
    def matches(self) -> list[dict]:
        """返回对局索引：首次同步构建，之后按数据指纹在后台刷新，不阻塞请求。"""
        now = self.clock()
        with self._lock:
            if self._matches is None:
                self._matches = self._build_index()
                self._built_at = now
                self._fingerprint = self._data_fingerprint()
                return self._matches
            stale = now - self._built_at > self.index_ttl
            if stale and not self._refreshing and self._data_fingerprint() != self._fingerprint:
                self._refreshing = True
                threading.Thread(target=self._refresh, name="replay-index", daemon=True).start()
            return self._matches

    def _refresh(self) -> None:
        try:
            data = self._build_index()
            fingerprint = self._data_fingerprint()
            with self._lock:
                self._matches = data
                self._built_at = self.clock()
                self._fingerprint = fingerprint
        finally:
            with self._lock:
                self._refreshing = False

    def _data_fingerprint(self) -> tuple:
        con = self._connect()
        try:
            row = con.execute("SELECT COUNT(*), MAX(id) FROM replays").fetchone()
        finally:
            con.close()
        return (int(row[0] or 0), int(row[1] or 0))

    # ---- session 补全（replay_sessions 从 11-04 起没有新数据） ----------
    def _ledger_sessions(self) -> dict[str, tuple]:
        """上传账本 data/watch-upload-sessions.sqlite3 的 digest -> (session, game_no)。"""
        now = self.clock()
        if self._ledger and now - self._ledger_at < self.index_ttl:
            return self._ledger
        data: dict[str, tuple] = {}
        try:
            uri = "file:%s?mode=ro" % quote(self.ledger_path.as_posix(), safe="/:")
            con = sqlite3.connect(uri, uri=True, timeout=10.0)
            try:
                for digest, capture in con.execute("SELECT digest, capture FROM uploads"):
                    if not capture:
                        continue
                    try:
                        payload = json.loads(capture)
                    except (TypeError, ValueError):
                        continue
                    session = str(payload.get("session_id") or "").strip()
                    if session and not session.startswith("prefix:"):
                        data[str(digest)] = (session, int(payload.get("game_no") or 0))
            finally:
                con.close()
        except sqlite3.Error:
            data = {}
        self._ledger = data
        self._ledger_at = now
        return data

    def _evidence_session(self, path: Path | None, digest: str) -> tuple[str, int] | None:
        """最后的兜底：读录像旁证据 JSON 的尾部（session_id/game_no 通常在末尾）。"""
        sidecar = Path(str(path) + ".evidence.json") if path is not None else None
        if sidecar is None or not sidecar.is_file():
            return None
        try:
            mtime = sidecar.stat().st_mtime
        except OSError:
            return None
        cached = self._evidence_cache.get(digest)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            size = sidecar.stat().st_size
            with sidecar.open("rb") as handle:
                if size > 8192:
                    handle.seek(size - 8192)
                tail = handle.read(8192).decode("utf-8", "ignore")
        except OSError:
            return None
        match = _SESSION_RE.search(tail)
        if not match:
            self._evidence_cache[digest] = (mtime, None)
            return None
        game = _GAME_RE.search(tail)
        found = (match.group(1), int(game.group(1)) if game else 0)
        self._evidence_cache[digest] = (mtime, found)
        return found

    def _build_index(self) -> list[dict]:
        con = self._connect()
        try:
            game_rows = con.execute(
                """SELECT r.id, r.digest, r.rel_path, r.players_json, r.recorded_at,
                          s.session_id, s.game_no
                   FROM replays r LEFT JOIN replay_sessions s ON s.replay_id = r.id"""
            ).fetchall()
            view_rows = con.execute(
                "SELECT digest, player, player_index, build, outcome, first_player FROM stat_views"
            ).fetchall()
            build_rows = con.execute("SELECT fingerprint, deck_type FROM stat_builds").fetchall()
            series_rows = con.execute("SELECT build, series FROM stat_build_series").fetchall()
            try:
                top100_rows = con.execute("SELECT player FROM stat_current_top100").fetchall()
            except sqlite3.Error:  # 名单表缺失时按“无人上榜”处理，不影响其它筛选
                top100_rows = []
        finally:
            con.close()
        self._top100 = frozenset(str(row["player"]).strip() for row in top100_rows
                                 if str(row["player"]).strip())

        decks = {row["fingerprint"]: str(row["deck_type"] or "") for row in build_rows}
        series: dict[str, list[str]] = {}
        for row in series_rows:
            series.setdefault(row["build"], []).append(str(row["series"]))
        views: dict[str, list[dict]] = {}
        for row in view_rows:
            views.setdefault(row["digest"], []).append(dict(
                player=str(row["player"] or ""),
                outcome=str(row["outcome"] or ""),
                deck=decks.get(row["build"], "") or "",
                series=series.get(row["build"]) or [],
            ))

        ledger = self._ledger_sessions()
        grouped: dict[str, dict] = {}
        for row in game_rows:
            digest = str(row["digest"])
            session = str(row["session_id"] or "").strip()
            game_no = int(row["game_no"] or 0)
            path = self.safe_path(row["rel_path"])
            if not session or session.startswith("prefix:"):
                fallback = ledger.get(digest) or self._evidence_session(path, digest)
                if fallback and fallback[0]:
                    session, game_no = fallback
            key = "s:" + session if session and not session.startswith("prefix:") else "d:" + digest
            match = grouped.get(key)
            if match is None:
                players: list[str] = []
                try:
                    raw = json.loads(row["players_json"] or "[]")
                    if isinstance(raw, list):
                        players = [str(item) for item in raw if str(item).strip()]
                except (TypeError, ValueError):
                    players = []
                match = grouped[key] = dict(key=key, session=session, players=players,
                                            games={}, start=float(row["recorded_at"] or 0.0),
                                            end=float(row["recorded_at"] or 0.0))
            game = match["games"].get(digest)
            started = float(row["recorded_at"] or 0.0)
            match["start"] = min(match["start"], started) if match["start"] else started
            match["end"] = max(match["end"], started)
            if game is None:
                size = None
                if path is not None:
                    try:
                        if path.is_file():
                            size = path.stat().st_size
                    except OSError:
                        size = None
                game = match["games"][digest] = dict(
                    id=int(row["id"]), digest=digest,
                    name=Path(str(row["rel_path"] or "")).name,
                    path=row["rel_path"], game_no=game_no,
                    recorded_at=started, size=size, evidence=False,
                )
                if path is not None:
                    game["evidence"] = self.evidence_path(path) is not None

        matches: list[dict] = []
        for match in grouped.values():
            games = sorted(match["games"].values(),
                           key=lambda g: (g["game_no"] or 999, g["recorded_at"]))
            for game in games:
                game["views"] = views.get(game["digest"]) or []
            players = self._merge_players(match["players"], games)
            decks = {}
            for player in players:
                decks[player] = self._player_deck(player, games)
            wins = {player: 0 for player in players}
            decided = 0
            for game in games:
                for view in game["views"]:
                    if view["outcome"] == "win" and view["player"] in wins:
                        wins[view["player"]] += 1
                        decided += 1
            total_bytes = sum(game["size"] or 0 for game in games if game["size"])
            match.update(
                players=players, games=games, decks=decks,
                wins=wins, decided=decided, bytes=total_bytes,
                available=any(game["size"] for game in games),
                game_count=len(games),
            )
            match.pop("games_dict", None)
            matches.append(match)
        matches.sort(key=lambda item: item["end"], reverse=True)
        return matches

    @staticmethod
    def _merge_players(seed: list[str], games: list[dict]) -> list[str]:
        players = list(seed)
        for game in games:
            for view in game["views"]:
                if view["player"] and view["player"] not in players:
                    players.append(view["player"])
        return players[:4]

    @staticmethod
    def _player_deck(player: str, games: list[dict]) -> str:
        for game in sorted(games, key=lambda g: g["recorded_at"], reverse=True):
            for view in game["views"]:
                if view["player"] == player and view["deck"]:
                    return view["deck"]
        return ""

    # ---- 检索/详情 -----------------------------------------------------
    def search_matches(self, *, player: str = "", opponent: str = "", deck: str = "",
                       self_deck: str = "", opponent_deck: str = "",
                       start: float = 0.0, end: float = 0.0, page: int = 1,
                       page_size: int = DEFAULT_PAGE,
                       self_rank: str = "all", opponent_rank: str = "all") -> dict:
        """player/opponent：我方/对手玩家名；self_deck/opponent_deck：我方/对手卡组。

        玩家名都填时要求这一场里有**两个不同**玩家分别匹配（谁在哪边都行）；只填一边
        就按任意一边匹配。卡组按角色过滤：我方默认=player 或左方(players[0])，
        对手=opponent 或另一名玩家。deck 为旧的“任意一方卡组”兼容参数。
        """
        page = max(1, int(page or 1))
        size = max(1, min(MAX_PAGE, int(page_size or DEFAULT_PAGE)))
        needle = str(player or "").strip().lower()
        opp_needle = str(opponent or "").strip().lower()
        deck_needle = str(deck or "").strip().lower()
        self_deck_needle = str(self_deck or "").strip().lower()
        opp_deck_needle = str(opponent_deck or "").strip().lower()
        want_self = str(self_rank or "all").strip() == "100"
        want_opponent = str(opponent_rank or "all").strip() == "100"
        top100 = self._top100
        items = []
        for match in self.matches():
            if start and match["end"] < start:
                continue
            if end and match["start"] > end:
                continue
            players = match["players"]
            me = None
            if needle:
                me = next((name for name in players if needle in name.lower()), None)
                if me is None:
                    continue
            other = None
            if opp_needle:
                other = next((name for name in players
                              if name != me and opp_needle in name.lower()), None)
                if other is None:
                    continue
            if deck_needle and not any(deck_needle in (value or "").lower()
                                       for value in match["decks"].values()):
                continue
            if self_deck_needle or opp_deck_needle or want_self or want_opponent:
                self_name = me if me is not None else (players[0] if players else "")
                opponent_name = other if other is not None else \
                    next((name for name in players if name != self_name), "")
                if self_deck_needle and not self._player_has_deck(
                        match, self_name, self_deck_needle):
                    continue
                if opp_deck_needle and not self._player_has_deck(
                        match, opponent_name, opp_deck_needle):
                    continue
                if want_self and self_name not in top100:
                    continue
                if want_opponent and opponent_name not in top100:
                    continue
            items.append(match)
        total_bytes = sum(item["bytes"] for item in items)
        total = len(items)
        offset = (page - 1) * size
        return dict(page=page, page_size=size, total=total, total_bytes=total_bytes,
                    items=[self.public_match(item, with_games=True)
                           for item in items[offset:offset + size]])

    @staticmethod
    def _player_has_deck(match: dict, player: str, needle: str) -> bool:
        """该玩家在这场里任一局用过含 needle 的卡组就算命中（子串、忽略大小写）。"""
        for game in match.get("games") or []:
            for view in game.get("views") or []:
                if view.get("player") == player and needle in (view.get("deck") or "").lower():
                    return True
        return needle in (match["decks"].get(player) or "").lower()

    def find_match(self, key: str) -> dict | None:
        key = str(key or "").strip()
        if not key:
            return None
        for match in self.matches():
            if match["key"] == key:
                return match
        return None

    def public_match(self, match: dict, *, with_games: bool = True) -> dict:
        players = match["players"]
        wins = match["wins"]
        score = ""
        winner = ""
        if players and match["decided"]:
            counts = [wins.get(name, 0) for name in players]
            score = ":".join(str(count) for count in counts)
            best = max(counts)
            if counts.count(best) == 1:
                winner = players[counts.index(best)]
        item = dict(
            key=match["key"], session=match["session"], players=players,
            decks={name: (match["decks"].get(name) or "") for name in players},
            score=score, winner=winner, decided=match["decided"],
            start=match["start"], end=match["end"],
            game_count=match["game_count"], bytes=match["bytes"],
            available=match["available"],
            download="%s/match.zip?key=%s" % (API_PREFIX, quote(match["key"], safe="")),
        )
        folder = self.folder_name(match)
        item["folder"] = folder
        if with_games:
            item["games"] = [self._public_game(game) for game in match["games"]]
        return item

    @staticmethod
    def _public_game(game: dict) -> dict:
        return dict(
            id=game["id"], digest=game["digest"], game_no=game["game_no"],
            name=game["name"], recorded_at=game["recorded_at"], size=game["size"],
            evidence=bool(game.get("evidence")),
            views=[dict(player=view["player"], outcome=view["outcome"],
                        deck=view["deck"], series=view["series"])
                   for view in game["views"]],
            download="%s/download?id=%d" % (API_PREFIX, game["id"]),
        )

    def folder_name(self, match: dict) -> str:
        """下载目录名：日期_玩家_vs_玩家。卡组名不进目录（.yrp 里已经有卡组）。"""
        stamp = datetime.fromtimestamp(match["start"] or match["end"]).strftime("%Y%m%d-%H%M")
        names = [safe_part(name) for name in match["players"][:2]]
        return safe_part(stamp + ("_" + "_vs_".join(names) if names else ""), "对局")

    # ---- 打包 ----------------------------------------------------------
    def _game_summary(self, game: dict) -> dict:
        """每局的 json：胜负、玩家、卡组、是否从头录。优先用证据文件，缺失时用统计库。"""
        views = game.get("views") or []
        summary = dict(
            digest=game.get("digest"), game_no=game.get("game_no") or 0,
            players=[view["player"] for view in views],
            winner="", winner_index=None, captured_from_start=None,
            source="stat_views", received_at=None,
            decks={view["player"]: view["deck"] for view in views},
            outcomes={view["player"]: view["outcome"] for view in views},
        )
        path = self.safe_path(game.get("path") or "")
        sidecar = self.evidence_path(path) if path is not None else None
        if sidecar is not None:
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            players = [str(item) for item in (data.get("players") or []) if str(item).strip()]
            index = data.get("winner")
            if players:
                summary["players"] = players
                if isinstance(index, int) and 0 <= index < len(players):
                    summary["winner"] = players[index]
                    summary["winner_index"] = index
            summary["captured_from_start"] = bool(data.get("captured_from_start"))
            summary["source"] = data.get("source") or ""
            summary["received_at"] = data.get("received_at")
        if not summary["winner"]:
            for player, outcome in summary["outcomes"].items():
                if outcome == "win":
                    summary["winner"] = player
                    break
        return summary

    def build_match_zip(self, match: dict, *, raw: bool = False) -> tuple[bytes, str]:
        item = self.public_match(match, with_games=True)
        folder = item["folder"]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            for game in match["games"]:
                path = self.safe_path(game["path"])
                if path is None or not path.is_file():
                    continue
                index = game["game_no"] or 1
                arcname = "%s/%d_%s" % (folder, index, game["name"])
                archive.write(path, arcname)
                archive.writestr("%s/%d_game.json" % (folder, index),
                                 json.dumps(self._game_summary(game),
                                            ensure_ascii=False, indent=2))
                if raw:
                    sidecar = self.evidence_path(path)
                    if sidecar is not None:
                        archive.write(sidecar, str(arcname) + ".evidence.json")
            archive.writestr(folder + "/match.json",
                             json.dumps(item, ensure_ascii=False, indent=2))
        return buffer.getvalue(), folder + ".zip"

    # ---- 单份（v1 兼容） ----------------------------------------------
    def resolve(self, replay_id: int) -> tuple[Path | None, str]:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, rel_path FROM replays WHERE id=?", (int(replay_id),)
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None, ""
        path = self.safe_path(row["rel_path"])
        name = Path(str(row["rel_path"] or "")).name
        if path is None or not path.is_file():
            return None, name
        return path, name

    def search_files(self, *, player: str = "", start: float = 0.0, end: float = 0.0,
                     page: int = 1, page_size: int = DEFAULT_PAGE) -> dict:
        """v1 的单份录像检索，保留给旧客户端。"""
        page = max(1, int(page or 1))
        size = max(1, min(MAX_PAGE, int(page_size or DEFAULT_PAGE)))
        clauses, params = [], []
        text = str(player or "").strip()
        if text:
            pattern = "%" + _like_escape(text) + "%"
            clauses.append("(focus_player LIKE ? ESCAPE '\\' OR players_json LIKE ? ESCAPE '\\')")
            params.extend([pattern, pattern])
        if start:
            clauses.append("recorded_at >= ?")
            params.append(float(start))
        if end:
            clauses.append("recorded_at <= ?")
            params.append(float(end))
        where = " AND ".join(clauses) or "1=1"
        con = self._connect()
        try:
            total = int(con.execute(
                f"SELECT COUNT(DISTINCT digest) FROM replays WHERE {where}", params).fetchone()[0])
            rows = con.execute(
                f"""SELECT digest, MIN(id) AS id, MIN(rel_path) AS rel_path,
                           MIN(players_json) AS players_json, MIN(recorded_at) AS recorded_at,
                           GROUP_CONCAT(DISTINCT focus_player) AS focuses
                    FROM replays WHERE {where} GROUP BY digest
                    ORDER BY recorded_at DESC LIMIT ? OFFSET ?""",
                params + [size, (page - 1) * size]).fetchall()
            ids = [int(row["id"]) for row in rows]
            sessions: dict[int, tuple] = {}
            if ids:
                marks = ",".join("?" * len(ids))
                for session_row in con.execute(
                        f"SELECT replay_id, session_id, game_no FROM replay_sessions "
                        f"WHERE replay_id IN ({marks})", ids):
                    sessions[int(session_row["replay_id"])] = (
                        str(session_row["session_id"] or ""), int(session_row["game_no"] or 0))
        finally:
            con.close()
        items = []
        for row in rows:
            path = self.safe_path(row["rel_path"])
            size_bytes = None
            if path is not None:
                try:
                    if path.is_file():
                        size_bytes = path.stat().st_size
                except OSError:
                    size_bytes = None
            players: list[str] = []
            try:
                raw_players = json.loads(row["players_json"] or "[]")
                if isinstance(raw_players, list):
                    players = [str(item) for item in raw_players if str(item).strip()]
            except (TypeError, ValueError):
                players = []
            session, game_no = sessions.get(int(row["id"]), ("", 0))
            items.append(dict(digest=row["digest"], id=int(row["id"]),
                              name=Path(str(row["rel_path"] or "")).name,
                              players=players,
                              focus=[item for item in str(row["focuses"] or "").split(",") if item],
                              session=session, game_no=game_no,
                              recorded_at=float(row["recorded_at"] or 0.0),
                              size=size_bytes, available=size_bytes is not None,
                              download="%s/download?id=%d" % (API_PREFIX, int(row["id"]))))
        return dict(page=page, page_size=size, total=total, items=items)


class ReplayApp:
    log_tag = "replay-api"

    def __init__(self, store: ReplayStore, *, log=None, clock=None):
        self.store = store
        self.log = log or (lambda message: None)
        self.clock = clock or store.clock
        self.limiter = RateLimiter()
        self._search_slots = threading.BoundedSemaphore(SEARCH_CONCURRENCY)
        self._download_slots = threading.BoundedSemaphore(DOWNLOAD_CONCURRENCY)

    def authorize(self, header: str | None) -> dict | None:
        text = str(header or "")
        token = text[7:].strip() if text.lower().startswith("bearer ") else ""
        if not token:
            return None
        try:
            return self.store.verify_token(token)
        except Exception as exc:
            self.log("[录像库] 令牌校验失败：%s" % type(exc).__name__)
            return None

    def search(self, params: dict) -> dict:
        player = str(params.get("player", [""])[0] or "").strip()[:64]
        opponent = str(params.get("opponent", [""])[0] or "").strip()[:64]
        deck = str(params.get("deck", [""])[0] or "").strip()[:64]
        self_deck = str(params.get("self_deck", [""])[0] or "").strip()[:64]
        opponent_deck = str(params.get("opponent_deck", [""])[0] or "").strip()[:64]
        self_rank = str(params.get("self_rank", ["all"])[0] or "all").strip().lower()
        opponent_rank = str(params.get("opponent_rank", ["all"])[0] or "all").strip().lower()
        start = parse_day(str(params.get("from", [""])[0] or ""))
        end = parse_day(str(params.get("to", [""])[0]), end=True)
        if end and start and end < start:
            raise ValueError("bad_range")
        page = self._int(params.get("page"), 1)
        page_size = self._int(params.get("page_size"), DEFAULT_PAGE)
        if not self._search_slots.acquire(timeout=5):
            raise BusyError("busy")
        try:
            return self.store.search_matches(player=player, opponent=opponent, deck=deck,
                                             self_deck=self_deck, opponent_deck=opponent_deck,
                                             start=start, end=end, page=page,
                                             page_size=page_size,
                                             self_rank=self_rank, opponent_rank=opponent_rank)
        finally:
            self._search_slots.release()

    def acquire_download(self):
        if not self._download_slots.acquire(timeout=10):
            raise BusyError("busy")
        return True

    def release_download(self) -> None:
        try:
            self._download_slots.release()
        except ValueError:
            pass

    @staticmethod
    def _int(values, default: int) -> int:
        try:
            return int(str((values or [default])[0] or default))
        except (TypeError, ValueError):
            return default


class _Handler(BaseHTTPRequestHandler):
    server_version = "ygobot-replay/2"
    protocol_version = "HTTP/1.1"

    def app(self) -> ReplayApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: D401 - 访问日志由 nginx 记录
        return

    # ---- 输出 ----------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, body: bytes, filename: str, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         "attachment; filename=\"replay.bin\"; filename*=UTF-8''%s"
                         % quote(filename))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for index in range(0, len(body), CHUNK):
                self.app().limiter.take(min(CHUNK, len(body) - index))
                self.wfile.write(body[index:index + CHUNK])
        except (BrokenPipeError, ConnectionResetError, BusyError):
            return

    def _send_file(self, path: Path, name: str) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         "attachment; filename=\"replay.yrp\"; filename*=UTF-8''%s"
                         % quote(name or path.name))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    self.app().limiter.take(len(chunk))
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, BusyError):
            return

    # ---- 路由 ----------------------------------------------------------
    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        if not (path == API_PREFIX or path.startswith(API_PREFIX + "/")):
            self._send_json(404, dict(ok=False, error="not_found"))
            return
        row = self.app().authorize(self.headers.get("Authorization"))
        if row is None:
            self._send_json(401, dict(ok=False, error="unauthorized"))
            return
        params = parse_qs(parts.query)

        if path == API_PREFIX + "/ping":
            self._send_json(200, dict(ok=True, worker=str(row.get("worker") or ""),
                                      version=2))
            return
        if path == API_PREFIX + "/matches":
            try:
                result = self.app().search(params)
            except ValueError:
                self._send_json(400, dict(ok=False, error="bad_date"))
                return
            except BusyError:
                self._send_json(503, dict(ok=False, error="busy"))
                return
            except Exception as exc:
                self.app().log("[录像库] 检索失败：%s" % type(exc).__name__)
                self._send_json(500, dict(ok=False, error="search_failed"))
                return
            self._send_json(200, dict(ok=True, **result))
            return
        if path == API_PREFIX + "/match":
            key = str(params.get("key", [""])[0] or "").strip()
            try:
                match = self.app().store.find_match(key)
            except Exception as exc:
                self.app().log("[录像库] 取对局失败：%s" % type(exc).__name__)
                self._send_json(500, dict(ok=False, error="match_failed"))
                return
            if match is None:
                self._send_json(404, dict(ok=False, error="match_missing"))
                return
            self._send_json(200, dict(ok=True,
                                      match=self.app().store.public_match(match, with_games=True)))
            return
        if path == API_PREFIX + "/match.zip":
            key = str(params.get("key", [""])[0] or "").strip()
            raw = str(params.get("raw", [""])[0] or "") in ("1", "true", "yes")
            try:
                match = self.app().store.find_match(key)
                if match is None:
                    self._send_json(404, dict(ok=False, error="match_missing"))
                    return
                body, filename = self.app().store.build_match_zip(match, raw=raw)
            except BusyError:
                self._send_json(503, dict(ok=False, error="busy"))
                return
            except Exception as exc:
                self.app().log("[录像库] 打包失败：%s" % type(exc).__name__)
                self._send_json(500, dict(ok=False, error="zip_failed"))
                return
            self.app().acquire_download()
            try:
                self._send_bytes(body, filename, "application/zip")
            finally:
                self.app().release_download()
            return
        if path == API_PREFIX + "/download":
            raw = str(params.get("id", [""])[0] or "").strip()
            try:
                replay_id = int(raw)
            except ValueError:
                self._send_json(400, dict(ok=False, error="bad_id"))
                return
            try:
                file_path, name = self.app().store.resolve(replay_id)
            except Exception as exc:
                self.app().log("[录像库] 取文件失败：%s" % type(exc).__name__)
                self._send_json(500, dict(ok=False, error="resolve_failed"))
                return
            if file_path is None:
                self._send_json(404, dict(ok=False, error="file_missing"))
                return
            self.app().acquire_download()
            try:
                self._send_file(file_path, name)
            finally:
                self.app().release_download()
            return
        if path == API_PREFIX + "/search":
            try:
                result = self.app().store.search_files(
                    player=str(params.get("player", [""])[0] or "").strip()[:64],
                    start=parse_day(str(params.get("from", [""])[0] or "")),
                    end=parse_day(str(params.get("to", [""])[0]), end=True),
                    page=self.app()._int(params.get("page"), 1),
                    page_size=self.app()._int(params.get("page_size"), DEFAULT_PAGE))
            except ValueError:
                self._send_json(400, dict(ok=False, error="bad_date"))
                return
            except Exception as exc:
                self.app().log("[录像库] 旧检索失败：%s" % type(exc).__name__)
                self._send_json(500, dict(ok=False, error="search_failed"))
                return
            self._send_json(200, dict(ok=True, **result))
            return
        self._send_json(404, dict(ok=False, error="not_found"))


class ReplayApiServer:
    def __init__(self, root, *, host: str = "127.0.0.1", port: int = 8792,
                 db_path=None, replays_dir=None, log=None):
        root_path = Path(root).resolve()
        self.log = log or (lambda message: print(message, flush=True))
        self.store = ReplayStore(db_path or root_path / "data" / "bot.sqlite3",
                                 replays_dir or root_path / "data" / "replays")
        self.app = ReplayApp(self.store, log=self.log)
        self.httpd = ThreadingHTTPServer((host, int(port)), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = self.app  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def serve_forever(self) -> None:
        self.log("[录像库] %s:%d 已启动（只读，按对局，限速 %d KiB/s）"
                 % (self.httpd.server_address[0], self.port, DOWNLOAD_RATE_BYTES // 1024))
        # 启动后先把对局索引建好（冷建约 10 秒），别让第一个用户请求等。
        threading.Thread(target=self._warm_up, name="replay-warmup", daemon=True).start()
        self.httpd.serve_forever(poll_interval=0.5)

    def _warm_up(self) -> None:
        started = time.time()
        try:
            matches = self.store.matches()
            self.log("[录像库] 对局索引就绪：%d 场，耗时 %.1fs"
                     % (len(matches), time.time() - started))
        except Exception as exc:  # 预热失败不影响服务，首个请求会再试
            self.log("[录像库] 预热失败：%s" % type(exc).__name__)

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass


def load_settings(root: Path) -> dict:
    settings = dict(host="127.0.0.1", port=8792)
    config_path = root / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    section = config.get("replay_api")
    section = section if isinstance(section, dict) else {}
    settings["host"] = str(section.get("bind_host") or settings["host"])
    try:
        settings["port"] = int(section.get("bind_port") or settings["port"])
    except (TypeError, ValueError):
        settings["port"] = 8792
    data_dir = Path(config.get("data_dir") or root / "data")
    settings["db_path"] = data_dir / "bot.sqlite3"
    settings["replays_dir"] = data_dir / "replays"
    return settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="录像库只读检索/下载接口（按对局）")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--db", default="")
    parser.add_argument("--replays", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    settings = load_settings(root)
    server = ReplayApiServer(
        root,
        host=args.host or settings["host"],
        port=args.port or settings["port"],
        db_path=args.db or settings["db_path"],
        replays_dir=args.replays or settings["replays_dir"],
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
