import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from bot.statistics_refresh import QueryRefresh


def reply(value):return 200,'application/json',json.dumps(value).encode(),{}


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.database=Path(self.temp.name)/'bot.sqlite3'
        self.cache=QueryRefresh(self.database);self.addCleanup(self.cache.jobs.join)

    def test_concurrent_same_query_computes_once(self):
        entered=threading.Event();release=threading.Event();calls=[]
        self.addCleanup(release.set)
        def compute():calls.append(1);entered.set();release.wait(2);return reply({'count':7})
        for _ in range(6):self.cache.get(('roster',),compute,lambda:1,30,wait=0)
        self.assertTrue(entered.wait(1));release.set();self.cache.jobs.join()
        self.assertEqual(len(calls),1)
        result=self.cache.get(('roster',),compute,lambda:1,30)
        self.assertEqual(json.loads(result[2])['count'],7)

    def test_old_result_is_fast_marked_and_never_crosses_conditions(self):
        self.cache.get(('recordings','player=A'),lambda:reply({'player':'A'}),lambda:1,30)
        with self.cache.lock:self.cache.items[('recordings','player=A')]['saved']-=36
        release=threading.Event();self.addCleanup(release.set)
        def compute():release.wait(2);return reply({'player':'A','new':True})
        started=time.monotonic()
        result=self.cache.get(('recordings','player=A'),compute,lambda:2,30)
        self.assertLess(time.monotonic()-started,.2)
        self.assertEqual(json.loads(result[2])['_query_refresh']['state'],'refreshing')
        self.assertEqual(self.cache.get(('recordings','player=B'),compute,lambda:2,30,wait=0)[0],503)
        release.set()

    def test_snapshot_survives_restart_as_explicit_old_result(self):
        self.cache.get(('card-report',''),lambda:reply({'rows':[1]}),lambda:1,10)
        self.cache.jobs.join()
        other=QueryRefresh(self.database);self.addCleanup(other.jobs.join)
        result=other.get(('card-report',''),lambda:reply({'rows':[2]}),lambda:1,10,wait=0)
        self.assertEqual(json.loads(result[2])['rows'],[1])
        self.assertEqual(result[3]['X-Statistics-State'],'stale')

    def test_old_snapshot_expires_and_queue_is_bounded(self):
        release=threading.Event();self.addCleanup(release.set)
        def compute():release.wait(2);return reply({'ok':True})
        for i in range(25):self.cache.get(('recordings',str(i)),compute,lambda:1,10,wait=0)
        self.assertLessEqual(len(self.cache.pending),9)
        self.assertLessEqual(self.cache.jobs.qsize(),8)
        release.set();self.cache.jobs.join()
        with self.cache.lock:
            self.cache.items[('recordings','0')]['saved']-=1801
        with patch.object(self.cache,'_read',return_value=None):
            self.assertEqual(self.cache.get(('recordings','0'),compute,lambda:2,10,wait=0)[0],503)

    def test_invalid_query_returns_validation_error(self):
        def invalid():raise ValueError('条件无效')
        result=self.cache.get(('recordings','invalid'),invalid,lambda:1,10)
        self.assertEqual(result[0],400);self.assertIn('条件无效',json.loads(result[2])['error'])

    def test_different_queries_share_one_compute_worker(self):
        active=0;maximum=0;lock=threading.Lock()
        def compute():
            nonlocal active,maximum
            with lock:active+=1;maximum=max(maximum,active)
            time.sleep(.01)
            with lock:active-=1
            return reply({'ok':True})
        for i in range(5):self.cache.get(('card-report',str(i)),compute,lambda:1,10,wait=0)
        self.cache.jobs.join();self.assertEqual(maximum,1)

    def test_corrupt_disk_snapshot_is_ignored(self):
        self.cache.directory.mkdir(parents=True)
        self.cache._path(('roster',)).write_text(json.dumps(dict(saved=time.time(),result=[])))
        self.assertIsNone(self.cache._read(('roster',)))

    def test_no_card_recording_snapshot_defers_equivalence_read(self):
        from bot import statistics_recording_filters as filters
        with patch.object(filters,'eligible_views',return_value={'views':[]}),patch.object(
                filters,'reference',return_value=({99:(9,)},None,None)) as load:
            result=filters.snapshot(None,'.',0,annotate_series=False,with_facts=False)
            load.assert_not_called()
            self.assertEqual(result[-1](99),9);load.assert_called_once()


class RefreshPressureTests(unittest.TestCase):
    setUp = RefreshTests.setUp
    def test_continuous_version_changes_keep_explicit_stale_without_rebuilding(self):
        calls=[]
        def compute():calls.append(1);return reply({'value':1})
        self.cache.get(('card-report','q'),compute,lambda:1,10)
        self.cache.jobs.join()
        for version in range(2,20):
            response=self.cache.get(('card-report','q'),compute,lambda:version,10,wait=0)
            self.assertEqual(response[3]['X-Statistics-State'],'stale')
        self.assertEqual(len(calls),1)

    def test_keepwarm_yields_to_requested_work_and_slow_roster(self):
        self.cache.get(('roster',),lambda:reply({'ok':1}),lambda:1,300)
        self.cache.jobs.join()
        with self.cache.lock:
            self.cache.items[('roster',)]['saved']-=301
            self.cache.durations[('roster',)]=200
        calls=[]
        self.cache.get(('roster',),lambda:calls.append(1) or reply({}),lambda:2,300,wait=0,background=True)
        self.assertFalse(calls)
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def slow():entered.set();release.wait(2);return reply({})
        self.cache.get(('card-report','busy'),slow,lambda:1,10,wait=0)
        self.assertTrue(entered.wait(1))
        self.cache.get(('roster',),lambda:calls.append(1) or reply({}),lambda:2,300,wait=0,background=True)
        self.assertNotIn(('roster',),self.cache.pending)
        release.set()

    def test_failed_key_has_backoff_without_requeue_storm(self):
        calls=[]
        def failure():calls.append(1);raise ValueError('bad')
        self.cache.get(('card-report','invalid'),failure,lambda:1,10)
        self.cache.jobs.join()
        for _ in range(10):self.assertEqual(self.cache.get(('card-report','invalid'),failure,lambda:2,10)[0],400)
        self.assertEqual(calls,[1])

    def test_write_during_compute_never_claims_fresh(self):
        version=[1]
        def compute():version[0]=2;return reply({'ok':1})
        response=self.cache.get(('card-report','q'),compute,lambda:version[0],10)
        self.assertEqual(response[3]['X-Statistics-State'],'stale')
