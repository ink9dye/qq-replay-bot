#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时把「前百饼图」渲染到 data/cache/deck-pie/，让 QQ 与网页只读成品。

产物（全部落盘，不常驻内存）：
  latest.png            带缓存 key 前缀的整图
  latest-pie.jpg        机器人发送用的饼图
  latest-legend.jpg     机器人发送用的图例
  latest.json           名称/人数元数据

请求路径只读这些文件；本脚本由 timer 每 2 小时跑一次，单进程、限内存。
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAUSE_FILE = Path('/run/ygobot-batch-pause')
AVAILABLE_MIN_MB = 250


def available_mb():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def load_average():
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def log(path, message):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message))
    except OSError:
        pass


def main() -> int:
    try:
        config = json.loads(Path('/opt/ygobot/config.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        config = {}
    data_dir = Path(config.get('data_dir') or '/opt/ygobot/data')
    replay_root = Path(config.get('replay_dir') or data_dir / 'replays')
    log_path = data_dir / 'logs' / 'deck-pie-precompute.log'

    try:
        os.nice(15)
    except (AttributeError, OSError):
        pass

    if PAUSE_FILE.exists():
        log(log_path, 'skip_pause batch_pause')
        print('skip_pause')
        return 0
    free = available_mb()
    if free is not None and free < AVAILABLE_MIN_MB:
        log(log_path, 'skip_guard memory_low %.0fMB' % free)
        print('skip_guard memory_low')
        return 0
    load = load_average()
    if load is not None and load > 4.0:
        log(log_path, 'skip_guard load_high %.2f' % load)
        print('skip_guard load_high')
        return 0

    started = time.monotonic()
    try:
        from bot import statistics_pie_image
        blob = statistics_pie_image.render(data_dir / 'bot.sqlite3', replay_root, time.time())
    except Exception as exc:  # noqa: BLE001 - 失败保留上一版成品
        log(log_path, 'error %s: %s' % (type(exc).__name__, str(exc)[:160]))
        print('error', type(exc).__name__)
        return 0
    log(log_path, 'ok bytes=%d seconds=%.1f available=%.0fMB' % (
        len(blob), time.monotonic() - started, available_mb() or 0))
    print('ok bytes=%d' % len(blob))
    return 0


if __name__ == '__main__':
    for name in ('SIGTERM', 'SIGINT'):
        try:
            signal.signal(getattr(signal, name), lambda *a: sys.exit(0))
        except (AttributeError, ValueError, OSError):
            pass
    raise SystemExit(main())
