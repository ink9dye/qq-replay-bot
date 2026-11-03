import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from bot.statistics_portal import StatisticsPortal


class QueryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/'db.sqlite3';path.touch()
        self.portal=StatisticsPortal(path,Path(self.tmp.name),lambda:1000,'x'*43)

    def test_simultaneous_identical_queries_compute_once(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def callback(db):
            calls.append(1);entered.set();release.wait(3);return {'ok':True}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.portal._cached_query_reply,('same',),callback)
            self.assertTrue(entered.wait(2))
            second=pool.submit(self.portal._cached_query_reply,('same',),callback)
            release.set()
            self.assertEqual(first.result()[0],200)
            self.assertEqual(second.result()[0],200)
        self.assertEqual(len(calls),1)

    def test_overload_returns_retryable_response_instead_of_starting_query(self):
        entered=threading.Event();release=threading.Event()
        def hold():
            with self.portal._query_compute_lock:
                entered.set();release.wait(4)
        thread=threading.Thread(target=hold);thread.start()
        self.addCleanup(thread.join);self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        with patch('bot.statistics_portal.QUERY_WAIT_SECONDS',0.05):
            result=self.portal._cached_query_reply(('blocked',),lambda db:self.fail('must not compute'))
        self.assertEqual(result[0],503)
        self.assertEqual(result[3]['Retry-After'],'3')
        release.set();thread.join()

    def test_cached_route_returns_while_another_query_holds_gate(self):
        reply=self.portal._cached_query_reply(('roster',),lambda db:{'ok':True},ttl=300,stamp_seconds=86400)
        entered=threading.Event();release=threading.Event()
        def hold():
            with self.portal._query_compute_lock:
                entered.set();release.wait(3)
        thread=threading.Thread(target=hold);thread.start()
        self.addCleanup(thread.join);self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        headers={'Host':'localhost','Cookie':'qqstats_owner='+self.portal._session(1100)}
        with patch('bot.statistics_portal.QUERY_WAIT_SECONDS',0):
            self.assertEqual(self.portal.route('/statistics/api/roster',headers,'127.0.0.1'),reply)
            self.assertEqual(self.portal.route('/statistics/api/roster',{},'127.0.0.1')[0],403)

    def test_cold_query_waits_longer_than_old_one_second_limit(self):
        entered=threading.Event();release=threading.Event()
        def hold():
            with self.portal._query_compute_lock:
                entered.set();release.wait(3)
        thread=threading.Thread(target=hold);thread.start()
        self.addCleanup(thread.join);self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        timer=threading.Timer(1.2,release.set);timer.start();self.addCleanup(timer.join)
        self.assertEqual(self.portal._cached_query_reply(('new',),lambda db:{'ok':True})[0],200)

    def test_expired_cache_never_bypasses_busy_gate(self):
        self.portal._cached_query_reply(('old',),lambda db:{'old':True})
        stamp,_,reply=self.portal._query_cache[('old',)]
        self.portal._query_cache[('old',)]=(stamp,-100,reply)
        entered=threading.Event();release=threading.Event()
        def hold():
            with self.portal._query_compute_lock:
                entered.set();release.wait(3)
        thread=threading.Thread(target=hold);thread.start()
        self.addCleanup(thread.join);self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        with patch('bot.statistics_portal.QUERY_WAIT_SECONDS',0):
            self.assertEqual(self.portal._cached_query_reply(('old',),lambda db:{})[0],503)

    def test_callback_exception_releases_gate(self):
        def fail(db):raise ValueError('test failure')
        with self.assertRaises(ValueError):self.portal._cached_query_reply(('bad',),fail)
        self.assertEqual(self.portal._cached_query_reply(('good',),lambda db:{'ok':True})[0],200)
