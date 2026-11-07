# -*- coding: utf-8 -*-
"""Side 批次：并发安全写、超时语义、内存门槛、锁与空闲门。"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import side_cache, side_inference, side_runner


class SideCacheCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'cache').mkdir()

    def test_commit_inserts_new_and_keeps_existing(self):
        side_cache.save(self.root, {'a': {'cards': [1]}})
        result = side_cache.commit(self.root, {'a': {'cards': [99]}, 'b': {'cards': [2]}})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['inserted'], 1)
        data = side_cache.load(self.root)
        self.assertEqual(data['a'], {'cards': [1]})   # 已存在的不被覆盖
        self.assertEqual(data['b'], {'cards': [2]})
        self.assertFalse((self.root / 'cache' / 'side-inference.tmp').exists())

    def test_commit_nochange_when_everything_exists(self):
        side_cache.save(self.root, {'a': {'cards': [1]}})
        result = side_cache.commit(self.root, {'a': {'cards': [2]}})
        self.assertEqual(result['status'], 'nochange')
        self.assertEqual(result['inserted'], 0)

    def test_commit_merges_when_file_changed_between_load_and_write(self):
        side_cache.save(self.root, {'a': {'cards': [1]}})
        original_stamp = side_cache.stamp(self.root)
        calls = {'n': 0}
        real_write = side_cache._write_atomic

        def write_then_change(path, payload):
            tmp = real_write(path, payload)
            if calls['n'] == 0:
                # 模拟“读之后、替换之前”另一个写入者追加了 'c'
                calls['n'] += 1
                side_cache.save(self.root, {'a': {'cards': [1]}, 'c': {'cards': [3]}})
            return tmp

        with patch('bot.side_cache._write_atomic', side_effect=write_then_change):
            result = side_cache.commit(self.root, {'b': {'cards': [2]}})
        self.assertEqual(result['status'], 'ok')
        data = side_cache.load(self.root)
        self.assertEqual(sorted(data), ['a', 'b', 'c'])
        self.assertNotEqual(original_stamp, side_cache.stamp(self.root))
        self.assertEqual(calls['n'], 1)

    def test_commit_reports_conflict_without_losing_data(self):
        side_cache.save(self.root, {'a': {'cards': [1]}})
        with patch('bot.side_cache.stamp', side_effect=[(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6)]):
            result = side_cache.commit(self.root, {'b': {'cards': [2]}}, attempts=2)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(sorted(side_cache.load(self.root)), ['a'])

    def test_commit_conflicts_without_reloading_while_holding_old_snapshot(self):
        side_cache.save(self.root, {'a': {'cards': [1]}})
        stale = side_cache.stamp(self.root)
        side_cache.save(self.root, {'a': {'cards': [1]}, 'b': {'cards': [2]}})   # 别的写入者
        loads = []
        with patch('bot.side_cache.load', side_effect=lambda d: loads.append(d) or {}):
            result = side_cache.commit(self.root, {'c': {'cards': [3]}},
                                       base={'a': {'cards': [1]}}, base_stamp=stale)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['reason'], 'stamp_changed')
        self.assertEqual(loads, [])                     # 未在持有旧快照时重新解析整份文件
        self.assertEqual(sorted(side_cache.load(self.root)), ['a', 'b'])   # 别人的写入保留

    def test_save_is_atomic_and_corrupt_reads_empty(self):
        side_cache.save(self.root, {'a': {'cards': [1]}})
        self.assertFalse((self.root / 'cache' / 'side-inference.tmp').exists())
        (self.root / 'cache' / 'side-inference.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(side_cache.load(self.root), {})


class SideRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'data' / 'cache').mkdir(parents=True)
        (self.root / 'config.json').write_text(json.dumps(dict(data_dir=str(self.root / 'data'),
                                                               replay_dir=str(self.root))),
                                               encoding='utf-8')
        sqlite3.connect(str(self.root / 'data' / 'bot.sqlite3')).close()
        side_cache.save(self.root / 'data', {'cached-build': {'cards': [9]}})

    def test_loads_cache_once_and_commits_once(self):
        views = {'p1': dict(build='new-1', digest='d1'), 'p2': dict(build='cached-build', digest='d2'),
                 'p3': dict(build='new-2', digest='d3'), 'p4': dict(build='new-3', digest='d4')}
        loads, commits = [], []
        real = side_cache.commit

        def commit(data_dir, added, base=None, base_stamp=None, attempts=3):
            commits.append(dict(added))
            return real(data_dir, added, base=base, base_stamp=base_stamp, attempts=attempts)

        def query(db, replays, now, player, digest):
            return dict(inferred_side=dict(cards=[player]))

        with patch('bot.statistics_deck_naming._latest_views', return_value=views), \
                patch('bot.statistics_build_detail.query', side_effect=query), \
                patch('bot.side_cache.load', side_effect=lambda d: loads.append(d) or {'cached-build': {'cards': [9]}}), \
                patch('bot.side_cache.commit', side_effect=commit):
            written = side_inference.run(self.root, limit=2)
        self.assertEqual(written, 2)
        # run() 读一次；commit() 复用传入的 base 快照，不再重复解析（所以仍是 1 次）。
        self.assertEqual(len(loads), 1)
        self.assertEqual(len(commits), 1)
        self.assertEqual(sorted(commits[0]), ['new-1', 'new-2'])

    def test_conflict_retries_once_without_old_snapshot(self):
        views = {'p1': dict(build='new-1', digest='d1')}
        calls = []

        def commit(data_dir, added, base=None, base_stamp=None, attempts=3):
            calls.append(dict(keys=sorted(added), has_base=base is not None))
            if len(calls) == 1:
                return dict(status='conflict', inserted=0, entries=None, reason='stamp_changed')
            return dict(status='ok', inserted=len(added), entries=2)

        def query(db, replays, now, player, digest):
            return dict(inferred_side=dict(cards=[1]))

        with patch('bot.statistics_deck_naming._latest_views', return_value=views), \
                patch('bot.statistics_build_detail.query', side_effect=query), \
                patch('bot.side_cache.commit', side_effect=commit):
            written = side_inference.run(self.root, limit=1)
        self.assertEqual(written, 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]['has_base'])
        self.assertFalse(calls[1]['has_base'])      # 第二次重试时已释放旧快照

    def test_deadline_stops_and_marks_partial(self):
        views = {'p%d' % i: dict(build='b%d' % i, digest='d%d' % i) for i in range(5)}
        stats = {}

        def query(db, replays, now, player, digest):
            return dict(inferred_side=dict(cards=[1]))

        calls = {'n': 0}

        def deadlined(*a, **k):
            calls['n'] += 1
            return False if calls['n'] > 1 else False

        # deadline 立刻到期：第一条前就应停止
        with patch('bot.statistics_deck_naming._latest_views', return_value=views), \
                patch('bot.statistics_build_detail.query', side_effect=query):
            written = side_inference.run(self.root, limit=5,
                                         deadline=__import__('time').monotonic() - 1,
                                         stats=stats)
        self.assertEqual(written, 0)
        self.assertTrue(stats['partial'])
        self.assertEqual(stats['commit_status'], 'nochange')
        self.assertEqual(deadlined(), False)

    def test_state_file_records_commit(self):
        state = self.root / 'state.json'
        views = {'p1': dict(build='new-1', digest='d1')}
        with patch('bot.statistics_deck_naming._latest_views', return_value=views), \
                patch('bot.statistics_build_detail.query',
                      return_value=dict(inferred_side=dict(cards=[1]))):
            side_inference.run(self.root, limit=1, state_path=state)
        payload = json.loads(state.read_text(encoding='utf-8'))
        self.assertEqual(payload['written'], 1)
        self.assertEqual(payload['commit_status'], 'ok')
        self.assertFalse(payload['partial'])
        self.assertEqual(payload['entries'], 2)
        self.assertFalse(state.with_suffix('.tmp').exists())


class SideRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        self.lock = None

    def tearDown(self):
        side_runner.release(self.lock)

    def write_status(self, active=0, age=0.0):
        (self.root / 'data' / 'capture-status.json').write_text(
            json.dumps(dict(active_observations=active, updated_at=__import__('time').time() - age)),
            encoding='utf-8')

    def write_meminfo(self, available_mb):
        path = self.root / 'meminfo'
        path.write_text('MemTotal: 1740000 kB\nMemAvailable: %d kB\n' % (available_mb * 1024),
                        encoding='utf-8')
        return path

    def test_lock_is_exclusive(self):
        self.lock, holder = side_runner.acquire(self.root)
        self.assertIsNotNone(self.lock)
        second, holder = side_runner.acquire(self.root)
        self.assertIsNone(second)
        self.assertEqual(holder['pid'], os.getpid())

    def test_stale_and_dead_locks_are_taken_over(self):
        path = side_runner._lock_path(self.root)
        path.write_text(json.dumps(dict(pid=999999, started_at=0)), encoding='utf-8')
        lock, holder = side_runner.acquire(self.root)
        self.assertIsNotNone(lock)
        self.lock = lock

    def test_lock_with_live_pid_but_expired_age_is_taken_over(self):
        path = side_runner._lock_path(self.root)
        path.write_text(json.dumps(dict(pid=os.getpid(), started_at=0)), encoding='utf-8')
        lock, holder = side_runner.acquire(self.root)
        self.assertIsNotNone(lock)
        self.lock = lock

    def test_idle_gate(self):
        self.write_status(active=2)
        self.assertEqual(side_runner.idle_ok(self.root), (False, 'active_observations'))
        self.write_status(active=0, age=999)
        self.assertEqual(side_runner.idle_ok(self.root), (False, 'capture_status_stale'))
        self.write_status(active=0, age=1)
        self.assertEqual(side_runner.idle_ok(self.root), (True, 'idle'))

    def test_memory_gate_skips_before_starting(self):
        state = self.root / 'state.json'
        called = []
        with patch('bot.side_inference.run', side_effect=lambda *a, **k: called.append(1)):
            code = side_runner.main(['--root', str(self.root), '--state', str(state),
                                     '--min-available-mb', '900'],
                                    meminfo_path=self.write_meminfo(700))
        self.assertEqual(code, 0)
        self.assertEqual(called, [])
        payload = json.loads(state.read_text(encoding='utf-8'))
        self.assertEqual(payload['result'], 'skipped')
        self.assertEqual(payload['reason'], 'memory_low')
        self.assertEqual(payload['gap_mb'], 200)

    def test_memory_gate_allows_enough_memory(self):
        state = self.root / 'state.json'

        def fake_run(root, limit=20, deadline=None, stats=None):
            stats.update(dict(partial=False, commit_status='ok', written=2))
            return 2

        with patch('bot.side_inference.run', side_effect=fake_run):
            code = side_runner.main(['--root', str(self.root), '--state', str(state),
                                     '--min-available-mb', '900'],
                                    meminfo_path=self.write_meminfo(1200))
        self.assertEqual(code, 0)
        payload = json.loads(state.read_text(encoding='utf-8'))
        self.assertEqual(payload['result'], 'ok')
        self.assertEqual(payload['written'], 2)

    def test_unreadable_meminfo_skips(self):
        state = self.root / 'state.json'
        code = side_runner.main(['--root', str(self.root), '--state', str(state)],
                                meminfo_path=self.root / 'missing-meminfo')
        self.assertEqual(code, 0)
        payload = json.loads(state.read_text(encoding='utf-8'))
        self.assertEqual(payload['reason'], 'meminfo_unavailable')

    def test_partial_is_not_success(self):
        state = self.root / 'state.json'

        def fake_run(root, limit=20, deadline=None, stats=None):
            stats.update(dict(partial=True, commit_status='ok', written=3))
            return 3

        with patch('bot.side_inference.run', side_effect=fake_run):
            code = side_runner.main(['--root', str(self.root), '--state', str(state),
                                     '--min-available-mb', '1'],
                                    meminfo_path=self.write_meminfo(2000))
        self.assertEqual(code, 0)
        payload = json.loads(state.read_text(encoding='utf-8'))
        self.assertEqual(payload['result'], 'partial')
        self.assertEqual(payload['reason'], 'max_seconds')

    def test_main_skips_when_busy_and_releases_lock(self):
        self.write_status(active=3)
        state = self.root / 'state.json'
        called = []
        with patch('bot.side_inference.run', side_effect=lambda *a, **k: called.append(1)):
            code = side_runner.main(['--root', str(self.root), '--state', str(state),
                                     '--idle-require', '--min-available-mb', '1'],
                                    meminfo_path=self.write_meminfo(2000))
        self.assertEqual(code, 0)
        self.assertEqual(called, [])
        self.assertFalse(side_runner._lock_path(self.root).exists())


if __name__ == '__main__':
    unittest.main()
