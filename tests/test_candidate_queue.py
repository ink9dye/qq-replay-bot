import json,tempfile,threading,unittest,time
from pathlib import Path
from unittest.mock import patch
from bot.statistics_portal import StatisticsPortal
from bot.statistics_refresh import QueryRefresh
from bot.web_perf_metrics import ObservedQueryLock

class CandidateQueueTests(unittest.TestCase):
    def test_candidate_route_does_not_wait_for_global_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            portal=StatisticsPortal(Path(tmp)/'db',Path(tmp),time.time,'x'*43)
            entered=threading.Event();release=threading.Event()
            def hold():
                with portal._query_compute_lock:entered.set();release.wait(2)
            thread=threading.Thread(target=hold);thread.start()
            try:
                self.assertTrue(entered.wait(1))
                with patch.object(portal,'_route',return_value=(200,'x',b'queued',{})) as route:
                    result=portal.route('/statistics/api/groups?candidates=1',{},'127.0.0.1')
                self.assertEqual(result[0],200);route.assert_called_once()
            finally:release.set();thread.join()

    def test_candidate_waits_in_existing_worker_then_returns_same_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            refresh=QueryRefresh(Path(tmp)/'db');entered=threading.Event();release=threading.Event();calls=[]
            def heavy():entered.set();release.wait(2);return 200,'application/json',b'{}',{}
            refresh.get(('recordings',''),heavy,lambda:1,30,wait=0)
            self.assertTrue(entered.wait(1))
            def candidate():calls.append(1);return 200,'application/json',b'{"deck":["same"]}',{}
            try:
                for _ in range(3):self.assertEqual(refresh.get(('deck-candidates','q=x'),candidate,lambda:1,30,wait=0)[0],503)
            finally:release.set();refresh.jobs.join()
            result=refresh.get(('deck-candidates','q=x'),candidate,lambda:2,30)
            self.assertEqual(json.loads(result[2])['deck'],['same']);self.assertEqual(len(calls),1)
            with refresh.lock:refresh.items[('deck-candidates','q=x')]['saved']-=31
            gate=threading.Event()
            def slow_candidate():gate.wait(2);return candidate()
            try:
                result=refresh.get(('deck-candidates','q=x'),slow_candidate,lambda:3,30,wait=0)
                self.assertEqual(result[0],503,'expired candidate must not be used')
            finally:gate.set();refresh.jobs.join()
