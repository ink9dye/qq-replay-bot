import unittest
from unittest.mock import patch
from recording_filter_fixture import set_up
import test_card_statistics as C
from bot.top100 import eligible_views
from bot.web_perf_metrics import request_metrics

class EligibleObservationTests(unittest.TestCase):
    setUp=set_up

    def test_cold_warm_outputs_equal_and_cache_counters_match(self):
        with request_metrics() as cold:
            first=eligible_views(self.store._db,self.replays,C.NOW,annotate_series=False)
        with request_metrics() as warm:
            second=eligible_views(self.store._db,self.replays,C.NOW,annotate_series=False)
        self.assertEqual(first,second)
        self.assertGreater(cold['source_cache_misses'],0)
        self.assertEqual(warm['source_cache_hits'],cold['source_cache_misses'])
        self.assertEqual(warm['source_cache_misses'],0)
        self.assertEqual(cold['source_index_cache_misses'],1)
        self.assertEqual(warm['source_index_cache_hits'],1)
        for field in ('eligible_sql_ms','eligible_source_ms','eligible_source_index_ms'):
            self.assertGreaterEqual(cold[field],0)

    def test_skipped_source_check_is_not_reported_as_cache_hit(self):
        with request_metrics() as metrics:
            eligible_views(self.store._db,self.replays,C.NOW,check_files=False,annotate_series=False)
        self.assertIn('eligible_sql_ms',metrics)
        self.assertNotIn('source_cache_hits',metrics)
