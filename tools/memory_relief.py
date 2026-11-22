"""低内存自动泄压：先暂停后台批处理，极端情况下才重启 ygo-web，独立于 web 进程。

2026-11-22 调整：原策略是"可用内存连续 3 次低于 250 MB 就重启 ygo-web"。实测 web
大部分时间常驻只剩 30–40 MB（420 MB 在 swap 里），重启释放不了多少内存，却会清空
页缓存、打断正在跑的查询，当天已触发 4 次。现改为：
  1. 连续 2 次低于 280 MB → 写 /run/ygobot-batch-pause，让预计算任务本轮跳过；
  2. 回到 340 MB 以上 → 删除暂停标记；
  3. 只有跌破 110 MB 连续 5 次且距上次重启超过 1 小时，才重启（11-03 整机 fork 堵死的兜底）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_THRESHOLD_MB = 250
DEFAULT_SAMPLES = 3
DEFAULT_COOLDOWN_SECONDS = 1800
STATE_VERSION = 1

# 暂停后台批处理（第一响应）；恢复阈值要高于暂停阈值，避免在边界反复抖动。
PAUSE_FILE = Path('/run/ygobot-batch-pause')
PAUSE_AT_MB = 240
RESUME_AT_MB = 310
PAUSE_SAMPLES = 2
# 重启只作为整机 fork 堵死的兜底，阈值远低于暂停线。
RESTART_THRESHOLD_MB = 110
RESTART_SAMPLES = 5
RESTART_COOLDOWN_SECONDS = 3600


def read_available_mb(meminfo: Path = Path('/proc/meminfo')) -> int | None:
    """返回 MemAvailable（MB）；读不到返回 None，调用方不得据此触发。"""
    try:
        for line in Path(meminfo).read_text(encoding='utf-8').splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def evaluate(state: dict, available_mb: int | None, now: float,
             threshold_mb: int = DEFAULT_THRESHOLD_MB,
             needed: int = DEFAULT_SAMPLES,
             cooldown: int = DEFAULT_COOLDOWN_SECONDS) -> tuple[dict, str]:
    """纯函数：返回 (新状态, 'restart'|'none')。读不到内存时不累计、不触发。"""
    new = dict(state or {})
    new['version'] = STATE_VERSION
    new['updated'] = float(now)
    new['last_available_mb'] = available_mb
    if available_mb is None:
        new['low_count'] = int(new.get('low_count') or 0)
        return new, 'none'
    low = available_mb < threshold_mb
    count = int(new.get('low_count') or 0) + 1 if low else 0
    last_restart = float(new.get('last_restart') or 0)
    action = 'none'
    if low and count >= needed and (last_restart <= 0 or now - last_restart >= cooldown):
        action = 'restart'
        last_restart = float(now)
        count = 0
    new['low_count'] = count
    new['last_restart'] = last_restart
    return new, action


def load_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    os.replace(tmp, path)


def restart_web(runner=subprocess.run) -> int:
    try:
        return int(runner(['systemctl', 'restart', 'ygo-web'], check=False).returncode)
    except (OSError, ValueError):
        return -1


def log(path: Path, message: str) -> None:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), message))
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description='低内存时先暂停后台批处理，极端情况才重启 ygo-web')
    parser.add_argument('--threshold-mb', type=int, default=RESTART_THRESHOLD_MB)
    parser.add_argument('--samples', type=int, default=RESTART_SAMPLES)
    parser.add_argument('--cooldown', type=int, default=RESTART_COOLDOWN_SECONDS)
    parser.add_argument('--pause-at-mb', type=int, default=PAUSE_AT_MB)
    parser.add_argument('--resume-at-mb', type=int, default=RESUME_AT_MB)
    parser.add_argument('--state', type=Path, default=root / 'data' / 'memory-relief-state.json')
    parser.add_argument('--log', type=Path, default=root / 'data' / 'logs' / 'memory-relief.log')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)

    now = time.time()
    available = read_available_mb()
    state = load_state(args.state)

    # 第一步：低内存先暂停后台批处理；回到高水位再恢复。只写 tmpfs，不碰业务。
    pause_action = 'none'
    if available is not None:
        try:
            paused = PAUSE_FILE.exists()
        except OSError:
            paused = False
        if available < args.pause_at_mb:
            count = int(state.get('pause_low_count') or 0) + 1
            state['pause_low_count'] = count
            if not paused and count >= PAUSE_SAMPLES:
                pause_action = 'pause'
        else:
            state['pause_low_count'] = 0
            if paused and available >= args.resume_at_mb:
                pause_action = 'resume'
        if pause_action != 'none':
            state['pause_at'] = now

    if not args.dry_run:
        if pause_action == 'pause':
            try:
                PAUSE_FILE.write_text(
                    'available=%dMB at=%s\n' % (available, time.strftime('%Y-%m-%dT%H:%M:%S')),
                    encoding='utf-8')
                os.chmod(PAUSE_FILE, 0o644)
            except OSError:
                pause_action = 'pause_failed'
        elif pause_action == 'resume':
            try:
                PAUSE_FILE.unlink()
            except OSError:
                pass

    # 第二步：只有极端低内存才重启（兜住整机 fork 堵死）。
    new_state, action = evaluate(state, available, now,
                                 threshold_mb=args.threshold_mb,
                                 needed=args.samples,
                                 cooldown=args.cooldown)
    line = 'available=%s low_count=%d action=%s pause=%s' % (
        'unknown' if available is None else '%dMB' % available,
        int(new_state.get('low_count') or 0), action, pause_action)
    if args.dry_run:
        print(line)
        return 0
    if action == 'restart':
        code = restart_web()
        line += ' restart_rc=%d' % code
        log(args.log, line)
        save_state(args.state, new_state)
        return 0 if code == 0 else 1
    log(args.log, line)
    save_state(args.state, new_state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
