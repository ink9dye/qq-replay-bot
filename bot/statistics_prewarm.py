# -*- coding: utf-8 -*-
"""常用真实条件下的预热：只触发既有查询与缓存管道，不新建表、不写事实库。

覆盖的真实页面请求（参数逐键对齐页面 `currentParams()` / `collect()`）：
- 录像页：我方前100/对手前100、我方前200/对手前200（含空 player=）；
- 卡片页默认宽表：前200/前200 与 前100/前100（seat=all、四 phase、metric=opening、
  threshold=1、min=0、sort=rate；页面不发 partition/limit，走服务端默认）。

轮转与让路：
- 每轮只排 **一个** key（轮转 4 个），因此用户请求最多排在 1 个后台计算之后；
- 排之前检查：刷新 worker 有 pending/排队、或 Side 批次持锁 → 整轮跳过；
- 内存门槛：MemAvailable 低于“400MB 预留 + 该 key 峰值余量” → 跳过并记录；
- 依赖 QueryRefresh：`background=True` 只在没有其他后台任务时排队，天然让路；
- 失败不重试，由 QueryRefresh 的 errors/COOLDOWN 处理；旧结果只以 stale 出现，不标最新。

观察：每轮把每个 key 的最近成功时间/年龄、跳过原因、实际间隔写入 `data/prewarm-state.json`
（只观察，不改 TTL、不承诺持续覆盖）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode

PHASES = ('main_first', 'main_second', 'side_first', 'side_second')
DEFAULT_SCOPES = (('100', '100'), ('200', '200'))
RECORDINGS_PARAMS = dict(mode='drawn', match='any', not_match='all', opponent_mode='drawn',
                         opponent_match='any', opponent_not_match='all')
CARD_PARAMS = dict(seat='all', metric='opening', threshold='1', min='0', sort='rate')
MEMINFO = Path('/proc/meminfo')
# 预热在 web 进程内：400MB 预留 + 该 key 的瞬时峰值余量
MIN_AVAILABLE_MB = {'recordings': 550, 'card-report': 650}


def recordings_query(own, opponent):
    """录像页 currentParams() 默认模式下的查询串（含空 player=）。"""
    pairs = list(RECORDINGS_PARAMS.items()) + [('self_range', own), ('opponent_range', opponent),
                                               ('player', '')]
    pairs += [('phase', phase) for phase in PHASES]
    return urlencode(pairs)


def card_query(own, opponent):
    """卡片页 collect() 默认控件下的查询串（无 code/deck/exclude/combo/partition/limit）。"""
    pairs = list(CARD_PARAMS.items())
    pairs += [('phase', phase) for phase in PHASES]
    pairs += [('self_range', own), ('opponent_range', opponent)]
    return urlencode(pairs)


def plan(scopes=DEFAULT_SCOPES, include_recordings=True, include_card=True):
    steps = []
    for own, opponent in scopes:
        if include_recordings:
            steps.append(('recordings', own, opponent))
        if include_card:
            steps.append(('card-report', own, opponent))
    return tuple(steps)


def available_mb(meminfo_path=None):
    path = Path(meminfo_path or MEMINFO)
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def side_lock(data_dir):
    return Path(data_dir) / 'side-inference.lock'


def busy(portal):
    """有排队/在算，或 Side 批次在跑时让路；返回原因或 None。"""
    refresh = getattr(portal, '_refresh', None)
    if refresh is None:
        return 'no_refresh_worker'
    lock = getattr(refresh, 'lock', None)
    if lock is None:
        return 'no_refresh_lock'
    with lock:
        if refresh.pending or not refresh.jobs.empty():
            return 'refresh_busy'
    if side_lock(Path(portal.database).parent).exists():
        return 'side_batch_running'
    return None


def state_path(portal):
    return Path(portal.database).parent / 'prewarm-state.json'


def _atomic_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(target)


def normalize(qs):
    """与门户 _cached_query_reply 相同的 key 归一化（参数排序）。"""
    return urlencode(sorted(parse_qs(qs, keep_blank_values=True).items()), doseq=True)


def snapshot(portal, steps, now=None):
    """每个 key 的最近成功时间与年龄（只读观察，不改 TTL/版本规则）。"""
    now = time.time() if now is None else now
    refresh = getattr(portal, '_refresh', None)
    keys = {}
    for kind, own, opponent in steps:
        raw = recordings_query(own, opponent) if kind == 'recordings' else card_query(own, opponent)
        item = refresh.items.get((kind, normalize(raw))) if refresh is not None else None
        saved = item['saved'] if item else None
        keys['%s/%s-%s' % (kind, own, opponent)] = dict(
            saved_at=round(saved, 3) if saved else None,
            age_seconds=round(now - saved, 1) if saved else None)
    return dict(updated_at=round(now, 3), keys=keys)


def record(portal, payload):
    try:
        _atomic_json(state_path(portal), payload)
    except OSError:
        pass


def warm_step(portal, steps, cursor=0, meminfo_path=None):
    """按 cursor 预热一个 key；返回 (摘要 dict, 下一个 cursor)。

    摘要里带 skip 原因（refresh_busy / side_batch_running / memory_low / meminfo_unavailable），
    并每轮写一次 `prewarm-state.json` 供外部核对最近成功时间与实际间隔。
    """
    from urllib.parse import parse_qs
    from .statistics_recording_filters import query as recordings_fn
    from .statistics_card_report import query as card_fn
    from .card_statistics import Filters
    from .statistics_rank_scope import parse_ranges, scoped_query

    if not steps:
        return dict(skipped='no_steps', cursor=cursor), cursor
    kind, own, opponent = steps[cursor % len(steps)]
    scope = '%s-%s' % (own, opponent)
    free = available_mb(meminfo_path)
    if free is None:
        reason = 'meminfo_unavailable'
    elif free < MIN_AVAILABLE_MB[kind]:
        reason = 'memory_low'
    else:
        reason = busy(portal)
    if reason:
        payload = snapshot(portal, steps)
        payload.update(last_skip=dict(step=kind, scope=scope, reason=reason,
                                      available_mb=free, at=round(time.time(), 3)),
                       cursor=cursor)
        record(portal, payload)
        return dict(skipped=reason, cursor=cursor, available_mb=free), (cursor + 1) % len(steps)

    started = time.monotonic()
    if kind == 'recordings':
        query_string = recordings_query(own, opponent)
        args = parse_qs(query_string)

        def compute(db, args=args):
            return recordings_fn(db, portal.replay_root, float(portal.clock()), args)

        result = portal._cached_query_reply(('recordings', query_string), compute,
                                            ttl=30.0, background=True)
    else:
        query_string = card_query(own, opponent)
        args = parse_qs(query_string, keep_blank_values=True)
        ranges = parse_ranges(args)
        filters = Filters((), (), (), (), None, False, deck_contains=True)

        def compute(db, args=args, ranges=ranges):
            return scoped_query(ranges, lambda: card_fn(
                db, portal.replay_root, float(portal.clock()), filters=filters,
                metric=args.get('metric', ['opening'])[0],
                threshold=args.get('threshold', ['1'])[0],
                min_games=int(args.get('min', ['0'])[0]),
                sort=args.get('sort', ['rate'])[0],
                partition=args.get('partition', ['auto'])[0],
                limit=args.get('limit', ['500'])[0],
                phases=args.get('phase', [])))

        result = portal._cached_query_reply(('card-report', query_string), compute,
                                            ttl=30.0, background=True)
    step = dict(step=kind, scope=scope, status=result[0],
                seconds=round(time.monotonic() - started, 3), bytes=len(result[2]),
                available_mb=free, cursor=cursor)
    payload = snapshot(portal, steps)
    payload.update(last_step=dict(step=kind, scope=scope, status=result[0],
                                  at=round(time.time(), 3)), cursor=(cursor + 1) % len(steps))
    record(portal, payload)
    return step, (cursor + 1) % len(steps)


def config_section(config):
    section = (config or {}).get('prewarm') or {}
    if not section.get('enabled'):
        return {}
    scopes = []
    for item in section.get('scopes') or [list(pair) for pair in DEFAULT_SCOPES]:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            own, opponent = str(item[0]), str(item[1])
            if own in ('100', '200', 'all') and opponent in ('100', '200', 'all'):
                scopes.append((own, opponent))
    scopes = tuple(scopes) or DEFAULT_SCOPES
    steps = plan(scopes, bool(section.get('include_recordings', True)),
                 bool(section.get('include_card', True)))
    return dict(scopes=scopes, steps=steps,
                interval=max(300, int(section.get('interval_seconds') or 300)))
