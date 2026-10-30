import unittest
import test_card_statistics as C


class EvidenceTests(unittest.TestCase):
    def test_positive_usage_with_unknown_draw_is_not_zero_draw(self):
        C.CardStatisticsTests.setUp(self)
        self.store._db.execute("UPDATE stat_card_facts SET drawn=NULL WHERE digest=? AND player='甲' AND code=1 AND partition='main'",(self.rows[4]['digest'],));self.store._db.commit()
        result=self.store.card_statistics(self.replays,1,'main',C.NOW)
        metrics=result['metrics'];e=metrics['evidence']
        self.assertEqual(e['used_draw_unknown_samples'],1)
        self.assertEqual(e['confirmed_used_samples'],3)
        self.assertEqual(metrics['used_win_rate']['denominator'],3)
        self.assertEqual(metrics['draw_rate']['denominator'],3)
        self.assertEqual(metrics['draw_rate']['unknown_samples'],1)
        self.assertEqual(e['complete_draw_samples'],3)

    def test_confirmed_use_without_draw_is_allowed_for_direct_hand_addition(self):
        C.CardStatisticsTests.setUp(self)
        metrics=self.store.card_statistics(self.replays,1,'main',C.NOW)['metrics']
        self.assertGreaterEqual(metrics['evidence']['used_not_drawn_samples'],1)
        self.assertEqual(metrics['used_win_rate']['denominator'],3)
