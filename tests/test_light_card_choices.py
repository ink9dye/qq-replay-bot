import unittest
from recording_filter_fixture import set_up
import test_card_statistics as C
from bot.statistics_card_overview import query,choices

class LightChoicesTests(unittest.TestCase):
    setUp=set_up
    def test_projection_and_order_equal_full_overview(self):
        full=query(self.store._db,self.replays,C.NOW)
        expected=[{k:r[k] for k in ('code','name','base_partition')} for r in full['rows']]
        self.assertEqual(choices(self.store._db,self.replays,C.NOW)['rows'],expected)
    def test_exclusion_and_missing_source_preserve_projection(self):
        (self.replays/self.rows[0]['rel_path']).unlink()
        self.store.set_fact_exclusion(self.rows[1]['digest'],'甲',reason='test')
        self.test_projection_and_order_equal_full_overview()
