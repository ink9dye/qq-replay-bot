# -*- coding: utf-8 -*-
"""构筑预热：跳过新鲜结果、忙时让路、按前100轮转、调用真实 route。"""
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bot import statistics_prewarm as prewarm


class _Refresh:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.jobs = queue.Queue(maxsize=8)
        self.items = {}

    def _read(self, key):
        return None


class _Portal:
    def __init__(self, data_dir):
        self._refresh = _Refresh()
        self.database = str(data_dir / 'bot.sqlite3')
        self.routes = []

    def _session(self, ts):
        return 'S' * 43

    def clock(self):
        return 1.0

    def route(self, path, headers, host):
        self.routes.append(path)


class BuildPrewarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        self.data.mkdir()
        self.portal = _Portal(self.data)
        self._orig_top = prewarm.top_players
        prewarm.top_players = lambda portal, ttl=600: ['甲', '乙', '丙']

    def tearDown(self):
        prewarm.top_players = self._orig_top

    def meminfo(self, mb=2000):
        path = self.data / 'meminfo'
        path.write_text('MemTotal: 1 kB\nMemAvailable: %d kB\n' % (mb * 1024), encoding='utf-8')
        return path

    def test_fresh_build_is_skipped(self):
        self.portal._refresh.items[('deck-detail', '甲')] = dict(saved=time.time() - 60,
                                                                 stamp=None,
                                                                 result=(200, 'x', b'{}'), size=2)
        payload, cursor = prewarm.build_cursor_state(self.portal, (), 0,
                                                     meminfo_path=self.meminfo())
        self.assertEqual(payload['skipped'], 'build_fresh')
        self.assertEqual(cursor, 1)
        self.assertEqual(self.portal.routes, [])

    def test_stale_build_queues_route(self):
        payload, cursor = prewarm.build_cursor_state(self.portal, (), 0,
                                                     meminfo_path=self.meminfo())
        self.assertEqual(payload['step'], 'build')
        self.assertEqual(payload['player'], '甲')
        self.assertEqual(cursor, 1)
        self.assertEqual(self.portal.routes,
                         ['/statistics/api/build?player=%E7%94%B2'])

    def test_busy_refresh_skips_without_route(self):
        self.portal._refresh.pending['x'] = threading.Event()
        payload, cursor = prewarm.build_cursor_state(self.portal, (), 0,
                                                     meminfo_path=self.meminfo())
        self.assertEqual(payload['skipped'], 'refresh_busy')
        self.assertEqual(cursor, 0)
        self.assertEqual(self.portal.routes, [])

    def test_memory_low_skips(self):
        payload, cursor = prewarm.build_cursor_state(self.portal, (), 0,
                                                     meminfo_path=self.meminfo(500))
        self.assertEqual(payload['skipped'], 'memory_low')
        self.assertEqual(self.portal.routes, [])

    def test_cursor_wraps_by_player_count(self):
        _, cursor = prewarm.build_cursor_state(self.portal, (), 2, meminfo_path=self.meminfo())
        self.assertEqual(cursor, 0)

    def test_config_builds_defaults(self):
        cfg = prewarm.config_section(dict(prewarm=dict(enabled=True)))
        self.assertTrue(cfg['builds']['enabled'])
        self.assertEqual(cfg['builds']['interval'], 15)
        custom = prewarm.config_section(dict(prewarm=dict(enabled=True,
                                                          builds=dict(enabled=False, interval_seconds=30))))
        self.assertFalse(custom['builds']['enabled'])
        self.assertEqual(custom['builds']['interval'], 30)


if __name__ == '__main__':
    unittest.main()
