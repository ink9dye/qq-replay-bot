import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.arena_profile import arena_points, _CACHE, ArenaScore
from test_bot import temporary_service, link_token


class ArenaProfileTests(unittest.TestCase):
    def test_failure_retains_success_rank_points_and_time_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [1000]
            score = arena_points('甲', tmp, fetch=lambda p: {'pt': 1592.25, 'arena_rank': 37}, clock=lambda: now[0])
            self.assertEqual((float(score), score.rank, score.updated_at), (1592.25, 37, 1000))
            now[0] += 120
            _CACHE.clear()
            def fail(_):
                raise OSError('network down')
            old = arena_points('甲', tmp, fetch=fail, clock=lambda: now[0])
            self.assertEqual((float(old), old.rank, old.updated_at), (1592.25, 37, 1000))
            self.assertTrue(old.stale)
            _CACHE.clear()
            cached = arena_points('甲', tmp, refresh=False, clock=lambda: now[0])
            self.assertEqual(float(cached), 1592.25)
            self.assertTrue(cached.stale)

    def test_simultaneous_requests_wait_instead_of_receiving_placeholder_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def fetch(player):
                calls.append(player)
                return {'pt': 1234, 'arena_rank': 42}
            with concurrent.futures.ThreadPoolExecutor(5) as pool:
                results = list(pool.map(lambda _: arena_points('甲', tmp, fetch=fetch), range(5)))
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(r == 1234 and r.rank == 42 for r in results))

    def test_failed_or_invalid_first_request_does_not_invent_score_or_rank(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(arena_points('甲', tmp, fetch=lambda p: {'pt': float('nan'), 'arena_rank': 3}))
            self.assertIsNone(arena_points('乙', tmp, fetch=lambda p: {'arena_rank': 5}))

    def test_read_only_page_lookup_never_calls_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            def unexpected(_):
                self.fail('page lookup must not wait on network')
            self.assertIsNone(arena_points('甲', tmp, fetch=unexpected, refresh=False))

    def test_page_shows_score_rank_and_timestamp_even_without_deck_or_replays(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            reply = service.handle_text('10001', '录像 甲')
            score = ArenaScore({'pt': 1592, 'rank': 37, 'updated_at': 1700000000}, stale=True)
            with patch('bot.share.arena_points', return_value=score):
                status, _, body = service.share.page(link_token(reply), 'dir')
            text = body.decode()
            self.assertEqual(status, 200)
            self.assertIn('1592分', text)
            self.assertIn('第37名', text)
            self.assertIn('更新于', text)
            self.assertIn('最近成功记录', text)
            self.assertNotIn('分数暂不可用', text)
