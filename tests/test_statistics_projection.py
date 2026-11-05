import json,subprocess,types,unittest
from unittest.mock import patch
import test_card_statistics as fixture_module
from bot import statistics_card_report as report, statistics_report_intermediate as cache

class ProjectionTests(unittest.TestCase):
    def test_narrow_and_wide_match_baseline_for_alias_combo_and_negative_conditions(self):
        fixture=fixture_module.CardStatisticsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        module=types.ModuleType('bot._baseline_card_report');module.__package__='bot'
        source=subprocess.run(['git','show','acb8996:bot/statistics_card_report.py'],capture_output=True,text=True,encoding='utf8',check=True).stdout
        exec(compile(source,'baseline_card_report','exec'),module.__dict__)
        db=fixture.store._db
        cases=[{},dict(codes=[1]),dict(codes=[1,2],combos=['1,2']),dict(combos=['1,2']),dict(codes=[1],exclude=[2]),dict(codes=[9],partition='extra')]
        with patch.object(cache,'raw_tables',lambda db,loader,**kwargs:loader()):
            for args in cases:
                with self.subTest(args=args):
                    before=module.query(db,fixture.replays,fixture_module.NOW,threshold='0',**args)
                    after=report.query(db,fixture.replays,fixture_module.NOW,threshold='0',**args)
                    self.assertEqual(before,after)
        statements=[];db.set_trace_callback(statements.append)
        report.query(db,fixture.replays,fixture_module.NOW,codes=[1],threshold='0')
        facts=[s for s in statements if 'FROM stat_card_facts f WHERE (f.digest,f.player)' in s]
        self.assertTrue(facts);self.assertTrue(all('code IN (1)' in s for s in facts))
        db.set_trace_callback(None)

    def test_projected_cache_never_serves_subset_as_full_data(self):
        import test_statistics_intermediate as t
        fixture=t.IntermediateTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        cache.raw_tables(fixture.db,fixture.loader,projection=[1])
        cache.raw_tables(fixture.db,fixture.loader,projection=[2])
        cache.raw_tables(fixture.db,fixture.loader,projection=None)
        self.assertEqual(fixture.called,3)
        cache.raw_tables(fixture.db,fixture.loader,projection=[1]);self.assertEqual(fixture.called,3)


    def test_extra_report_is_identical_to_full_scan_baseline(self):
        from bot import statistics_extra_report as extra
        fixture=fixture_module.CardStatisticsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        module=types.ModuleType('bot._baseline_extra_report');module.__package__='bot'
        source=subprocess.run(['git','show','acb8996:bot/statistics_extra_report.py'],capture_output=True,text=True,encoding='utf8',check=True).stdout
        exec(compile(source,'baseline_extra_report','exec'),module.__dict__)
        for kwargs in ({},dict(phase_none=True),dict(filters=report.Filters(first_player=True))):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(module.query(fixture.store._db,fixture.replays,fixture_module.NOW,**kwargs),extra.query(fixture.store._db,fixture.replays,fixture_module.NOW,**kwargs))
