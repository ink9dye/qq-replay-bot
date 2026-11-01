import unittest
from bot.watch_rank_window import RankWindow


class RankWindowTests(unittest.TestCase):
    def test_pause_blocks_then_resumes_best_effort(self):
        now = [1000.0]
        calls = []

        def fetch(name):
            calls.append(name)
            raise TimeoutError('timeout')

        window = RankWindow(limit=200, fetch=fetch, clock=lambda: now[0])
        window.errors = 8
        window.pause_until = now[0] + 120
        window.update({'p'}, set(), now[0], False)
        self.assertEqual(calls, [])
        now[0] += 121
        window.update({'p'}, set(), now[0], False)
        self.assertGreater(len(calls), 0)

    def test_known_top100_players_are_never_queried(self):
        now = [1000.0]
        calls = []
        window = RankWindow(limit=200, fetch=lambda name: calls.append(name), clock=lambda: now[0])
        window.update({'甲'}, {'甲'}, now[0], False)
        self.assertNotIn('甲', calls)
