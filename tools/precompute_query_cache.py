# -*- coding: utf-8 -*-
"""低峰预计算查询层构筑缓存。

顺序：当前前 100 → 订阅玩家；每组按缓存文件 mtime 从旧到新（没缓存的最前）。
只写 data/query-cache/build/*.json，不改数据库、不发 QQ。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.query_core import PortalBusy, PortalClient, PortalUnavailable  # noqa: E402
from bot.query_precompute import cache_path, cleanup, read_precomputed, write_precomputed, MISS  # noqa: E402

PSI_MAX = 40.0
AVAILABLE_MIN_MB = 400
LOAD_MAX = 2.5
MAX_RUNTIME_SECONDS = 5400


def _psi_some_avg10() -> float:
    try:
        with open("/proc/pressure/memory", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("some "):
                    return float(line.split("avg10=", 1)[1].split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _available_mb() -> float:
    try:
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return 1e9


def _load1() -> float:
    try:
        return float(os.getloadavg()[0])
    except (AttributeError, OSError):
        return 0.0


def _guards_tripped() -> str:
    psi = _psi_some_avg10()
    if psi > PSI_MAX:
        return f"psi={psi:.1f}>%d" % PSI_MAX
    available = _available_mb()
    if available < AVAILABLE_MIN_MB:
        return f"available={available:.0f}MB<%dMB" % AVAILABLE_MIN_MB
    load = _load1()
    if load > LOAD_MAX:
        return f"load={load:.2f}>%s" % LOAD_MAX
    return ""


def _log(path: Path, message: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + message
    print(line, flush=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


def _targets(db: sqlite3.Connection) -> list[str]:
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ordered: list[str] = []
    if "stat_current_top100" in names:
        ordered.extend(str(row[0]) for row in db.execute("SELECT player FROM stat_current_top100"))
    if "subscriptions" in names:
        ordered.extend(str(row[0]) for row in db.execute("SELECT DISTINCT player FROM subscriptions"))
    seen = set()
    result = []
    for name in ordered:
        name = str(name or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _latest_replay(db: sqlite3.Connection, player: str) -> float:
    try:
        row = db.execute("SELECT MAX(recorded_at) FROM replays WHERE focus_player=?", (player,)).fetchone()
        return float(row[0] or 0) if row else 0.0
    except sqlite3.Error:
        return 0.0


def _stamp(data_dir: Path, player: str) -> float:
    try:
        return cache_path(data_dir, player).stat().st_mtime
    except OSError:
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/ygobot/config.json")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--ttl-hours", type=float, default=24.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = {}
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    data_dir = Path(args.data_dir or config.get("data_dir") or "/opt/ygobot/data")
    query_cfg = config.get("query") or {}
    portal_port = int(query_cfg.get("portal_port", 8788))
    log_path = data_dir / "logs" / "query-precompute.log"

    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass

    db_path = data_dir / "bot.sqlite3"
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        _log(log_path, f"db_open_failed {type(exc).__name__}")
        return 1
    try:
        targets = _targets(db)
        if not targets:
            _log(log_path, "no_targets")
            return 0
        # 前 100 优先；组内按缓存 mtime 从旧到新（没缓存的 mtime=0 最前）。
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        top_players = set()
        if "stat_current_top100" in names:
            top_players = {str(row[0]) for row in db.execute("SELECT player FROM stat_current_top100")}
        indexed = list(enumerate(targets))
        head = sorted((item for item in indexed if item[1] in top_players),
                      key=lambda pair: (_stamp(data_dir, pair[1]), pair[0]))
        tail = sorted((item for item in indexed if item[1] not in top_players),
                      key=lambda pair: (_stamp(data_dir, pair[1]), pair[0]))
        ordered = [player for _, player in head + tail]

        if args.dry_run:
            for player in ordered[: args.limit]:
                latest = _latest_replay(db, player)
                fresh = read_precomputed(data_dir, player, latest) is not MISS
                _log(log_path, f"dry_run player={player!r} latest={latest:.0f} fresh={fresh}")
            _log(log_path, f"dry_run_done targets={len(ordered)}")
            return 0

        cache_dir = data_dir / "query-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock = open(cache_dir / "precompute.lock", "w", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            _log(log_path, "already_running_or_no_lock")
            return 0

        portal = PortalClient(f"http://127.0.0.1:{portal_port}",
                              secret_path=data_dir / "statistics-owner.key",
                              timeout=args.timeout)
        started = time.time()
        processed = 0
        ok = skipped = busy = failed = 0
        for player in ordered:
            if processed >= max(1, args.limit) or time.time() - started > MAX_RUNTIME_SECONDS:
                break
            reason = _guards_tripped()
            if reason:
                _log(log_path, f"stop_guard {reason}")
                break
            latest = _latest_replay(db, player)
            if read_precomputed(data_dir, player, latest) is not MISS:
                skipped += 1
                continue
            processed += 1
            ttl = int(args.ttl_hours * 3600)
            try:
                payload = portal.get_json("/statistics/api/build", {"player": player})
            except PortalBusy:
                busy += 1
                time.sleep(10)
                try:
                    payload = portal.get_json("/statistics/api/build", {"player": player})
                except (PortalBusy, PortalUnavailable) as exc:
                    failed += 1
                    _log(log_path, f"busy player={player!r} {type(exc).__name__}")
                    continue
            except PortalUnavailable as exc:
                failed += 1
                _log(log_path, f"error player={player!r} {type(exc).__name__}")
                continue
            if payload is None:
                ttl = 600
            try:
                path = write_precomputed(data_dir, player, payload, latest, ttl)
                ok += 1
                _log(log_path, f"ok player={player!r} size={path.stat().st_size}")
            except OSError as exc:
                failed += 1
                _log(log_path, f"write_failed player={player!r} {type(exc).__name__}")
        removed = cleanup(data_dir, 7)
        _log(log_path, f"done ok={ok} skipped={skipped} busy={busy} failed={failed} removed={removed} processed={processed}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
