import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watch


class WatchPriorityTests(unittest.TestCase):
    def setUp(self):
        for name in ('_official_ranks', '_observed_room_ranks', '_rank_cache'):
            mock = patch.object(watch, name, {})
            mock.start()
            self.addCleanup(mock.stop)

    def room(self, key, ranks):
        players = [f'{key}-{i}' for i in range(len(ranks))]
        watch._official_ranks.update(zip(players, ranks))
        return watch.RoomInfo(key, key, key, players, True)

    def ordered(self, rooms, starts, **kwargs):
        monitor = watch.CaptureMonitor(kwargs.pop('limit', 200))
        result = monitor.candidates(rooms, rooms, kwargs.pop('active', {}),
                                    kwargs.pop('cooldown', {}), 2000,
                                    starts=starts, **kwargs)
        return [r.key for r in result]

    def test_top200_duel_precedes_older_room_with_lower_rank_sum(self):
        other = self.room('other', [1, 201])
        duel = self.room('duel', [199, 200])
        self.assertEqual(self.ordered([other, duel], {'other': 1000, 'duel': 1700}),
                         ['duel', 'other'])

    def test_top200_boundary_requires_both_players(self):
        self.assertTrue(watch.room_is_top200_duel(self.room('yes', [1, 200])))
        for rank in (201, 0, -1, None):
            self.assertFalse(watch.room_is_top200_duel(self.room(str(rank), [1, rank])))
        self.assertFalse(watch.room_is_top200_duel(self.room('single', [1])))
        self.assertFalse(watch.room_is_top200_duel(self.room('empty', [])))

    def test_each_tier_keeps_earlier_batch_first(self):
        for ranks in ([199, 200], [400, 500]):
            with self.subTest(ranks=ranks):
                early = self.room('early', ranks)
                late = self.room('late', [1, ranks[1]])
                self.assertEqual(self.ordered([late, early], {'early': 1000, 'late': 1301}),
                                 ['early', 'late'])

    def test_five_minute_boundary_uses_rank_sum_in_each_tier(self):
        for ranks in ([199, 200], [400, 500]):
            with self.subTest(ranks=ranks):
                early = self.room('early', ranks)
                late = self.room('late', [1, ranks[1]])
                self.assertEqual(self.ordered([early, late], {'early': 1000, 'late': 1300}),
                                 ['late', 'early'])

    def test_batches_anchor_to_first_room_not_previous_room(self):
        a = self.room('a', [199, 200])
        b = self.room('b', [150, 160])
        c = self.room('c', [1, 2])
        self.assertEqual(self.ordered([c, b, a], {'a': 1000, 'b': 1200, 'c': 1400}),
                         ['b', 'a', 'c'])

    def test_equal_sum_uses_start_then_key(self):
        a = self.room('a', [10, 20])
        b = self.room('b', [15, 15])
        self.assertEqual(self.ordered([b, a], {'a': 1000, 'b': 1001}), ['a', 'b'])
        self.assertEqual(self.ordered([b, a], {'a': 1000, 'b': 1000}), ['a', 'b'])

    def test_unknown_rank_keeps_existing_penalty(self):
        room = self.room('unknown', [None, 10])
        self.assertEqual(watch.room_rank_sum(room), 100010)

    def test_cached_rank_sources_and_observed_expiry_without_network(self):
        room = self.room('cached', [None, None])
        a, b = room.players
        watch._observed_room_ranks[a] = (200, 995)
        watch._rank_cache[b] = 100
        with patch.object(watch.time, 'time', return_value=1000), \
                patch.object(watch, 'arena_rank_number', side_effect=AssertionError('network lookup')):
            self.assertTrue(watch.room_is_top200_duel(room))
            watch._official_ranks[a] = 201
            self.assertFalse(watch.room_is_top200_duel(room))
            watch._official_ranks[a] = None
            watch._observed_room_ranks[a] = (200, 880)
            self.assertFalse(watch.room_is_top200_duel(room))

    def test_active_cooldown_and_reserved_rooms_still_excluded(self):
        rooms = [self.room(k, [1, 2]) for k in ('active', 'cool', 'reserved', 'free')]
        self.assertEqual(self.ordered(rooms, {r.key: 1000 for r in rooms},
                                      active={'active': object()}, cooldown={'cool': 2001},
                                      remote_until={'reserved': 2001}), ['free'])

    def test_highest_tier_selected_before_queue_limit(self):
        other = self.room('other', [1, 201])
        duel = self.room('duel', [199, 200])
        self.assertEqual(self.ordered([other, duel], {'other': 1000, 'duel': 1700}, limit=1),
                         ['duel'])


if __name__ == '__main__':
    unittest.main()
