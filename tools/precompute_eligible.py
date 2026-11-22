#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时刷新资格视角快照（data/cache/eligible-views.sqlite3）。

只写这一个快照文件，不写业务库、不改统计口径：内容就是 eligible_views 的一次真实输出。
资源不足或压力过高时整轮跳过，保留上一版快照；单实例锁避免并发刷新。
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PSI_MAX = 50.0
AVAILABLE_MIN_MB = 300
LOAD_MAX = 3.5
MAX_SECONDS = 600
# 构建中判定：构建自身要占 200–240 MB，拿「系统剩余内存」直接和 300 MB 比必然自我推穿。
# 改为只统计外部新增占用，并保留一条更低的硬下限兜住真正的 OOM 风险。
MEMORY_HARD_FLOOR_MB = 120

# 2026-11-22 守卫死锁修复：REFRESH_LIMITS 的 io PSI 上限 50 在生产长期不可达
# （11-22 17:14–19:04 连续 12 轮 0 成功，PSI 51–81），快照停更后请求只能回退全量扫库，
# 压力反而更高。快照已过期（或读不出年龄）时，改用与请求端 REQUEST_LIMITS 同一档阈值
# （200 MB / PSI 80 / load 4.0）重建一次；正常刷新仍走原阈值。
PAUSE_FILE = Path('/run/ygobot-batch-pause')
_REBUILD_LIMITS = dict(available_mb=200, psi=80.0, load=4.0)
# 构建中不因「自己造成的 I/O 尖峰」中止：PSI avg10 会把本次扫描算进去，
# 实测基线 50–60 时一开始构建就冲到 82，按 80 拦会把每一轮都杀掉。
# 重建期内只兜真正的内存风险与机器整体过载。
_REBUILD_BUILD_LIMITS = dict(available_mb=200, psi=95.0, load=8.0)
_REBUILD_EXTERNAL_MB = 450
_REBUILD_HARD_FLOOR_MB = 100
_OVERRIDE = dict(active=False)

_STOP = False
_WATCH = dict(done=False, reason=None, count=0, fake_at=0.0, started=0.0, interval=2.0)


def _handle_stop(signum, frame):
    global _STOP
    _STOP = True


def _request_stop():
    global _STOP
    _STOP = True


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
    """与读取端共用一套阈值，避免两处常量漂移。"""
    from bot import eligible_snapshot
    return eligible_snapshot.resource_state('refresh')


def pause_reason():
    """后台批处理暂停标记：由 tools/memory_relief.py 在低内存时写入 /run（tmpfs）。"""
    try:
        if PAUSE_FILE.exists():
            return 'batch_pause'
    except OSError:
        return None
    return None


def override_guard():
    """快照已过期时的重建条件：与请求端同一档，比正常刷新宽一档。"""
    free = available_mb()
    if free is not None and free < _REBUILD_LIMITS['available_mb']:
        return 'memory_low %.0fMB' % free
    pressure = io_pressure()
    if pressure >= _REBUILD_LIMITS['psi']:
        return 'io_pressure %.0f' % pressure
    load = load_average()
    if load is not None and load > _REBUILD_LIMITS['load']:
        return 'load_high %.2f' % load
    return None


def load_average():
    try:
        return float(Path('/proc/loadavg').read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return None


def own_rss_mb():
    """本进程当前常驻内存：构建期间这部分是「预期内的占用」，不算外部压力。"""
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def build_guard(baseline_mb):
    """构建期间的守卫：内存只看外部新增占用，IO 与 load 沿用同一套阈值。

    以构建启动时的可用内存为基线，扣掉本进程自身增长后仍比基线少 300 MB，
    才认定是外部把内存吃掉了；可用跌破 120 MB 则无条件中止（兜住真正的 OOM 风险，
    OOM 本身另有 systemd MemoryMax 兜底）。
    """
    from bot import eligible_snapshot
    if _OVERRIDE['active']:
        external_limit = _REBUILD_EXTERNAL_MB
        floor = _REBUILD_HARD_FLOOR_MB
    else:
        external_limit = eligible_snapshot.REFRESH_LIMITS['available_mb']
        floor = MEMORY_HARD_FLOOR_MB
    # I/O 判定只兜极端值：PSI avg10 会把本次扫描自身算进去，实测基线 50–60 时开工即冲到
    # 53–82，按 50 拦会让每一轮在十几秒内被杀（11-22 全天重建 0 成功的主要原因之一）。
    # 真正的 OOM 风险仍由内存下限 + 外部新增占用兜住。
    pressure = io_pressure()
    if pressure >= 95.0:
        return 'io_pressure %.0f' % pressure
    load = load_average()
    if load is not None and load > 8.0:
        return 'load_high %.2f' % load
    free = available_mb()
    if free is None:
        return None
    if free < floor:
        return 'memory_critical %.0fMB' % free
    if baseline_mb is not None:
        external = (baseline_mb - own_rss_mb()) - free
        if external > external_limit:
            return 'memory_external %.0fMB' % external
    return None


def _watch_loop():
    """构建期间持续采样：连续两次超限就请求中止，构建完成后不发布，旧版继续服务。"""
    baseline = available_mb()
    while not _WATCH['done']:
        time.sleep(_WATCH['interval'])
        if _WATCH['done']:
            return
        if _WATCH['fake_at'] and time.monotonic() - _WATCH['started'] >= _WATCH['fake_at']:
            reason = 'io_pressure 99'
        else:
            reason = build_guard(baseline)
        if reason:
            _WATCH['count'] += 1
            _WATCH['reason'] = reason
            if _WATCH['count'] >= 2:
                _request_stop()
                return
        else:
            _WATCH['count'] = 0


def _progress():
    return 1 if _STOP else 0


def log(path, message):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message))
    except OSError:
        pass


def parse_scope(value):
    own, _, opponent = value.partition(':')
    if own not in ('100', '200', 'all') or opponent not in ('100', '200', 'all'):
        raise argparse.ArgumentTypeError('范围只支持 100/200/all，例如 100:all')
    return own, opponent or 'all'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/opt/ygobot/config.json')
    parser.add_argument('--data-dir', default='')
    parser.add_argument('--scope', type=parse_scope, default=('100', 'all'),
                        help='own:opponent，默认页面用的 100:all')
    parser.add_argument('--internal', action='store_true', help='只统计前 100 内战')
    parser.add_argument('--max-seconds', type=int, default=MAX_SECONDS)
    parser.add_argument('--now', type=float, default=0.0,
                        help='仅离线复算用：按指定时间点构造快照，默认当前时间')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--target', default='',
                        help='快照文件名；默认 eligible-views.sqlite3，'
                             '前 200 内战用 eligible-views-civil200.sqlite3')
    parser.add_argument('--force', action='store_true',
                        help='忽略资源守卫与新鲜度检查（仅离线使用）')
    parser.add_argument('--simulate-pressure-at', type=float, default=0.0,
                        help='仅离线验收用：构建开始 N 秒后模拟持续超压，验证中止且不发布')
    parser.add_argument('--watch-interval', type=float, default=2.0,
                        help='仅离线验收用：采样间隔（生产保持 2 秒）')
    args = parser.parse_args()

    try:
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        config = {}
    data_dir = Path(args.data_dir or config.get('data_dir') or '/opt/ygobot/data')
    from bot import eligible_snapshot as _snap_name
    snapshot_name = args.target or _snap_name.FILE_NAME
    replay_root = Path(config.get('replay_dir') or data_dir / 'replays')
    log_path = data_dir / 'logs' / 'eligible-snapshot.log'

    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    for name in ('SIGTERM', 'SIGINT'):
        try:
            signal.signal(getattr(signal, name), _handle_stop)
        except (AttributeError, ValueError, OSError):
            pass

    from bot import eligible_snapshot as _snap
    state = _snap.status(data_dir / 'bot.sqlite3', snapshot_name)
    age = _snap.age_seconds(data_dir / 'bot.sqlite3', args.now or time.time(), snapshot_name)

    if not args.force:
        pause = pause_reason()
        if pause:
            log(log_path, 'skip_pause %s' % pause)
            print('skip_pause')
            return 0
        reason = guard()
        if reason:
            # 快照已过期（或读不出年龄）时，按请求端同一档阈值再判一次：
            # 否则守卫永远过不去，请求只能回退全量扫库，压力反而更高。
            stale = age is None or age > _snap.FRESH_AGE
            blocked = override_guard() if stale else 'snapshot_fresh'
            if blocked:
                log(log_path, 'skip_guard %s age=%s override=%s' % (
                    reason, '?' if age is None else '%.0fs' % age, blocked))
                print('skip_guard', reason)
                return 0
            _OVERRIDE['active'] = True
            log(log_path, 'guard_override %s age=%s' % (
                reason, '?' if age is None else '%.0fs' % age))
        # 刷新目标与业务可接受年龄分开：快照还新、参数一致时连构建都不做。
        if (state and age is not None and age <= _snap.FRESH_AGE
                and list(state.get('scope') or ()) == list(args.scope)
                and bool(state.get('internal_only')) == bool(args.internal)):
            log(log_path, 'skip_fresh age=%.0fs' % age)
            print('skip_fresh')
            return 0
    if args.dry_run:
        print('dry_run scope=%s:%s data=%s replays=%s' % (args.scope[0], args.scope[1],
                                                          data_dir, replay_root))
        return 0

    cache_dir = data_dir / 'cache'
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock = open(cache_dir / 'eligible-snapshot.lock', 'w', encoding='utf-8')
    except OSError:
        log(log_path, 'lock_open_failed')
        print('lock_open_failed')
        return 0
    try:
        try:
            import fcntl
        except ImportError:
            fcntl = None
            log(log_path, 'lock_unavailable')
        if fcntl is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                log(log_path, 'already_running')
                print('already_running')
                return 0

        from bot import eligible_snapshot

        started = time.monotonic()
        db_path = data_dir / 'bot.sqlite3'
        _WATCH.update(done=False, reason=None, count=0, started=started,
                      fake_at=args.simulate_pressure_at,
                      interval=max(0.05, args.watch_interval))
        threading.Thread(target=_watch_loop, daemon=True, name='eligible-guard').start()
        try:
            db = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, timeout=20)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            # 压力持续超限时让 SQLite 自己中断当前语句，而不是等构建自然结束。
            db.set_progress_handler(_progress, 4000)
            try:
                payload = eligible_snapshot.build(db, replay_root, args.now or time.time(),
                                                  scope=args.scope,
                                                  internal_only=bool(args.internal))
            finally:
                db.rollback()
                db.close()
        except Exception as exc:  # noqa: BLE001 - 任何失败都保留上一版
            if _STOP:
                log(log_path, 'abort_guard %s scope=%s:%s' % (
                    _WATCH['reason'] or 'signal', args.scope[0], args.scope[1]))
                print('abort_guard')
                return 0
            log(log_path, 'error scope=%s:%s %s: %s' % (args.scope[0], args.scope[1],
                                                        type(exc).__name__, str(exc)[:120]))
            print('error', type(exc).__name__)
            return 0
        finally:
            _WATCH['done'] = True

        elapsed = time.monotonic() - started
        if _STOP:
            log(log_path, 'abort_guard %s seconds=%.1f' % (
                _WATCH['reason'] or 'signal', elapsed))
            print('aborted')
            return 0
        if elapsed > args.max_seconds:
            log(log_path, 'skip_slow seconds=%.1f views=%d' % (elapsed, payload['meta']['views']))
            print('skip_slow')
            return 0
        # 构建完成后再查一次内存会误伤：这次快照自身就占着内存，读到的可用量必然偏低。
        # 峰值已由 MemoryMax 兜住，这里只记录不拦。
        log(log_path, 'built views=%d seconds=%.1f available=%sMB' % (
            payload['meta']['views'], elapsed,
            '?' if available_mb() is None else '%.0f' % available_mb()))
        try:
            state = eligible_snapshot.publish(data_dir / 'bot.sqlite3', payload, name=snapshot_name)
        except (OSError, sqlite3.Error) as exc:
            log(log_path, 'publish_failed %s: %s' % (type(exc).__name__, str(exc)[:120]))
            print('publish_failed')
            return 0
        meta = payload['meta']
        log(log_path, 'ok scope=%s:%s views=%d bytes=%s seconds=%.1f' % (
            args.scope[0], args.scope[1], meta['views'],
            state.get('bytes') if state else '?', elapsed))
        print('ok views=%d bytes=%s seconds=%.1f' % (
            meta['views'], state.get('bytes') if state else '?', elapsed))
        del payload
        gc.collect()
        return 0
    finally:
        try:
            lock.close()
        except OSError:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
