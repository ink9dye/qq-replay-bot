"""进场节流：同一时刻限制并发 join、控制间隔、连续失败后暂停、小时配额。

设计依据（2026-10-30/31 实测）：启动瞬间 30 秒内连开 14 个中途局后，观战被平台限制约 17 分钟
（00:19:52 自行恢复）。所以真正要防的是"启动爆发"，不是"进不去"。
本模块只做决策，不碰网络；调用方负责真正发起/结束连接。
"""
from __future__ import annotations

import random
import time

DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MIN_INTERVAL = 5.0
DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_PAUSE = 60.0
DEFAULT_JITTER = 0.0
DEFAULT_MAX_PER_HOUR = 0          # 0 = 不限制
FAILED_OUTCOMES = ('gone', 'retry', 'config', 'unstable')


class JoinGate:
    def __init__(self, max_concurrent=DEFAULT_MAX_CONCURRENT, min_interval=DEFAULT_MIN_INTERVAL,
                 fail_threshold=DEFAULT_FAIL_THRESHOLD, pause_seconds=DEFAULT_PAUSE,
                 jitter=DEFAULT_JITTER, max_per_hour=DEFAULT_MAX_PER_HOUR, hour_seconds=3600.0):
        for name, value in (('max_concurrent', max_concurrent), ('fail_threshold', fail_threshold)):
            if not isinstance(value, int) or value < 1:
                raise ValueError('invalid_' + name)
        for name, value in (('min_interval', min_interval), ('pause_seconds', pause_seconds),
                            ('jitter', jitter), ('hour_seconds', hour_seconds)):
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError('invalid_' + name)
        if not isinstance(max_per_hour, int) or max_per_hour < 0:
            raise ValueError('invalid_max_per_hour')
        self.max_concurrent = max_concurrent
        self.min_interval = float(min_interval)
        self.fail_threshold = fail_threshold
        self.pause_seconds = float(pause_seconds)
        self.jitter = float(jitter)
        self.max_per_hour = int(max_per_hour)
        self.hour_seconds = float(hour_seconds)
        self.in_flight = 0
        self.last_started_at = None
        self.next_allowed_at = 0.0
        self.starts = []          # 最近一小时的实际进场时刻（monotonic）
        self.probation_until = 0.0
        self.probation_cap = 0
        self.probation_interval = 0.0
        self.block_reason = ''
        self.failures = 0
        self.paused_until = 0.0
        self.stats = dict(started=0, succeeded=0, failed=0, pauses=0)

    def start_probation(self, now, seconds, cap, interval):
        """观察期：在 now+seconds 前把并发压到 cap、间隔至少 interval（新 IP 前 10 分钟）。"""
        self.probation_until = max(self.probation_until, float(now) + float(seconds))
        self.probation_cap = int(cap)
        self.probation_interval = float(interval)

    def probation_left(self, now):
        return round(max(0.0, self.probation_until - now), 1)

    def _cap(self, now):
        if self.probation_cap and now < self.probation_until:
            return min(self.max_concurrent, self.probation_cap)
        return self.max_concurrent

    def _interval(self, now):
        if self.probation_interval and now < self.probation_until:
            return max(self.min_interval, self.probation_interval)
        return self.min_interval

    def _prune_starts(self, now):
        if self.max_per_hour <= 0:
            return
        cutoff = now - self.hour_seconds
        if self.starts and self.starts[0] < cutoff:
            self.starts = [item for item in self.starts if item >= cutoff]

    def allow(self, now):
        """(能否开始, 还要等多少秒)。等待值只用于日志，调用方自己决定要不要等。"""
        self.block_reason = ''
        if now < self.paused_until:
            self.block_reason = 'paused'
            return False, round(self.paused_until - now, 3)
        if self.in_flight >= self._cap(now):
            self.block_reason = 'concurrent'
            return False, None
        self._prune_starts(now)
        if self.max_per_hour > 0 and len(self.starts) >= self.max_per_hour:
            self.block_reason = 'quota'
            return False, round(self.starts[0] + self.hour_seconds - now, 3)
        if self.last_started_at is not None:
            wait = self.next_allowed_at - now
            if wait > 0:
                self.block_reason = 'interval'
                return False, round(wait, 3)
        return True, 0.0

    def started(self, now):
        if not self.allow(now)[0]:
            raise ValueError('gate_not_open')
        self.in_flight += 1
        self.last_started_at = now
        span = self._interval(now)
        if self.jitter > 0 and span > 0:
            span += random.uniform(0.0, self.jitter)
        self.next_allowed_at = now + span
        if self.max_per_hour > 0:
            self.starts.append(now)
            self._prune_starts(now)
        self.stats['started'] += 1

    def finished(self, now, outcome, startup=False):
        """结束一次连接。gone/retry 这类失败累计到阈值就暂停；saved 等成功会清零。

        startup=True：这是"程序启动前就在打"的中途局，进不去不罚（不累计失败、不触发暂停），
        只释放 in_flight；否则重启后追这些旧局会把节流一路推到 300 秒。
        """
        self.in_flight = max(0, self.in_flight - 1)
        if startup:
            self.stats['startup_skipped'] = self.stats.get('startup_skipped', 0) + 1
            return
        if outcome in FAILED_OUTCOMES:
            self.failures += 1
            self.stats['failed'] += 1
            if self.failures >= self.fail_threshold:
                self.paused_until = now + self.pause_seconds
                self.failures = 0
                self.stats['pauses'] += 1
        else:
            self.failures = 0
            self.stats['succeeded'] += 1

    def state(self):
        return dict(in_flight=self.in_flight, failures=self.failures,
                    paused_until=round(self.paused_until, 3),
                    max_concurrent=self.max_concurrent, min_interval=self.min_interval,
                    jitter=self.jitter, max_per_hour=self.max_per_hour,
                    probation_left=self.probation_left(time.monotonic()) if self.probation_until else 0.0,
                    block_reason=self.block_reason,
                    **self.stats)
