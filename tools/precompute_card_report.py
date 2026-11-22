#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""低峰预计算卡片统计默认视图（100/100、200/200）到 data/cache/。

只写 data/cache/card-report-<own>-<opp>.json，不写数据库、不改统计口径。
参数与页面默认一致：seat=all、metric=opening、threshold=1、min=0、sort=rate、
partition=auto、limit=500、四个 phase；任何筛选请求仍走实时计算。
200/200 默认每 45 分钟才重算一次（--heavy-interval），减少单轮 60 秒负载。
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import statistics_card_report as card_report  # noqa: E402
from bot import statistics_matrix as matrix_report  # noqa: E402
from bot.card_statistics import Filters  # noqa: E402
from bot.statistics_rank_scope import scoped_query  # noqa: E402

PSI_MAX = 50.0
AVAILABLE_MIN_MB = 300
LOAD_MAX = 3.5
# 2026-11-22 守卫死锁修复：生产 io PSI 长期 50–80，正常阈值永远过不去，
# 成品停更后请求只能回退全量重算。成品已过刷新目标时，改用与请求端同一档阈值重建一次。
PAUSE_FILE = Path('/run/ygobot-batch-pause')
REBUILD_LIMITS = dict(available_mb=200, psi=80.0, load=4.0)
NORMAL_INTERVAL = 1800
PHASES = ('main_first', 'main_second', 'side_first', 'side_second')
# 页面默认范围是 100:all（前端 self_range=100、opponent_range=all），
# 它也必须有成品，否则默认页每次都要从零算。
SCOPES = (('100', 'all'), ('100', '100'), ('200', '200'))
HEAVY_SCOPES = (('200', '200'),)
MATRIX_SCOPES = (('200', '200'),)


def available_mb():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def io_pressure():
    try:
        for line in Path('/proc/pressure/io').read_text().splitlines():
            if line.startswith('some'):
                return float(line.split('avg10=')[1].split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def guard():
    free = available_mb()
    if free is not None and free < AVAILABLE_MIN_MB:
        return 'memory_low %.0fMB' % free
    if io_pressure() >= PSI_MAX:
        return 'io_pressure %.0f' % io_pressure()
    try:
        if os.getloadavg()[0] > LOAD_MAX:
            return 'load_high %.2f' % os.getloadavg()[0]
    except (AttributeError, OSError):
        pass
    return None


def pause_reason():
    """后台批处理暂停标记：由 tools/memory_relief.py 在低内存时写入 /run（tmpfs）。"""
    try:
        if PAUSE_FILE.exists():
            return 'batch_pause'
    except OSError:
        return None
    return None


def rebuild_guard():
    """成品已过期时的重建条件：与请求端同一档，比正常刷新宽一档。"""
    free = available_mb()
    if free is not None and free < REBUILD_LIMITS['available_mb']:
        return 'memory_low %.0fMB' % free
    pressure = io_pressure()
    if pressure >= REBUILD_LIMITS['psi']:
        return 'io_pressure %.0f' % pressure
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    if load is not None and load > REBUILD_LIMITS['load']:
        return 'load_high %.2f' % load
    return None


def artifact_age(path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def release_memory():
    """把上一个 scope 占用的堆真正还给系统。

    否则同一进程内紧接着的 guard() 读到的仍是「还没释放」的可用内存：
    实测 19:36 建完 100-all 后只剩 120MB，后三个成品全被自己的残留挡住，
    一次运行只能补一个成品。gc 只回收对象，glibc 仍握着 arena，需 malloc_trim。
    """
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except (OSError, AttributeError, ImportError):
        pass


def log(path, message):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message))
    except OSError:
        pass


def compute(db, replay_root, own, opponent):
    filters = Filters((), (), (), (), None, False, deck_contains=True)
    value = scoped_query((own, opponent), lambda: card_report.query(
        db, replay_root, time.time(), filters=filters, metric='opening', threshold='1',
        min_games=0, sort='rate', partition='auto', limit='500', phases=PHASES))
    return json.dumps(value, ensure_ascii=False)


def compute_matrix(db, replay_root, own, opponent):
    value = scoped_query((own, opponent), lambda: matrix_report.query(
        db, replay_root, time.time(), (), (), None, False, include_samples=False))
    return json.dumps(value, ensure_ascii=False)


def write_atomic(path, own, opponent, body, saved):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(dict(version=1, own=own, opponent=opponent,
                                         saved=saved, status=200, body=body),
                                    ensure_ascii=False), encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/opt/ygobot/config.json')
    parser.add_argument('--data-dir', default='')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true', help='refresh regardless of cache age while respecting resource guards')
    parser.add_argument('--heavy-interval', type=int, default=2700,
                        help='minimum seconds between 200/200 refreshes (default 2700)')
    parser.add_argument('--matrix-interval', type=int, default=1800,
                        help='minimum seconds between matrix 200/200 refreshes (default 1800)')
    parser.add_argument('--only', default='',
                        help='只刷新一个成品（100:all / 100:100 / 200:200 / matrix:200:200）；'
                             '由 unit 串行多次调用，让每个 scope 用独立进程、内存彻底归还')
    args = parser.parse_args()

    config = {}
    try:
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        config = {}
    data_dir = Path(args.data_dir or config.get('data_dir') or '/opt/ygobot/data')
    replay_root = Path(config.get('replay_dir') or data_dir / 'replays')
    log_path = data_dir / 'logs' / 'card-report-precompute.log'

    # --only 让 unit 把每个成品拆成独立进程：单进程连续建时，上一个 scope 的堆还没归还，
    # 下一个 guard 必然判低（实测建完 100-all 只剩 120–198MB），一轮只能补一个成品。
    scopes, matrices = list(SCOPES), list(MATRIX_SCOPES)
    if args.only:
        if args.only.startswith('matrix'):
            rest = args.only.split(':', 1)[1] if ':' in args.only else '200:200'
            own, _, opponent = rest.partition(':')
            matrices = [(own, opponent or '200')]
            scopes = []
        else:
            own, _, opponent = args.only.partition(':')
            scopes = [(own, opponent or 'all')]
            matrices = []

    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass

    cache_dir = data_dir / 'cache'
    pause = pause_reason()
    if pause:
        log(log_path, 'skip_pause %s' % pause)
        print('skip_pause')
        return 0
    reason = guard()
    if reason:
        # 任一成品已过刷新目标时，按请求端同一档阈值放行（各 scope 后面仍逐个再判）。
        overdue = False
        for own, opponent in scopes:
            limit = args.heavy_interval if (own, opponent) in HEAVY_SCOPES else NORMAL_INTERVAL
            age = artifact_age(cache_dir / ('card-report-%s-%s.json' % (own, opponent)))
            if age is None or age > limit:
                overdue = True
                break
        for own, opponent in matrices:
            age = artifact_age(cache_dir / ('matrix-%s-%s.json' % (own, opponent)))
            if age is None or age > args.matrix_interval:
                overdue = True
                break
        blocked = rebuild_guard() if overdue else 'artifacts_fresh'
        if blocked:
            log(log_path, 'skip_guard %s override=%s' % (reason, blocked))
            print('skip_guard', reason)
            return 0
        log(log_path, 'guard_override %s' % reason)
    if args.dry_run:
        print('dry_run scopes', scopes, 'matrix', matrices, 'data', data_dir, 'replays', replay_root)
        return 0

    try:
        lock = open(cache_dir / 'card-report-precompute.lock', 'w', encoding='utf-8')
    except OSError:
        log(log_path, 'lock_open_failed')
        return 0
    try:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            log(log_path, 'already_running')
            return 0

        db_path = data_dir / 'bot.sqlite3'
        for own, opponent in scopes:
            scope = (own, opponent)
            path = cache_dir / ('card-report-%s-%s.json' % scope)
            if scope in HEAVY_SCOPES and not args.force:
                try:
                    age = time.time() - path.stat().st_mtime
                except OSError:
                    age = None
                if age is not None and age < args.heavy_interval:
                    log(log_path, 'skip_heavy age=%ds scope=%s-%s' % (age, own, opponent))
                    continue
            reason = guard()
            if reason:
                age = artifact_age(path)
                limit = args.heavy_interval if scope in HEAVY_SCOPES else NORMAL_INTERVAL
                if (age is None or age > limit) and rebuild_guard() is None:
                    log(log_path, 'guard_override %s scope=%s-%s' % (reason, own, opponent))
                    reason = None
            if reason:
                log(log_path, 'skip_guard %s scope=%s-%s' % (reason, own, opponent))
                continue
            started = time.monotonic()
            try:
                db = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, timeout=20)
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                try:
                    body = compute(db, replay_root, own, opponent)
                finally:
                    db.rollback()
                    db.close()
            except Exception as exc:  # noqa
                log(log_path, 'error scope=%s-%s %s: %s' % (own, opponent,
                                                             type(exc).__name__, str(exc)[:120]))
                continue
            saved = time.time()
            try:
                write_atomic(path, own, opponent, body, saved)
            except OSError as exc:
                log(log_path, 'write_failed scope=%s-%s %s' % (own, opponent, type(exc).__name__))
                continue
            log(log_path, 'ok scope=%s-%s bytes=%d seconds=%.1f' % (
                own, opponent, len(body.encode('utf8')), time.monotonic() - started))
            del body
            release_memory()
        for own, opponent in matrices:
            scope = (own, opponent)
            path = cache_dir / ('matrix-%s-%s.json' % scope)
            if not args.force:
                try:
                    age = time.time() - path.stat().st_mtime
                except OSError:
                    age = None
                if age is not None and age < args.matrix_interval:
                    log(log_path, 'skip_matrix age=%ds scope=%s-%s' % (age, own, opponent))
                    continue
            reason = guard()
            if reason:
                age = artifact_age(path)
                if (age is None or age > args.matrix_interval) and rebuild_guard() is None:
                    log(log_path, 'guard_override %s scope=matrix-%s-%s' % (reason, own, opponent))
                    reason = None
            if reason:
                log(log_path, 'skip_guard %s scope=matrix-%s-%s' % (reason, own, opponent))
                continue
            started = time.monotonic()
            try:
                db = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, timeout=20)
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                try:
                    body = compute_matrix(db, replay_root, own, opponent)
                finally:
                    db.rollback()
                    db.close()
            except Exception as exc:  # noqa
                log(log_path, 'error scope=matrix-%s-%s %s: %s' % (
                    own, opponent, type(exc).__name__, str(exc)[:120]))
                continue
            saved = time.time()
            try:
                write_atomic(path, own, opponent, body, saved)
            except OSError as exc:
                log(log_path, 'write_failed scope=matrix-%s-%s %s' % (
                    own, opponent, type(exc).__name__))
                continue
            log(log_path, 'ok_matrix scope=%s-%s bytes=%d seconds=%.1f' % (
                own, opponent, len(body.encode('utf8')), time.monotonic() - started))
            del body
            release_memory()
        log(log_path, 'done')
        return 0
    finally:
        try:
            lock.close()
        except OSError:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
