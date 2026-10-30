import unittest
import test_card_statistics as C
import test_top100 as T
from bot.statistics_card_overview import query


class OverviewTests(unittest.TestCase):
    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)

    def test_bulk_bins_match_single_player_query_and_unknown_side_is_not_zero(self):
        bulk=query(self.store._db,self.replays,C.NOW)
        card=next(r for r in bulk['rows'] if r['code']==1)
        single=self.store.player_card_statistics(self.replays,1,C.NOW)
        for part in ('main','side','extra'):
            actual=card['partitions'][part];expected=single['observed_distributions'][part]
            self.assertEqual(actual['known_copy_players'],expected['known_players'])
            self.assertEqual(actual['unknown_copy_players'],expected['unknown_players'])
            for b in expected['bins']:
                if b['copies']<4:self.assertEqual(actual['bins'][str(b['copies'])],b['players'])
        self.assertEqual(card['partitions']['side']['unknown_copy_players'],2)
        self.assertEqual(card['partitions']['side']['unknown_presence_players'],2)
        self.assertEqual(bulk['known_basis_players'],2)

    def test_initial_missing_does_not_fall_back_to_latest_or_zero(self):
        bulk=query(self.store._db,self.replays,C.NOW,'initial')
        self.assertEqual((bulk['known_basis_players'],bulk['unknown_basis_players']),(0,2))
        self.assertEqual(bulk['rows'],[])

    def test_dropout_and_expiry_apply_before_person_selection(self):
        self.store.replace_top100(T.roster(['甲']),C.NOW)
        bulk=query(self.store._db,self.replays,C.NOW)
        self.assertEqual(bulk['covered_players'],1)
        self.assertEqual(query(self.store._db,self.replays,C.NOW+14*86400)['rows'],[])
