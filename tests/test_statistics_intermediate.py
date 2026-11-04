import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from bot import statistics_report_intermediate as cache


class IntermediateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=sqlite3.connect(str(self.root/'bot.sqlite3'))
        self.db.executescript('CREATE TABLE stat_games(digest PRIMARY KEY);'
            'CREATE TABLE stat_fact_revisions(digest PRIMARY KEY,revision);'
            'CREATE TEMP TABLE _report_selected(digest,player);'
            'CREATE TEMP TABLE _report_builds(build);'
            "INSERT INTO stat_games VALUES('a'); INSERT INTO _report_selected VALUES('a','p');"
            "INSERT INTO _report_builds VALUES('b');")
        self.called=0
        self.disk=self.root/'query-cache/card-report.sqlite3'

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def loader(self):
        self.called+=1
        return {'copies':[['b','main',1,2]],'facts':[{'code':1,'initial_count':self.called}]}

    def test_persists_raw_tables_and_reuses_across_new_connection(self):
        first=cache.raw_tables(self.db,self.loader)
        self.assertTrue(self.disk.is_file())
        second=cache.raw_tables(self.db,self.loader)
        self.assertEqual(self.called,1);self.assertEqual(first,second)
        self.db.close();self.db=sqlite3.connect(str(self.root/'bot.sqlite3'))
        self.db.executescript("CREATE TEMP TABLE _report_selected(digest,player);INSERT INTO _report_selected VALUES('a','p');CREATE TEMP TABLE _report_builds(build);INSERT INTO _report_builds VALUES('b');")
        cache.raw_tables(self.db,self.loader);self.assertEqual(self.called,1)

    def test_same_report_version_fact_revision_invalidates_immediately(self):
        cache.raw_tables(self.db,self.loader)
        self.db.execute("INSERT INTO stat_fact_revisions VALUES('a',1)")
        self.assertEqual(cache.raw_tables(self.db,self.loader)['facts'][0]['initial_count'],2)
        self.db.execute("UPDATE stat_fact_revisions SET revision=revision+1 WHERE digest='a'")
        cache.raw_tables(self.db,self.loader);self.assertEqual(self.called,3)

    def test_selection_change_and_age_expiry_miss(self):
        cache.raw_tables(self.db,self.loader)
        self.db.execute("UPDATE _report_selected SET player='other'")
        cache.raw_tables(self.db,self.loader);self.assertEqual(self.called,2)
        with sqlite3.connect(str(self.disk)) as db:db.execute('UPDATE entries SET created=0')
        db.close()
        cache.raw_tables(self.db,self.loader);self.assertEqual(self.called,3)

    def test_old_schema_and_corrupt_cache_fail_open(self):
        self.db.execute('DROP TABLE stat_fact_revisions')
        cache.raw_tables(self.db,self.loader);self.assertFalse(self.disk.exists())
        self.db.execute('CREATE TABLE stat_fact_revisions(digest,revision)')
        self.disk.parent.mkdir();self.disk.write_bytes(b'corrupt')
        self.assertEqual(cache.raw_tables(self.db,self.loader)['facts'][0]['initial_count'],2)

    def test_limits_disk_reserve_and_large_payload_bypass(self):
        with patch.object(cache,'MIN_FREE',10**30):cache.raw_tables(self.db,self.loader)
        self.assertFalse(self.disk.exists())
        with patch.object(cache,'MAX_RECORDS',0):cache.raw_tables(self.db,self.loader)
        self.assertFalse(self.disk.exists())
        with patch.object(cache,'MAX_ENTRIES',2):
            for n in range(4):
                self.db.execute('UPDATE _report_selected SET player=?',(str(n),))
                cache.raw_tables(self.db,self.loader)
        db=sqlite3.connect(str(self.disk))
        self.assertEqual(db.execute('SELECT count(*) FROM entries').fetchone()[0],2);db.close()

    def test_complete_response_round_trip_and_source_change_invalidation(self):
        key=('card-report','code=1&metric=opening')
        identity=cache.response_identity(self.root/'bot.sqlite3',key)
        result=(200,'application/json',b'{"ok":true}',{'Cache-Control':'private, no-store'})
        cache.response_put(identity,result)
        self.assertEqual(cache.response_get(identity,10),result)
        (self.root/'card-equivalence.json').write_text('{}')
        self.assertNotEqual(identity,cache.response_identity(self.root/'bot.sqlite3',key))


class ReportIntegrationTests(unittest.TestCase):
    def test_report_cold_hot_identical_and_raw_sql_reused(self):
        import test_card_statistics as fixture_module
        from bot.statistics_card_report import query
        fixture=fixture_module.CardStatisticsTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original=cache.raw_tables;calls=[]
        def trace(db,loader):
            def load():calls.append(True);return loader()
            return original(db,load)
        with patch.object(cache,'raw_tables',trace):
            first=query(fixture.store._db,fixture.replays,fixture_module.NOW,codes=[1,2],metric='opening',threshold='0',combos=['1,2'])
            second=query(fixture.store._db,fixture.replays,fixture_module.NOW,codes=[1,2],metric='opening',threshold='0',combos=['1,2'])
        self.assertEqual(first,second);self.assertEqual(len(calls),1)

    def test_same_version_real_import_advances_revision(self):
        import test_statistics_facts as fixture_module
        fixture=fixture_module.StatisticsFactsTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        row,path=fixture.source();report=fixture.report(row)
        fixture.store.import_replay_facts(row,path,report,fixture_module.NOW)
        revision=fixture.store._db.execute('SELECT revision FROM stat_fact_revisions WHERE digest=?',(row['digest'],)).fetchone()[0]
        fixture.store.import_replay_facts(row,path,report,fixture_module.NOW)
        after=fixture.store._db.execute('SELECT revision FROM stat_fact_revisions WHERE digest=?',(row['digest'],)).fetchone()[0]
        self.assertEqual(after,revision+1)


if __name__=='__main__':unittest.main()
