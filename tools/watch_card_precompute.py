#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""观察卡片统计预计算 24 小时：skip_guard 比例、memory.events high 增量、card-report 503 率。

设计：每 5 分钟采样一次，状态写 data/observation/card-precompute-watch.json，
逐样本增量写 .jsonl；到 24 小时写 .md 报告后退出。
开机由 systemd 重新拉起时从状态继续，关机时间计入 24 小时窗口。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

DATA = Path('/opt/ygobot/data')
WATCH_DIR = DATA / 'observation'
STATE = WATCH_DIR / 'card-precompute-watch.json'
SAMPLES = WATCH_DIR / 'card-precompute-watch.jsonl'
REPORT = WATCH_DIR / 'card-precompute-watch-report.md'
CUMULATIVE = WATCH_DIR / 'card-precompute-watch-cumulative.md'
ACCESS = DATA / 'logs' / 'web-access.jsonl'
TIMINGS = DATA / 'logs' / 'statistics-query-timings.jsonl'
PRECOMPUTE = DATA / 'logs' / 'card-report-precompute.log'
QQ = DATA / 'logs' / 'qq-diagnostics.jsonl'
QQ_CGROUP = Path('/sys/fs/cgroup/system.slice/ygo-qq.service')
CGROUP = Path('/sys/fs/cgroup/system.slice/ygo-web.service')

_STOP = False


def _handle_stop(signum, frame):
    global _STOP
    _STOP = True


def now():
    return time.time()


def blank_counters():
    return dict(card_requests=0, card_ok=0, card_fail=0, build_requests=0, build_ok=0, build_fail=0,
                computes=0, computes_fail=0, pre_ok=0, pre_skip=0, pre_error=0,
                high_total=0, high_resets=0, high_last=0, samples=0,
                min_available=10 ** 9, max_swap=0, max_rss=0, skip_reason={},
                qq_replies=0, qq_slow_replies=0, qq_sends=0, qq_send_slow=0,
                qq_send_errors=0, qq_send_uncertain=0, qq_commands=0, qq_slow_cmds=0,
                qq_gaps=0, qq_gap_max_ms=0, qq_last_beat=0,
                qq_reply_ms=[], qq_send_ms=[], qq_cmd_ms=[], qq_starts={},
                qq_totals=dict(replies=0, slow_replies=0, sends=0, send_slow=0,
                               send_errors=0, uncertain=0, commands=0, slow_cmds=0,
                               gaps=0, gap_max_ms=0))


def current_offset(path):
    """新观察从当前文件末尾起算，不把开始之前的请求算进来。"""
    try:
        stat = path.stat()
        return {'inode': stat.st_ino, 'offset': stat.st_size}
    except OSError:
        return {'inode': None, 'offset': 0}


def load_state():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def save_state(state):
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temporary, STATE)


def file_reader(path, state, key):
    """按 offset 增量读取；文件被截断/轮转时从头再来。"""
    info = state['offsets'].setdefault(key, {'inode': None, 'offset': 0})
    try:
        stat = path.stat()
    except OSError:
        return []
    if info['inode'] != stat.st_ino or stat.st_size < info['offset']:
        info['inode'] = stat.st_ino
        info['offset'] = 0
    lines = []
    try:
        with path.open('rb') as handle:
            handle.seek(info['offset'])
            for raw in handle:
                if not raw.endswith(b'\n'):
                    break
                info['offset'] += len(raw)
                try:
                    lines.append(raw.decode('utf-8', 'replace'))
                except ValueError:
                    continue
    except OSError:
        return []
    return lines


def count_access(lines, counters):
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        path = row.get('path', '')
        if path not in ('/statistics/api/card-report', '/statistics/api/build'):
            continue
        key = 'card' if path.endswith('card-report') else 'build'
        counters[key + '_requests'] += 1
        if row.get('status') == 200:
            counters[key + '_ok'] += 1
        else:
            counters[key + '_fail'] += 1


def count_timings(lines, counters):
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('kind') != 'card-report':
            continue
        counters['computes'] += 1
        if row.get('status') != 200:
            counters['computes_fail'] += 1


def count_precompute(lines, started_at, counters):
    for line in lines:
        if len(line) < 19:
            continue
        try:
            stamp = time.mktime(time.strptime(line[:19], '%Y-%m-%dT%H:%M:%S'))
        except ValueError:
            continue
        if stamp < started_at:
            continue
        if ' ok scope=' in line:
            counters['pre_ok'] += 1
        elif ' skip_guard ' in line:
            counters['pre_skip'] += 1
            reason = line.split(' skip_guard ', 1)[1].strip().split()[0]
            counters['skip_reason'][reason] = counters['skip_reason'].get(reason, 0) + 1
        elif ' error scope=' in line or ' write_failed' in line:
            counters['pre_error'] += 1


def memory_events(counters):
    try:
        text = (CGROUP / 'memory.events').read_text(encoding='utf-8')
    except OSError:
        return
    value = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == 'high':
            value = int(parts[1])
            break
    previous = counters.get('high_last', 0)
    if value < previous:
        counters['high_resets'] += 1
        counters['high_total'] += value
    else:
        counters['high_total'] += value - previous
    counters['high_last'] = value


def memory_now():
    info = {'available': None, 'swap_used': None, 'rss': None}
    total = free = None
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            name = line.split(':')[0]
            if name == 'MemAvailable':
                info['available'] = int(line.split()[1]) // 1024
            elif name == 'SwapTotal':
                total = int(line.split()[1]) // 1024
            elif name == 'SwapFree':
                free = int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    if total is not None and free is not None:
        info['swap_used'] = total - free
    try:
        pid = subprocess.run(['systemctl', 'show', 'ygo-web', '-p', 'MainPID'],
                             capture_output=True, text=True, timeout=5).stdout.strip().split('=')[1]
        if pid:
            status = Path('/proc/%s/status' % pid).read_text()
            for line in status.splitlines():
                if line.startswith('VmRSS:'):
                    info['rss'] = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return info


QQ_SLOW_REPLY_MS = 8000
QQ_SLOW_SEND_MS = 3000
QQ_SLOW_CMD_MS = 8000


def _percentile(values, ratio):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def qq_latency_rows(path):
    """Replay complete JSONL records from oldest rotation through the live log."""
    def rotation(candidate):
        suffix = candidate.name.removeprefix(path.name + '.').removesuffix('.gz')
        return int(suffix) if suffix.isdecimal() else 0

    rotated = [candidate for candidate in path.parent.glob(path.name + '.*')
               if rotation(candidate) > 0]
    for candidate in [*sorted(rotated, key=rotation, reverse=True), path]:
        opener = gzip.open if candidate.suffix == '.gz' else open
        try:
            with opener(candidate, 'rt', encoding='utf-8', errors='replace') as source:
                for line in source:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except FileNotFoundError:
            if candidate == path and not rotated:
                raise


_QQ_QUERY_PHASES = (
    'statistics_wait', 'statistics_hold', 'executor_wait', 'handler',
    'deck_cache_hit', 'deck_index_replays', 'deck_recover_sessions',
    'deck_visible_replays', 'deck_replay_ids', 'deck_share_link',
    'deck_build_query', 'deck_thumb_wait', 'deck_image_render', 'deck_rank_lookup',
)


def qq_latency_report(rows, since, until, operation):
    """One sample per trace. A timeout leaves delivery unconfirmed."""
    received, commands, active_sends, operations = {}, {}, {}, {}
    all_operations = operation == 'all'
    drops, unknown_drops = Counter(), Counter()
    dropped_source_ages = []
    drop_reasons = {'command_rate_limit', 'duplicate_event', 'dispatch_capacity', 'not_connected'}
    for row in rows:
        stamp = row.get('time')
        trace = row.get('trace')
        if not isinstance(stamp, (int, float)) or stamp >= until:
            continue
        event = row.get('event')
        reason = row.get('reason')
        scope = (row.get('pid'), row.get('session'))
        key = (*scope, trace)
        if since <= stamp and event in ('message_ignored', 'event_dropped') and reason in drop_reasons:
            drop_operation = row.get('operation')
            if (all_operations and drop_operation not in (None, '', 'unknown', 'empty')) or drop_operation == operation:
                drops[reason] += 1
                if key in received:
                    dropped_source_ages.append(received[key][1])
            elif not drop_operation or drop_operation in ('unknown', 'empty'):
                unknown_drops[reason] += 1
        if not trace:
            continue
        action = row.get('action') or ''
        attempt_key = (*key, action)
        if event == 'command_timeline_start':
            operations[key] = row.get('operation')
        elif event == 'command_lifecycle_complete':
            operations.pop(key, None)
        if action.startswith('send_'):
            if event == 'api_begin':
                active_sends[attempt_key] = dict(trace=trace, operation=operations.get(key),
                                                 action=action, began_at=stamp, finished_at=None,
                                                 error=None, retcode=None)
            elif event in ('api_response', 'api_error'):
                active = active_sends.pop(attempt_key, None)
                if active is not None:
                    active.update(finished_at=stamp, error=row.get('error_type'), retcode=row.get('retcode'))
        if stamp < since - 30:
            continue
        if event == 'message_received':
            received[key] = (stamp, row.get('source_age_ms'))
        elif event == 'command_timeline_start' and stamp >= since and (all_operations or row.get('operation') == operation):
            received_at, source_age_ms = received.pop(key, (None, None))
            commands[key] = dict(trace=trace, operation=row.get('operation'), started_at=stamp,
                                 received_at=received_at, source_age_ms=source_age_ms,
                                 first_queued_at=None, receive_to_queue_ms=None, dispatch_wait_ms=None, send_wait_ms=[],
                                 send_attempts=[], in_flight_at_first_queue=None,
                                 first_confirmed_reply_ms=None, last_result_ms=None, phase_totals_ms={},
                                 lifecycle_ms=None, bot_outcome='incomplete')
        sample = commands.get(key)
        if sample is None:
            continue
        ms = row.get('elapsed_ms')
        if event == 'reply_queued' and sample['in_flight_at_first_queue'] is None:
            start = sample['received_at']
            if start is not None and stamp >= start:
                sample['receive_to_queue_ms'] = round((stamp - start) * 1000, 3)
            sample['first_queued_at'] = stamp
            sample['in_flight_at_first_queue'] = [
                send for (pid, session, other_trace, _), send in active_sends.items()
                if (pid, session) == scope and other_trace != trace and send['began_at'] <= stamp
            ]
        elif event == 'phase_complete' and row.get('phase') == 'dispatch_wait':
            sample['dispatch_wait_ms'] = ms
        elif event == 'phase_complete' and row.get('phase') == 'send_wait':
            if isinstance(ms, (int, float)):
                sample['send_wait_ms'].append(ms)
        elif event == 'phase_complete' and row.get('phase') in _QQ_QUERY_PHASES and isinstance(ms, (int, float)):
            phase_name = row['phase']
            totals = sample['phase_totals_ms']
            totals[phase_name] = round(totals.get(phase_name, 0) + ms, 3)
        elif action.startswith('send_') and event in ('api_response', 'api_error'):
            sample['send_attempts'].append(dict(action=action, elapsed_ms=ms,
                                               retcode=row.get('retcode'), error=row.get('error_type'),
                                               delivery_uncertain=bool(row.get('delivery_uncertain'))))
        elif event == 'reply_milestone' and row.get('success') and row.get('actual_send'):
            if sample['first_confirmed_reply_ms'] is None:
                sample['first_confirmed_reply_ms'] = ms
        elif event == 'command_lifecycle_complete':
            sample['lifecycle_ms'] = ms
            sample['last_result_ms'] = row.get('last_result_ms')
            sample['bot_outcome'] = row.get('outcome')

    samples = list(commands.values())
    metrics = dict(source_age_ms=[s['source_age_ms'] for s in samples],
                   dropped_source_age_ms=dropped_source_ages,
                   receive_to_queue_ms=[s['receive_to_queue_ms'] for s in samples],
                   send_wait_ms=[ms for s in samples for ms in s['send_wait_ms']],
                   onebot_response_ms=[a['elapsed_ms'] for s in samples for a in s['send_attempts']
                                      if a['retcode'] == 0 and not a['error']],
                   first_confirmed_reply_ms=[s['first_confirmed_reply_ms'] for s in samples],
                   last_result_ms=[s['last_result_ms'] for s in samples])
    for phase_name in _QQ_QUERY_PHASES:
        metrics[phase_name + '_ms'] = [sample['phase_totals_ms'].get(phase_name) for sample in samples]
    summary = {}
    for name, values in metrics.items():
        values = [value for value in values if isinstance(value, (int, float))]
        summary[name] = dict(count=len(values), p50=_percentile(values, .50),
                             p95=_percentile(values, .95), max=max(values, default=None))
    coverage = {}
    for sample in samples:
        label = sample['operation'] or 'missing_operation'
        counts = coverage.setdefault(label, dict(started=0, lifecycle_recorded=0, result_confirmed=0,
                                                  source_timestamp_recorded=0, statistics_hold_recorded=0))
        counts['started'] += 1
        counts['lifecycle_recorded'] += sample['lifecycle_ms'] is not None
        counts['result_confirmed'] += sample['last_result_ms'] is not None
        counts['source_timestamp_recorded'] += sample['source_age_ms'] is not None
        counts['statistics_hold_recorded'] += 'statistics_hold' in sample['phase_totals_ms']
    return dict(operation=operation, since=since, until=until, commands=len(samples), coverage=coverage,
                ingress_drops=dict(drops), unclassified_ingress_drops=dict(unknown_drops),
                outcomes=dict(Counter(s['bot_outcome'] for s in samples)),
                timeouts=sum(a['error'] == 'TimeoutError' for s in samples for a in s['send_attempts']),
                unconfirmed_sends=sum(a['delivery_uncertain'] for s in samples for a in s['send_attempts']),
                summary=summary, samples=samples)


def qq_latency_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is not None:
            return parsed.timestamp()
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('请提供带时区偏移的 ISO 时间，例如 2026-11-20T22:15:50+08:00')


def count_qq(lines, counters):
    """QQ 诊断日志：回复端到端、发送 API、命令处理、心跳缺口。"""
    starts = counters.setdefault('qq_starts', {})
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        event = row.get('event')
        stamp = row.get('time') or 0
        if event in ('message_received', 'command_start'):
            starts[str(row.get('trace'))] = stamp
        elif event == 'reply_complete':
            start = starts.pop(str(row.get('trace')), None)
            if start:
                latency = (stamp - start) * 1000
                counters['qq_replies'] += 1
                counters['qq_reply_ms'].append(round(latency))
                if latency >= QQ_SLOW_REPLY_MS:
                    counters['qq_slow_replies'] += 1
        elif event == 'command_complete':
            ms = row.get('elapsed_ms')
            if isinstance(ms, (int, float)):
                counters['qq_commands'] += 1
                counters['qq_cmd_ms'].append(round(ms))
                if ms >= QQ_SLOW_CMD_MS:
                    counters['qq_slow_cmds'] += 1
        elif event == 'api_response':
            ms = row.get('elapsed_ms')
            if isinstance(ms, (int, float)):
                counters['qq_sends'] += 1
                counters['qq_send_ms'].append(round(ms))
                if ms >= QQ_SLOW_SEND_MS:
                    counters['qq_send_slow'] += 1
        elif event == 'api_error':
            counters['qq_send_errors'] += 1
            if row.get('delivery_uncertain'):
                counters['qq_send_uncertain'] += 1
        elif event == 'heartbeat':
            previous = counters.get('qq_last_beat') or 0
            if previous and stamp - previous > 90:
                counters['qq_gaps'] += 1
                counters['qq_gap_max_ms'] = max(counters['qq_gap_max_ms'], int((stamp - previous) * 1000))
            counters['qq_last_beat'] = stamp
    for name in ('qq_reply_ms', 'qq_send_ms', 'qq_cmd_ms'):
        if len(counters[name]) > 3000:
            counters[name] = counters[name][-3000:]
    latest = counters.get('qq_last_beat') or time.time()
    counters['qq_starts'] = {key: value for key, value in starts.items() if latest - value <= 3600}


def read_pressure(path):
    info = {}
    try:
        for line in Path(path).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ('some', 'full'):
                for field in parts[1:]:
                    if field.startswith('avg10='):
                        info[parts[0]] = float(field.split('=', 1)[1])
    except (OSError, ValueError, IndexError):
        pass
    return info


def qq_resources():
    info = dict(qq_cg_mb=None, qq_cg_peak_mb=None, qq_cg_high=0, qq_rss_mb=None,
                psi_io_some=None, psi_io_full=None, psi_mem_some=None)
    io = read_pressure('/proc/pressure/io')
    mem = read_pressure('/proc/pressure/memory')
    info['psi_io_some'] = io.get('some')
    info['psi_io_full'] = io.get('full')
    info['psi_mem_some'] = mem.get('some')
    try:
        info['qq_cg_mb'] = int((QQ_CGROUP / 'memory.current').read_text()) // 1048576
    except (OSError, ValueError):
        pass
    try:
        info['qq_cg_peak_mb'] = int((QQ_CGROUP / 'memory.peak').read_text()) // 1048576
    except (OSError, ValueError):
        pass
    try:
        for line in (QQ_CGROUP / 'memory.events').read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == 'high':
                info['qq_cg_high'] = int(parts[1])
    except (OSError, ValueError):
        pass
    try:
        pid = subprocess.run(['systemctl', 'show', 'ygo-qq', '-p', 'MainPID'],
                             capture_output=True, text=True, timeout=5).stdout.strip().split('=')[1]
        if pid:
            for line in Path('/proc/%s/status' % pid).read_text().splitlines():
                if line.startswith('VmRSS:'):
                    info['qq_rss_mb'] = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return info


def sample(state):
    counters = state['counters']
    deltas = {key: 0 for key in ('card_requests', 'card_fail', 'build_requests', 'build_fail',
                                 'computes', 'computes_fail', 'pre_ok', 'pre_skip', 'pre_error',
                                 'high_total', 'high_resets',
                                 'qq_replies', 'qq_slow_replies', 'qq_sends', 'qq_send_slow',
                                 'qq_send_errors', 'qq_send_uncertain', 'qq_commands',
                                 'qq_slow_cmds', 'qq_gaps')}
    before = {key: counters.get(key, 0) for key in deltas}
    count_access(file_reader(ACCESS, state, 'access'), counters)
    count_timings(file_reader(TIMINGS, state, 'timings'), counters)
    count_precompute(file_reader(PRECOMPUTE, state, 'precompute'), state['started_at'], counters)
    count_qq(file_reader(QQ, state, 'qq'), counters)
    memory_events(counters)
    memory = memory_now()
    qq = qq_resources()
    counters['samples'] = counters.get('samples', 0) + 1
    for key in deltas:
        deltas[key] = counters.get(key, 0) - before.get(key, 0)
    counters['min_available'] = min(counters.get('min_available', 10 ** 9), memory['available'] or 10 ** 9)
    counters['max_swap'] = max(counters.get('max_swap', 0), memory['swap_used'] or 0)
    counters['max_rss'] = max(counters.get('max_rss', 0), memory['rss'] or 0)
    counters['max_psi_io_full'] = max(counters.get('max_psi_io_full', 0), qq['psi_io_full'] or 0)
    counters['max_psi_mem_some'] = max(counters.get('max_psi_mem_some', 0), qq['psi_mem_some'] or 0)
    counters['max_qq_cg'] = max(counters.get('max_qq_cg', 0), qq['qq_cg_mb'] or 0)
    counters['max_qq_rss'] = max(counters.get('max_qq_rss', 0), qq['qq_rss_mb'] or 0)
    record = dict(at=round(now(), 1), window=state.get('window', 1),
                  mem_available=memory['available'], swap_used=memory['swap_used'],
                  rss=memory['rss'], **qq, **deltas)
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    with SAMPLES.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    save_state(state)
    return record


def summarize(state):
    counters = state['counters']
    card = counters.get('card_requests', 0)
    build = counters.get('build_requests', 0)
    rounds = counters.get('pre_ok', 0) + counters.get('pre_skip', 0) + counters.get('pre_error', 0)
    lines = []
    hours = (now() - state['started_at']) / 3600
    window = state.get('window', 1)
    lines.append('# 卡片统计预计算观察报告（第 %d 个 24h 窗口）' % window)
    lines.append('')
    lines.append('- 观察窗口：%s → %s（%.1f 小时，%d 次采样）' % (
        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state['started_at'])),
        time.strftime('%Y-%m-%d %H:%M:%S'), hours, counters.get('samples', 0)))
    lines.append('- 预计算轮次：成功 %d、跳过 %d、错误 %d（跳过率 %.0f%%）' % (
        counters.get('pre_ok', 0), counters.get('pre_skip', 0), counters.get('pre_error', 0),
        100.0 * counters.get('pre_skip', 0) / rounds if rounds else 0))
    if counters.get('skip_reason'):
        lines.append('- 跳过原因：%s' % json.dumps(counters['skip_reason'], ensure_ascii=False))
    lines.append('- 卡片统计请求：%d 次，失败（503 等）%d 次，失败率 %.0f%%' % (
        card, counters.get('card_fail', 0), 100.0 * counters.get('card_fail', 0) / card if card else 0))
    lines.append('- 构筑请求：%d 次，失败 %d 次，失败率 %.0f%%' % (
        build, counters.get('build_fail', 0), 100.0 * counters.get('build_fail', 0) / build if build else 0))
    lines.append('- 卡片统计实时计算：%d 次（失败 %d）→ 预计算未命中的次数' % (
        counters.get('computes', 0), counters.get('computes_fail', 0)))
    lines.append('- `memory.events high` 增量：%d（web 重启重置 %d 次）' % (
        counters.get('high_total', 0), counters.get('high_resets', 0)))
    lines.append('- MemAvailable 最低：%s MB；Swap 最高：%s MB；web RSS 最高：%s MB' % (
        counters.get('min_available') if counters.get('min_available') != 10 ** 9 else '-',
        counters.get('max_swap', 0), counters.get('max_rss', 0)))
    reply_p50 = _percentile(counters.get('qq_reply_ms') or [], 0.5)
    reply_p95 = _percentile(counters.get('qq_reply_ms') or [], 0.95)
    reply_max = max(counters.get('qq_reply_ms') or [0])
    send_p50 = _percentile(counters.get('qq_send_ms') or [], 0.5)
    send_p95 = _percentile(counters.get('qq_send_ms') or [], 0.95)
    send_max = max(counters.get('qq_send_ms') or [0])
    cmd_p50 = _percentile(counters.get('qq_cmd_ms') or [], 0.5)
    cmd_p95 = _percentile(counters.get('qq_cmd_ms') or [], 0.95)
    cmd_max = max(counters.get('qq_cmd_ms') or [0])
    lines.append('')
    lines.append('## QQ 机器人（反应时长/卡顿）')
    lines.append('')
    lines.append('- 回复端到端（消息→回复完成）：%d 条，p50 %s ms，p95 %s ms，max %s ms，≥8 s %d 条' % (
        counters.get('qq_replies', 0), reply_p50, reply_p95, reply_max, counters.get('qq_slow_replies', 0)))
    lines.append('- 发送 API（NapCat）：%d 次，p50 %s ms，p95 %s ms，max %s ms，≥3 s %d 次' % (
        counters.get('qq_sends', 0), send_p50, send_p95, send_max, counters.get('qq_send_slow', 0)))
    lines.append('- 命令处理：%d 次，p50 %s ms，p95 %s ms，max %s ms，≥8 s %d 次' % (
        counters.get('qq_commands', 0), cmd_p50, cmd_p95, cmd_max, counters.get('qq_slow_cmds', 0)))
    lines.append('- 发送错误/超时：%d 次（delivery_uncertain %d）' % (
        counters.get('qq_send_errors', 0), counters.get('qq_send_uncertain', 0)))
    lines.append('- 心跳缺口 >90 s：%d 次，最大缺口 %.0f s' % (
        counters.get('qq_gaps', 0), counters.get('qq_gap_max_ms', 0) / 1000))
    lines.append('- 资源：PSI io full 最高 %s%%，mem some 最高 %s%%，ygo-qq cgroup 峰值 %s MB，RSS 最高 %s MB' % (
        round(counters.get('max_psi_io_full', 0), 1), round(counters.get('max_psi_mem_some', 0), 1),
        counters.get('max_qq_cg', 0), counters.get('max_qq_rss', 0)))
    lines.append('')
    lines.append('## 逐小时')
    lines.append('')
    lines.append('| 小时 | card 请求 | card 失败 | build 失败 | 实时计算 | 预计算成功/跳过 | high 增量 | MemAvail |')
    lines.append('|---|---:|---:|---:|---:|---|---:|---:|')
    buckets = {}
    try:
        for raw in SAMPLES.read_text(encoding='utf-8').splitlines():
            row = json.loads(raw)
            if row.get('window', 1) != window:
                continue
            hour = time.strftime('%m-%d %H', time.localtime(row['at']))
            bucket = buckets.setdefault(hour, dict(card=0, card_fail=0, build_fail=0, computes=0,
                                                   pre_ok=0, pre_skip=0, high=0, mem=10 ** 9))
            bucket['card'] += row.get('card_requests', 0)
            bucket['card_fail'] += row.get('card_fail', 0)
            bucket['build_fail'] += row.get('build_fail', 0)
            bucket['computes'] += row.get('computes', 0)
            bucket['pre_ok'] += row.get('pre_ok', 0)
            bucket['pre_skip'] += row.get('pre_skip', 0)
            bucket['high'] += row.get('high_total', 0)
            if row.get('mem_available'):
                bucket['mem'] = min(bucket['mem'], row['mem_available'])
    except (OSError, ValueError):
        pass
    for hour in sorted(buckets):
        item = buckets[hour]
        lines.append('| %s | %d | %d | %d | %d | %d/%d | %d | %s |' % (
            hour, item['card'], item['card_fail'], item['build_fail'], item['computes'],
            item['pre_ok'], item['pre_skip'], item['high'],
            item['mem'] if item['mem'] != 10 ** 9 else '-'))
    return '\n'.join(lines) + '\n'


def cumulative_section(state):
    """所有窗口的累计表（含当前窗口已采到的部分）。"""
    windows = {}
    try:
        for raw in SAMPLES.read_text(encoding='utf-8').splitlines():
            row = json.loads(raw)
            window = int(row.get('window', 1))
            bucket = windows.setdefault(window, dict(
                first=row['at'], last=row['at'], card=0, card_fail=0, build_fail=0,
                pre_ok=0, pre_skip=0, pre_error=0, high=0, resets=0,
                qq_replies=0, qq_slow=0, qq_sends=0, qq_send_slow=0, qq_uncertain=0, qq_gaps=0,
                mem=10 ** 9, swap=0, psi_io=0, qq_cg=0))
            bucket['first'] = min(bucket['first'], row['at'])
            bucket['last'] = max(bucket['last'], row['at'])
            bucket['card'] += row.get('card_requests', 0)
            bucket['card_fail'] += row.get('card_fail', 0)
            bucket['build_fail'] += row.get('build_fail', 0)
            bucket['pre_ok'] += row.get('pre_ok', 0)
            bucket['pre_skip'] += row.get('pre_skip', 0)
            bucket['pre_error'] += row.get('pre_error', 0)
            bucket['high'] += row.get('high_total', 0)
            bucket['resets'] += row.get('high_resets', 0)
            bucket['qq_replies'] += row.get('qq_replies', 0)
            bucket['qq_slow'] += row.get('qq_slow_replies', 0)
            bucket['qq_sends'] += row.get('qq_sends', 0)
            bucket['qq_send_slow'] += row.get('qq_send_slow', 0)
            bucket['qq_uncertain'] += row.get('qq_send_uncertain', 0)
            bucket['qq_gaps'] += row.get('qq_gaps', 0)
            if row.get('mem_available'):
                bucket['mem'] = min(bucket['mem'], row['mem_available'])
            if row.get('swap_used'):
                bucket['swap'] = max(bucket['swap'], row['swap_used'])
            if row.get('psi_io_full'):
                bucket['psi_io'] = max(bucket['psi_io'], row['psi_io_full'])
            if row.get('qq_cg_mb'):
                bucket['qq_cg'] = max(bucket['qq_cg'], row['qq_cg_mb'])
    except (OSError, ValueError):
        pass
    lines = ['## 累计（所有窗口）', '',
             '| 窗口 | 起止 | card 请求/失败 | build 失败 | 预计算 成功/跳过 | QQ 回复(≥8s) | QQ 发送(≥3s/超时) | 心跳缺口 | high | MemAvail 低 | Swap 高 |',
             '|---|---|---|---:|---|---|---|---:|---:|---:|---:|']
    for window in sorted(windows):
        item = windows[window]
        lines.append('| %d | %s → %s | %d/%d | %d | %d/%d | %d(%d) | %d(%d/%d) | %d | %d | %s | %d |' % (
            window,
            time.strftime('%m-%d %H:%M', time.localtime(item['first'])),
            time.strftime('%m-%d %H:%M', time.localtime(item['last'])),
            item['card'], item['card_fail'], item['build_fail'],
            item['pre_ok'], item['pre_skip'],
            item['qq_replies'], item['qq_slow'],
            item['qq_sends'], item['qq_send_slow'], item['qq_uncertain'],
            item['qq_gaps'], item['high'],
            item['mem'] if item['mem'] != 10 ** 9 else '-', item['swap']))
    if len(windows) > 1:
        total = dict(card=sum(w['card'] for w in windows.values()),
                     card_fail=sum(w['card_fail'] for w in windows.values()),
                     build_fail=sum(w['build_fail'] for w in windows.values()),
                     pre_ok=sum(w['pre_ok'] for w in windows.values()),
                     pre_skip=sum(w['pre_skip'] for w in windows.values()),
                     qq_replies=sum(w['qq_replies'] for w in windows.values()),
                     qq_slow=sum(w['qq_slow'] for w in windows.values()),
                     qq_sends=sum(w['qq_sends'] for w in windows.values()),
                     qq_send_slow=sum(w['qq_send_slow'] for w in windows.values()),
                     qq_uncertain=sum(w['qq_uncertain'] for w in windows.values()),
                     qq_gaps=sum(w['qq_gaps'] for w in windows.values()),
                     high=sum(w['high'] for w in windows.values()))
        lines.append('| 合计 | — | %d/%d | %d | %d/%d | %d(%d) | %d(%d/%d) | %d | %d | — | — |' % (
            total['card'], total['card_fail'], total['build_fail'], total['pre_ok'], total['pre_skip'],
            total['qq_replies'], total['qq_slow'], total['qq_sends'], total['qq_send_slow'],
            total['qq_uncertain'], total['qq_gaps'], total['high']))
    return '\n'.join(lines) + '\n'


def write_report(state):
    report = summarize(state) + '\n' + cumulative_section(state)
    try:
        from web_latency_report import build_report
        report += '\n' + build_report([DATA / 'logs/web-access.jsonl.1', DATA / 'logs/web-access.jsonl'], [SAMPLES], since=state['started_at'])
    except Exception:
        report += '\n网页耗时诊断生成失败；原观察报告保留，请核对诊断脚本。\n'
    stamp = time.strftime('%Y%m%d-%H%M', time.localtime())
    path = WATCH_DIR / ('card-precompute-watch-report-w%d-%s.md' % (state.get('window', 1), stamp))
    path.write_text(report, encoding='utf-8')
    REPORT.write_text(report, encoding='utf-8')
    CUMULATIVE.write_text(cumulative_section(state), encoding='utf-8')
    return path, report


def reset_window(state, args):
    counters = state['counters']
    totals = dict(counters.get('qq_totals') or {})
    for source, target in (('qq_replies', 'replies'), ('qq_slow_replies', 'slow_replies'),
                           ('qq_sends', 'sends'), ('qq_send_slow', 'send_slow'),
                           ('qq_send_errors', 'send_errors'), ('qq_send_uncertain', 'uncertain'),
                           ('qq_commands', 'commands'), ('qq_slow_cmds', 'slow_cmds'),
                           ('qq_gaps', 'gaps')):
        totals[target] = totals.get(target, 0) + counters.get(source, 0)
    totals['gap_max_ms'] = max(totals.get('gap_max_ms', 0), counters.get('qq_gap_max_ms', 0))
    state['windows_done'] = state.get('windows_done', 0) + 1
    state['window'] = state.get('window', 1) + 1
    state['started_at'] = now()
    state['deadline'] = now() + args.hours * 3600
    state['counters'] = blank_counters()
    state['counters']['qq_totals'] = totals
    state['offsets'] = {'access': current_offset(ACCESS),
                        'timings': current_offset(TIMINGS),
                        'precompute': current_offset(PRECOMPUTE),
                        'qq': current_offset(QQ)}
    state['done'] = False


def main():
    global WATCH_DIR, STATE, SAMPLES, REPORT, CUMULATIVE, ACCESS, TIMINGS, PRECOMPUTE, QQ, DATA
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=str(DATA))
    parser.add_argument('--hours', type=float, default=24.0)
    parser.add_argument('--interval', type=int, default=300)
    parser.add_argument('--windows', type=int, default=1,
                        help='连续观察多少个 24h 窗口；0 = 一直继续')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--status', action='store_true')
    mode.add_argument('--qq-latency-since', type=qq_latency_time,
                      help='只读回放 QQ 延迟，起始 ISO 时间（含时区偏移）')
    parser.add_argument('--qq-latency-until', type=qq_latency_time, help='截止 ISO 时间，默认当前时间')
    parser.add_argument('--qq-operation', default='roll_dice', help='回放的命令标识，all 汇总全部（含未记命令名），默认 roll_dice')
    args = parser.parse_args()
    if args.qq_latency_until is not None and args.qq_latency_since is None:
        parser.error('--qq-latency-until 需要与 --qq-latency-since 一起使用')

    DATA = Path(args.data_dir)
    WATCH_DIR = DATA / 'observation'
    STATE = WATCH_DIR / 'card-precompute-watch.json'
    SAMPLES = WATCH_DIR / 'card-precompute-watch.jsonl'
    REPORT = WATCH_DIR / 'card-precompute-watch-report.md'
    CUMULATIVE = WATCH_DIR / 'card-precompute-watch-cumulative.md'
    ACCESS = DATA / 'logs' / 'web-access.jsonl'
    TIMINGS = DATA / 'logs' / 'statistics-query-timings.jsonl'
    PRECOMPUTE = DATA / 'logs' / 'card-report-precompute.log'
    QQ = DATA / 'logs' / 'qq-diagnostics.jsonl'

    if args.qq_latency_since is not None:
        until = args.qq_latency_until if args.qq_latency_until is not None else now()
        if until <= args.qq_latency_since:
            parser.error('QQ 延迟回放的截止时间必须晚于起始时间')
        try:
            result = qq_latency_report(qq_latency_rows(QQ), args.qq_latency_since, until, args.qq_operation)
        except (OSError, EOFError) as exc:
            parser.exit(1, f'读取 QQ 诊断日志失败：{exc}\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.status:
        state = load_state()
        if not state:
            print('尚未开始观察')
            return 0
        print(summarize(state))
        print(cumulative_section(state))
        return 0

    state = load_state()
    if state is None:
        state = dict(started_at=now(), deadline=now() + args.hours * 3600,
                     offsets={'access': current_offset(ACCESS),
                              'timings': current_offset(TIMINGS),
                              'precompute': current_offset(PRECOMPUTE),
                              'qq': current_offset(QQ)},
                     counters=blank_counters(), done=False, window=1, windows_done=0)
        save_state(state)
    for key, value in blank_counters().items():
        state.setdefault('counters', {}).setdefault(key, value)
    # 老状态升级：QQ 日志从当前末尾开始，避免把窗口之前的事件算进来
    if 'qq' not in state.setdefault('offsets', {}):
        state['offsets']['qq'] = current_offset(QQ)
    if state.get('done'):
        print('观察已完成，报告：%s' % REPORT)
        return 0

    if args.once:
        record = sample(state)
        print(json.dumps(record, ensure_ascii=False))
        return 0

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    while not _STOP:
        if now() >= state['deadline']:
            sample(state)
            path, report = write_report(state)
            print('window %d done -> %s' % (state.get('window', 1), path))
            completed = state.get('windows_done', 0) + 1
            if args.windows == 0 or completed < args.windows:
                reset_window(state, args)
                save_state(state)
                continue
            state['done'] = True
            state['finished_at'] = now()
            save_state(state)
            print(report)
            break
        sample(state)
        slept = 0
        while slept < args.interval and not _STOP:
            time.sleep(min(10, args.interval - slept))
            slept += 10
    save_state(state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
