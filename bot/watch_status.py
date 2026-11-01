# -*- coding: utf-8 -*-
"""watch 存活状态文件（D27 配套）。

`share.mode=process` 之后，8787 的 /health 只代表分享进程，不能再拿来判断"watch 在不在跑"。
watch 自己周期写 `data/watch-status.json`（pid/started_at/updated_at/roles/health），
pycharm_link、launcher 等读它；读不到/过期/损坏/已停止一律视为"watch 未运行"（保守：宁可让人确认）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILE_NAME = 'watch-status.json'
MAX_AGE_SECONDS = 45.0


def path_for(data_dir) -> Path:
    return Path(data_dir) / FILE_NAME


def write(data_dir, *, pid=None, roles=(), started_at=None, status='running', entry='', now=None) -> dict:
    """原子写状态文件；watch 进程每 10 秒调用一次。"""
    moment = time.time() if now is None else float(now)
    payload = {
        'pid': int(pid or os.getpid()),
        'started_at': float(started_at or 0.0),
        'updated_at': moment,
        'roles': sorted({str(role) for role in roles}),
        'health': {'service': 'qq-replay-bot', 'status': str(status or 'running')},
        'watch_entry': str(entry or ''),
    }
    path = path_for(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)
    return payload


def read(data_dir, *, max_age=MAX_AGE_SECONDS, now=None):
    """返回有效状态；过期/损坏/已停止/pid 非法一律 None。"""
    try:
        payload = json.loads(path_for(data_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        pid = int(payload.get('pid') or 0)
        updated = float(payload.get('updated_at') or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or updated <= 0:
        return None
    health = payload.get('health') if isinstance(payload.get('health'), dict) else {}
    if str(health.get('status') or 'running') != 'running':
        return None
    moment = time.time() if now is None else float(now)
    if moment - updated > float(max_age):
        return None
    return payload


def as_health(payload):
    """转成 8787 /health 的兼容形状（pycharm_link / launcher 旧调用方用）。"""
    if not payload:
        return None
    return {
        'service': 'qq-replay-bot',
        'status': 'running',
        'watch_pid': int(payload.get('pid') or 0),
        'watch_entry': str(payload.get('watch_entry') or ''),
        'service_roles': list(payload.get('roles') or []),
        'updated_at': payload.get('updated_at'),
        'source': 'watch-status.json',
    }
