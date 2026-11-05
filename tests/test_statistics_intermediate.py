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

    def test_slow_summary_writes_after_crossing_ten_second_boundary(self):
        from bot.statistics_portal import StatisticsPortal
        portal=StatisticsPortal(self.root/'bot.sqlite3',self.root/'replays',time.time,'x'*43)
        key=('card-report','code=38814750&combo=38814750%2C49036338')
        result={'combo':True}
        clock=[1001.0];calls=[]
        def compute(db):calls.append(True);clock[0]+=21;return result
        with patch.object(cache.time,'time',side_effect=lambda:clock[0]):
            first=portal._cached_query_reply(key,compute)
            self.assertEqual(json.loads(first[2]),result)
            other=StatisticsPortal(self.root/'bot.sqlite3',self.root/'replays',time.time,'x'*43)
            self.assertEqual(other._cached_query_reply(key,compute),first)
            self.assertEqual(len(calls),1)
            clock[0]+=11
            other._cached_query_reply(key,compute)
            self.assertEqual(len(calls),2)

    def test_new_replay_result_and_naming_changes_invalidate_summary_without_memory_fallback(self):
        from bot.statistics_portal import StatisticsPortal
        portal=StatisticsPortal(self.root/'bot.sqlite3',self.root/'replays',time.time,'x'*43)
        key=('card-report','code=10045474&combo=10045474%2C27204311')
        calls=[]
        def compute(db):
            calls.append(True)
            return {'revision':len(calls)}
        first=portal._cached_query_reply(key,compute)
        self.assertEqual(portal._cached_query_reply(key,compute),first)
        self.assertEqual(len(calls),1)
        self.db.execute("INSERT INTO stat_games VALUES('new')");self.db.commit()
        portal._cached_query_reply(key,compute);self.assertEqual(len(calls),2)
        self.db.execute("INSERT INTO stat_fact_revisions VALUES('a',1)");self.db.commit()
        portal._cached_query_reply(key,compute);self.assertEqual(len(calls),3)
        for filename in ('series-naming.json','watch-upload-sessions.sqlite3',
                         'watch-upload-sessions.sqlite3-wal','config.json'):
            (self.root/filename).write_text('changed')
            portal._cached_query_reply(key,compute)
        self.assertEqual(len(calls),7)
        different=portal._cached_query_reply(('card-report',key[1]+'&seat=first'),compute)
        self.assertEqual(len(calls),8)

    def test_data_change_during_summary_computation_does_not_cache_result(self):
        from bot.statistics_portal import StatisticsPortal
        portal=StatisticsPortal(self.root/'bot.sqlite3',self.root/'replays',time.time,'x'*43)
        key=('card-report','combo=1%2C2');calls=[]
        def compute(db):
            calls.append(True)
            self.db.execute('INSERT INTO stat_games VALUES(?)',('new'+str(len(calls)),));self.db.commit()
            return {}
        portal._cached_query_reply(key,compute);portal._cached_query_reply(key,compute)
        self.assertEqual(len(calls),2)

    def test_file_hard_limit_accounts_for_nondefault_page_size(self):
        self.disk.parent.mkdir()
        db=sqlite3.connect(str(self.disk));db.execute('PRAGMA page_size=8192')
        db.execute('CREATE TABLE entries(key TEXT PRIMARY KEY,created REAL NOT NULL,payload BLOB NOT NULL)')
        db.commit();db.close()
        original_connect=sqlite3.connect;observed_limits=[]
        class ObservedConnection(sqlite3.Connection):
            def close(connection):
                observed_limits.append(connection.execute('PRAGMA max_page_count').fetchone()[0]*
                                       connection.execute('PRAGMA page_size').fetchone()[0])
                super().close()
        def connect(*args,**kwargs):
            if not kwargs.get('uri') and str(args[0])==str(self.disk):kwargs['factory']=ObservedConnection
            return original_connect(*args,**kwargs)
        with patch.object(cache.sqlite3,'connect',side_effect=connect):cache.raw_tables(self.db,self.loader)
        self.assertTrue(observed_limits)
        self.assertTrue(all(size<=96*1024*1024 for size in observed_limits))
        db=sqlite3.connect(str(self.disk))
        page_size=db.execute('PRAGMA page_size').fetchone()[0]
        # max_page_count is per-connection; also verify continued cache reuse.
        self.assertEqual(page_size,8192);self.assertLessEqual(self.disk.stat().st_size,96*1024*1024)
        db.close();cache.raw_tables(self.db,self.loader);self.assertEqual(self.called,1)


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
