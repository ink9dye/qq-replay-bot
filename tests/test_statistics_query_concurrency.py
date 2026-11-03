import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
        result=self.portal.route('/statistics/api/matrix',{},'127.0.0.1')
        self.assertEqual(result[0],503)
        self.assertEqual(result[3]['Retry-After'],'3')
        release.set();thread.join()

    def test_callback_exception_releases_gate(self):
        def fail(db):raise ValueError('test failure')
        with self.assertRaises(ValueError):self.portal._cached_query_reply(('bad',),fail)
        self.assertEqual(self.portal._cached_query_reply(('good',),lambda db:{'ok':True})[0],200)
