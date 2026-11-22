#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预生成卡组/对手候选（deck-options）到 query-cache/last-success。

为什么：`/statistics/api/groups?candidates=1` 走 `deck_candidates()`，冷启动是一次
全量资格扫描（实测约 14 s）。页面默认范围只有三个（100:all、100:100、200:200），
提前算好写进网页本来就会读的 last-success 缓存后，请求直接拿到结果；未预存的长尾
范围仍按原路径计算并落盘（不改成“缺失即拒绝”）。

只写 `data/query-cache/last-success/`，不写数据库、不改统计口径。
资源不足或压力过高时整轮跳过，保留上一版缓存。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAUSE_FILE = Path('/run/ygobot-batch-pause')
AVAILABLE_MIN_MB = 250
PSI_MAX = 80.0
LOAD_MAX = 4.0
SCOPES = (('100', 'all'), ('100', '100'), ('200', '200'))


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
    if PAUSE_FILE.exists():
        return 'batch_pause'
    free = available_mb()
    if free is not None and free < AVAILABLE_MIN_MB:
        return 'memory_low %.0fMB' % free
    if io_pressure() >= PSI_MAX:
        return 'io_pressure %.0f' % io_pressure()
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    if load is not None and load > LOAD_MAX:
        return 'load_high %.2f' % load
    return None


def log(path, message):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message))
    except OSError:
        pass


def cache_path(directory: Path, key) -> Path:
    """与 QueryRefresh._path 完全一致，网页端才能按同一个 key 读到。"""
    name = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
    if key[0] in ('card-options', 'deck-options'):
        name = 'labels-' + name
    return directory / (name + '.json')


def store(path: Path, body: str) -> None:
    """与 QueryRefresh._persist 的文档格式一致：result[2] 在盘上是字符串。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = dict(saved=time.time(),
                    result=[200, 'application/json; charset=utf-8', body, {}])
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(document, ensure_ascii=False), encoding='utf8')
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/opt/ygobot/config.json')
    parser.add_argument('--data-dir', default='')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--only', default='', help='只做一个范围，例如 100:all')
    args = parser.parse_args()

    try:
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        config = {}
    data_dir = Path(args.data_dir or config.get('data_dir') or '/opt/ygobot/data')
    replay_root = Path(config.get('replay_dir') or data_dir / 'replays')
    log_path = data_dir / 'logs' / 'deck-options-precompute.log'
    directory = data_dir / 'query-cache' / 'last-success'

    try:
        os.nice(15)
    except (AttributeError, OSError):
        pass

    reason = guard()
    if reason:
        log(log_path, 'skip_guard %s' % reason)
        print('skip_guard', reason)
        return 0
    if args.dry_run:
        print('dry_run scopes', SCOPES, 'data', data_dir)
        return 0

    from bot.statistics_groups import deck_candidates

    scopes = SCOPES
    if args.only:
        own, _, opponent = args.only.partition(':')
        scopes = ((own, opponent or 'all'),)

    db_path = data_dir / 'bot.sqlite3'
    failures = 0
    for own, opponent in scopes:
        reason = guard()
        if reason:
            log(log_path, 'skip_guard %s scope=%s-%s' % (reason, own, opponent))
            continue
        scope_args = {'candidates': ['1'], 'page': ['cards'],
                      'self_range': [own], 'opponent_range': [opponent]}
        key = ('deck-options', urlencode(sorted(scope_args.items()), doseq=True))
        started = time.monotonic()
        try:
            db = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, timeout=20)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            try:
                data = deck_candidates(db, replay_root, time.time(), scope_args)
            finally:
                db.rollback()
                db.close()
            body = json.dumps(data, ensure_ascii=False)
            store(cache_path(directory, key), body)
            log(log_path, 'ok scope=%s-%s bytes=%d seconds=%.1f' % (
                own, opponent, len(body.encode('utf8')), time.monotonic() - started))
            print('ok scope=%s-%s seconds=%.1f' % (own, opponent, time.monotonic() - started))
        except Exception as exc:  # noqa: BLE001 - 失败保留旧缓存
            failures += 1
            log(log_path, 'error scope=%s-%s %s: %s' % (
                own, opponent, type(exc).__name__, str(exc)[:160]))
            print('error scope=%s-%s %s' % (own, opponent, type(exc).__name__))
    return 1 if failures == len(scopes) else 0


if __name__ == '__main__':
    raise SystemExit(main())
