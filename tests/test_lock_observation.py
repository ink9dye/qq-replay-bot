import json,tempfile,threading,time,unittest
from pathlib import Path
from bot.web_perf_metrics import ObservedQueryLock,request_metrics,acquire_query_lock
from bot.statistics_refresh import QueryRefresh
from tools.web_latency_report import build_report

class LockObservationTests(unittest.TestCase):
    def test_timeout_names_worker_and_release_clears_owner(self):
        lock=ObservedQueryLock();entered=threading.Event();release=threading.Event()
        def owner():
            with request_metrics('refresh-worker') as m:
                self.assertTrue(acquire_query_lock(lock,1,task='roster'))
                entered.set();release.wait(2);lock.release()
                self.assertGreaterEqual(m['lock_hold_ms'],0)
        t=threading.Thread(target=owner);t.start()
        try:
            self.assertTrue(entered.wait(1))
            with request_metrics() as m:
                self.assertFalse(acquire_query_lock(lock,.01,task='groups'))
            self.assertEqual(m['lock_owner_timeout']['task'],'roster')
            self.assertEqual(m['lock_owner_timeout']['source'],'refresh-worker')
        finally:release.set();t.join(2)
        self.assertIsNone(lock.snapshot())

    def test_recursion_exception_and_private_task(self):
        lock=ObservedQueryLock()
        with request_metrics() as m:
            self.assertTrue(acquire_query_lock(lock,1,task='private-token'))
            with lock:self.assertEqual(lock.snapshot()['task'],'other')
            lock.release()
        self.assertIsNone(lock.snapshot());self.assertNotIn('private-token',json.dumps(m))
        with self.assertRaises(RuntimeError):
            with lock:raise RuntimeError()
        self.assertIsNone(lock.snapshot())

    def test_worker_log_and_report_preserve_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);refresh=QueryRefresh(root/'bot.sqlite3');lock=ObservedQueryLock()
            def compute():
                self.assertTrue(acquire_query_lock(lock,1,task='recordings'));lock.release()
                return 200,'application/json',b'{}',{}
            refresh.get(('recordings','secret-query'),compute,lambda:1,30)
            refresh.jobs.join()
            worker=json.loads(refresh.log.read_text().splitlines()[-1])
            self.assertIn('queue_wait_ms',worker);self.assertIn('lock_hold_ms',worker)
            access=refresh.log.parent/'web-access.jsonl'
            access.write_text(json.dumps(dict(at=time.time(),path='/statistics/api/groups',status=503,
                elapsed_ms=10,lock_owner_timeout=dict(task='recordings',source='refresh-worker',held_ms=50)))+'\n')
            report=build_report([access],since=0)
            self.assertIn('recordings/refresh-worker',report);self.assertIn('queue=',report)
            self.assertNotIn('secret-query',report)
