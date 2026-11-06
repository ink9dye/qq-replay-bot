# -*- coding: utf-8 -*-
"""独立、有界的 Side 推断执行器：把批次从 web 请求进程搬出去。

约束（与冻结规则一致）：
- 推断算法、参数、证据规则完全复用 `bot.side_inference.run`，本模块只负责进程外层；
- **单实例**：`data/side-inference.lock` 用 O_EXCL 创建；持有者存活且锁未过期则不执行，
  不会重叠、也不会两路并发覆盖缓存；死进程/过期锁（含 PID 复用）会被接管；
- **内存门槛**：启动前读 MemAvailable，低于 `--min-available-mb` 直接跳过并记录缺口，
  不先启动再等内存跌破阈值；
- **有界**：`--limit` 限制条数；`--max-seconds` 到点即停（已推断的条目仍合并落盘，state 标 partial，
  不算成功）；systemd `TimeoutStartSec` 作为硬兜底；
- **读者安全**：缓存写入经 `side_cache.commit`（排他锁 + 合并 + 临时文件 + os.replace）；
- **不抢交互**：`--idle-require` 时若仍在录或状态过旧就不跑；
- 运行结束写 `--state` 进度文件（原子替换）。

用法（systemd oneshot）：
    python3 -m bot.side_runner --root /opt/ygobot --limit 20 --max-seconds 300 \
        --min-available-mb 900 --state /opt/ygobot/data/side-inference-state.json --idle-require
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

MEMINFO = Path('/proc/meminfo')


def _lock_path(root):
    return Path(root).resolve() / 'data' / 'side-inference.lock'


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def available_mb(meminfo_path=None):
    """MemAvailable（MB）；读不到返回 None（调用方不得据此启动）。"""
    path = Path(meminfo_path or MEMINFO)
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def acquire(root, stale_seconds=7200, now=None):
    """返回 (lock_path, holder)；已有存活且未过期的持有者时返回 (None, holder)。"""
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    for _ in range(2):
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_holder(path)
            started = float((holder or {}).get('started_at') or 0)
            # 存活但已超过 stale_seconds（含 PID 复用）也视为可接管；死进程立即可接管。
            if holder and _pid_alive(holder.get('pid', -1)) and now - started < stale_seconds:
                return None, holder
            try:
                path.unlink()
            except OSError:
                return None, holder
            continue
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(dict(pid=os.getpid(), started_at=now, boot=time.time()), stream)
        return path, None
    return None, _read_holder(path)


def _read_holder(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def release(path):
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def idle_ok(root):
    """仍在录或状态过旧时不跑；返回 (ok, reason)。"""
    status_path = Path(root).resolve() / 'data' / 'capture-status.json'
    try:
        status = json.loads(status_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False, 'capture_status_missing'
    if int(status.get('active_observations') or 0) != 0:
        return False, 'active_observations'
    if time.time() - float(status.get('updated_at') or 0) > 180:
        return False, 'capture_status_stale'
    return True, 'idle'


def _write_state(path, payload):
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(target)


def main(argv=None, meminfo_path=None):
    parser = argparse.ArgumentParser(description='独立 Side 推断执行器')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--max-seconds', type=float, default=300.0)
    parser.add_argument('--min-available-mb', type=int, default=900)
    parser.add_argument('--state', type=Path, default=None)
    parser.add_argument('--idle-require', action='store_true')
    args = parser.parse_args(argv)
    started = time.time()
    state = dict(started_at=started, pid=os.getpid(), limit=max(1, args.limit),
                 min_available_mb=args.min_available_mb)

    ok, reason = (idle_ok(args.root) if args.idle_require else (True, 'not_required'))
    if not ok:
        state.update(result='skipped', reason=reason, finished_at=time.time())
        _write_state(args.state, state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        return 0

    free = available_mb(meminfo_path)
    state['available_mb'] = free
    if free is None:
        state.update(result='skipped', reason='meminfo_unavailable', finished_at=time.time())
        _write_state(args.state, state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        return 0
    if free < args.min_available_mb:
        state.update(result='skipped', reason='memory_low', finished_at=time.time(),
                     gap_mb=args.min_available_mb - free)
        _write_state(args.state, state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        return 0

    lock, holder = acquire(args.root)
    if lock is None:
        state.update(result='skipped', reason='already_running', holder=holder,
                     finished_at=time.time())
        _write_state(args.state, state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        return 0
    try:
        from . import side_inference
        stats = {}
        deadline = time.monotonic() + args.max_seconds if args.max_seconds > 0 else None
        side_inference.run(args.root, limit=max(1, args.limit), deadline=deadline, stats=stats)
        state.update(stats)
        status = stats.get('commit_status')
        if stats.get('partial'):
            state['result'] = 'partial'
            state['reason'] = 'max_seconds'
        elif status == 'conflict':
            state['result'] = 'conflict'
            state['reason'] = 'cache_changed_during_run'
        else:
            state['result'] = 'ok'
    except Exception as exc:  # 独立进程：异常写入 state，返回非零
        state.update(result='error', error='%s: %s' % (type(exc).__name__, str(exc)[:200]))
    finally:
        release(lock)
        state['finished_at'] = time.time()
        state['seconds'] = round(state['finished_at'] - started, 3)
        _write_state(args.state, state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
    return 0 if state.get('result') in ('ok', 'partial', 'conflict') else 1


if __name__ == '__main__':
    sys.exit(main())
