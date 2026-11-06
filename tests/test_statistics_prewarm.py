# -*- coding: utf-8 -*-
"""预热：真实页面参数、每轮一个 key 轮转、忙碌/Side/内存让路、状态观察、默认关闭。"""
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from bot import statistics_prewarm as prewarm

PAGE_RECORDINGS = (
    'mode=drawn&match=any&not_match=all&opponent_mode=drawn&opponent_match=any'
    '&opponent_not_match=all&self_range=100&opponent_range=100&player='
    '&phase=main_first&phase=main_second&phase=side_first&phase=side_second'
)
PAGE_CARD_200 = (
    'seat=all&metric=opening&threshold=1&min=0&sort=rate'
    '&phase=main_first&phase=main_second&phase=side_first&phase=side_second'
    '&self_range=200&opponent_range=200'
)


def normalize(query):
    return urlencode(sorted(parse_qs(query, keep_blank_values=True).items()), doseq=True)


class _Refresh:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.jobs = queue.Queue(maxsize=8)
        self.items = {}


class _Portal:
    def __init__(self, data_dir):
        self._refresh = _Refresh()
        self.calls = []
        self.replay_root = '/nonexistent/replays'
        self.clock = lambda: 1.0
        self.database = str(data_dir / 'bot.sqlite3')

    def _cached_query_reply(self, key, callback, ttl=10.0, background=False):
        self.calls.append(dict(key=key, ttl=ttl, background=background))
        return 200, 'application/json', b'{"ok":true}', {}


class PrewarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        self.data.mkdir()
        self.portal = _Portal(self.data)

    def meminfo(self, mb=2000):
        path = self.data / 'meminfo'
        path.write_text('MemTotal: 1740000 kB\nMemAvailable: %d kB\n' % (mb * 1024),
                        encoding='utf-8')
        return path

    def test_recordings_key_matches_page(self):
        self.assertEqual(normalize(prewarm.recordings_query('100', '100')),
                         normalize(PAGE_RECORDINGS))
        params = parse_qs(prewarm.recordings_query('200', '200'), keep_blank_values=True)
        self.assertEqual(params['self_range'], ['200'])
        self.assertEqual(params['opponent_range'], ['200'])
        self.assertEqual(params['player'], [''])

    def test_card_key_matches_page_default(self):
        self.assertEqual(normalize(prewarm.card_query('200', '200')), normalize(PAGE_CARD_200))
        params = parse_qs(prewarm.card_query('200', '200'), keep_blank_values=True)
        for absent in ('code', 'exclude', 'combo', 'deck', 'opponent', 'partition', 'limit',
                       'phase_none'):
            self.assertNotIn(absent, params)

    def test_plan_covers_both_ranges_and_both_kinds(self):
        self.assertEqual(prewarm.plan(),
                         (('recordings', '100', '100'), ('card-report', '100', '100'),
                          ('recordings', '200', '200'), ('card-report', '200', '200')))
        self.assertEqual(prewarm.plan(include_card=False),
                         (('recordings', '100', '100'), ('recordings', '200', '200')))

    def test_one_key_per_round_and_cursor_advances(self):
        mem = self.meminfo()
        step, cursor = prewarm.warm_step(self.portal, prewarm.plan(), 0, meminfo_path=mem)
        self.assertEqual((step['step'], step['scope'], cursor), ('recordings', '100-100', 1))
        self.assertEqual(len(self.portal.calls), 1)
        self.assertEqual(normalize(self.portal.calls[0]['key'][1]), normalize(PAGE_RECORDINGS))
        step2, cursor2 = prewarm.warm_step(self.portal, prewarm.plan(), cursor, meminfo_path=mem)
        self.assertEqual((step2['step'], step2['scope'], cursor2), ('card-report', '100-100', 2))
        self.assertTrue(all(call['background'] for call in self.portal.calls))

    def test_cursor_wraps(self):
        mem = self.meminfo()
        _, cursor = prewarm.warm_step(self.portal, prewarm.plan(), 3, meminfo_path=mem)
        self.assertEqual(cursor, 0)

    def test_skips_when_refresh_busy_and_keeps_state(self):
        mem = self.meminfo()
        self.portal._refresh.pending['x'] = threading.Event()
        step, cursor = prewarm.warm_step(self.portal, prewarm.plan(), 2, meminfo_path=mem)
        self.assertEqual(step['skipped'], 'refresh_busy')
        self.assertEqual(cursor, 3)
        self.assertEqual(self.portal.calls, [])
        state = json.loads((self.data / 'prewarm-state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['last_skip']['reason'], 'refresh_busy')
        self.assertEqual(state['last_skip']['step'], 'recordings')

    def test_skips_while_side_batch_running(self):
        mem = self.meminfo()
        (self.data / 'side-inference.lock').write_text('{}', encoding='utf-8')
        step, _ = prewarm.warm_step(self.portal, prewarm.plan(), 1, meminfo_path=mem)
        self.assertEqual(step['skipped'], 'side_batch_running')
        self.assertEqual(self.portal.calls, [])

    def test_skips_when_memory_below_threshold(self):
        mem = self.meminfo(600)          # 卡片需要 650
        step, _ = prewarm.warm_step(self.portal, prewarm.plan(), 1, meminfo_path=mem)
        self.assertEqual(step['skipped'], 'memory_low')
        self.assertEqual(step['available_mb'], 600)
        self.assertEqual(self.portal.calls, [])
        # 录像门槛 550，600 应放行
        step2, _ = prewarm.warm_step(self.portal, prewarm.plan(), 0, meminfo_path=mem)
        self.assertEqual(step2['status'], 200)

    def test_skips_when_meminfo_unavailable(self):
        step, _ = prewarm.warm_step(self.portal, prewarm.plan(), 0,
                                    meminfo_path=self.data / 'missing')
        self.assertEqual(step['skipped'], 'meminfo_unavailable')
        self.assertEqual(self.portal.calls, [])

    def test_state_records_last_success_and_interval(self):
        mem = self.meminfo()
        import time
        saved = time.time() - 120
        self.portal._refresh.items[('recordings',
                                   prewarm.normalize(prewarm.recordings_query('100', '100')))] = dict(
            saved=saved, stamp=None, result=(200, 'x', b'{}'), size=2)
        prewarm.warm_step(self.portal, prewarm.plan(), 0, meminfo_path=mem)
        state = json.loads((self.data / 'prewarm-state.json').read_text(encoding='utf-8'))
        entry = state['keys']['recordings/100-100']
        self.assertAlmostEqual(entry['saved_at'], saved, delta=1)
        self.assertGreaterEqual(entry['age_seconds'], 119)
        self.assertIsNone(state['keys']['card-report/200-200']['saved_at'])

    def test_config_section_defaults(self):
        self.assertEqual(prewarm.config_section({}), {})
        self.assertEqual(prewarm.config_section(dict(prewarm=dict(enabled=False))), {})
        enabled = prewarm.config_section(dict(prewarm=dict(enabled=True)))
        self.assertEqual(enabled['interval'], 300)
        self.assertEqual(len(enabled['steps']), 4)
        custom = prewarm.config_section(dict(prewarm=dict(enabled=True, interval_seconds=60,
                                                         include_card=False)))
        self.assertEqual(custom['interval'], 300)
        self.assertEqual([s[0] for s in custom['steps']], ['recordings', 'recordings'])


if __name__ == '__main__':
    unittest.main()
