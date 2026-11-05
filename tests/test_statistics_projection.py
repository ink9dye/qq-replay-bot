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


    def test_wide_shards_reuse_unchanged_digests_and_deduplicate_build_copies(self):
        import sqlite3,tempfile
        from pathlib import Path
        from contextlib import closing
        with tempfile.TemporaryDirectory() as tmp, closing(sqlite3.connect(str(Path(tmp)/'bot.sqlite3'))) as db:
            db.executescript('CREATE TABLE stat_games(digest PRIMARY KEY);'
                'CREATE TABLE stat_fact_revisions(digest PRIMARY KEY,revision);'
                'CREATE TABLE stat_views(digest,player,build);'
                'CREATE TEMP TABLE _report_selected(digest,player);'
                'CREATE TEMP TABLE _report_builds(build);'
                "INSERT INTO _report_builds VALUES('shared');")
            for n in range(305):
                digest=str(n).zfill(64)
                db.execute('INSERT INTO stat_games VALUES(?)',(digest,))
                db.execute('INSERT INTO stat_views VALUES(?,?,?)',(digest,'p','shared'))
                db.execute('INSERT INTO _report_selected VALUES(?,?)',(digest,'p'))
            calls=[]
            def loader(selected,builds):
                calls.append(1)
                return dict(copies=[['shared','main',1,2]],facts=[dict(code=1,digest=r[0],player=r[1]) for r in db.execute('SELECT digest,player FROM '+selected)])
            first=cache.raw_tables(db,lambda:None,chunk_loader=loader)
            self.assertEqual(len(first['facts']),305);self.assertEqual(len(first['copies']),1)
            cold=len(calls);self.assertLessEqual(cold,32)
            second=cache.raw_tables(db,lambda:None,chunk_loader=loader)
            self.assertEqual(len(calls),cold);self.assertEqual(first,second)
            db.execute('INSERT INTO stat_fact_revisions VALUES(?,1)',(str(4).zfill(64),))
            third=cache.raw_tables(db,lambda:None,chunk_loader=loader)
            self.assertEqual(len(calls),cold+1);self.assertEqual(first,third)
            # Old databases without revisions must use normal SQL, not break reports.
            db.execute('DROP TABLE stat_fact_revisions')
            self.assertEqual(len(cache.raw_tables(db,lambda:None,chunk_loader=loader)['facts']),305)
