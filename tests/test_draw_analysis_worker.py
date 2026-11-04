import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from bot.draw_analysis_worker import process_one, candidates
from bot.replay_analysis import VERSION, sidecar
from retention_policy import RETENTION_SECONDS
import test_statistics_facts as F
from test_statistics_facts import NOW


class DrawAnalysisWorkerTests(unittest.TestCase):
    setUp = F.StatisticsFactsTests.setUp
    source = F.StatisticsFactsTests.source
    report = F.StatisticsFactsTests.report

    def run_one(self, runner, clock=lambda: NOW):
        return process_one(self.root/'db.sqlite3', self.replays, self.root, runner, clock)

    def test_native_draws_import_automatically_without_migrations_or_renewal(self):
        row,path=self.source(); report=self.report(row); report['version']=VERSION
        runner=Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps(report).encode()))
        self.assertEqual(self.run_one(runner),'analyzed')
        fact=self.store._db.execute('SELECT drawn,initial_count FROM stat_card_facts WHERE digest=? AND player=? AND code=1', (row['digest'],'甲')).fetchone()
        self.assertEqual(tuple(fact),(1,1))
        self.assertEqual(self.store._db.execute('SELECT expires_at FROM stat_games').fetchone()[0],NOW-100+RETENTION_SECONDS)
        self.assertEqual(self.run_one(runner),'idle');runner.assert_called_once()
        self.assertNotIn('--history',runner.call_args.args[0])

    def test_timeout_is_remembered_and_duplicate_copy_does_not_retry(self):
        row,path=self.source();self.source('乙',NOW-20)
        runner=Mock(side_effect=subprocess.TimeoutExpired('test',40))
        self.assertEqual(self.run_one(runner),'failed')
        self.assertEqual(self.run_one(runner),'idle');runner.assert_called_once()

    def test_deleted_during_analysis_is_not_resurrected(self):
        row,path=self.source();report=self.report(row);report['version']=VERSION
        def run(*a,**kw):
            path.unlink();return SimpleNamespace(returncode=0,stdout=json.dumps(report).encode())
        self.assertEqual(self.run_one(run),'source_gone')
        self.assertFalse(sidecar(path).exists())

    def test_expired_and_existing_failed_reports_are_not_replayed(self):
        row,path=self.source(stamp=NOW-RETENTION_SECONDS)
        runner=Mock();self.assertEqual(self.run_one(runner),'idle');runner.assert_not_called()
        row,path=self.source(stamp=NOW-100,code=7)
        sidecar(path).write_text('{"playback":"failed"}')
        self.assertEqual(self.run_one(runner),'idle');runner.assert_not_called()

    def test_expiring_during_analysis_is_not_saved(self):
        row,path=self.source(stamp=NOW-RETENTION_SECONDS+1)
        report=self.report(row);report['version']=VERSION
        ticks=iter([NOW,NOW+2])
        self.assertEqual(self.run_one(Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps(report).encode())),lambda:next(ticks)),'source_gone')
        self.assertFalse(sidecar(path).exists())

    def test_later_duplicate_does_not_renew_expired_source(self):
        self.source(stamp=NOW-RETENTION_SECONDS)
        self.source('乙',NOW-20)
        runner=Mock(); self.assertEqual(self.run_one(runner),'idle');runner.assert_not_called()

    def test_snapshot_candidate_skips_rescan_and_checks_expiry(self):
        from unittest.mock import patch
        row,path=self.source()
        item=next(candidates(self.root/'db.sqlite3',self.replays,NOW))
        report=self.report(row);report['version']=VERSION
        runner=Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps(report).encode()))
        with patch('bot.draw_analysis_worker.candidates',side_effect=AssertionError('unexpected scan')):
            result=process_one(self.root/'db.sqlite3',self.replays,self.root,runner,lambda:NOW,candidate=item)
        self.assertEqual(result,'analyzed');runner.assert_called_once()

    def test_snapshot_expired_while_waiting_does_not_launch_analysis(self):
        row,path=self.source()
        item=next(candidates(self.root/'db.sqlite3',self.replays,NOW))
        runner=Mock()
        result=process_one(self.root/'db.sqlite3',self.replays,self.root,runner,
                           lambda:item[0]['deadline']+1,candidate=item)
        self.assertEqual(result,'source_gone');runner.assert_not_called()


class DrawMemoryGateTests(unittest.TestCase):
    def test_pause_hysteresis_and_unreadable_metrics(self):
        from bot.draw_analysis_worker import MemoryGate
        from unittest.mock import patch
        gate = MemoryGate()
        with patch('bot.draw_analysis_worker.sys.platform', 'linux'), \
                patch('bot.draw_analysis_worker.available_memory_mb', side_effect=[249,275,299,300,250,None,299,301]):
            self.assertEqual([gate.ready() for _ in range(8)], [False,False,False,True,True,False,False,True])

    def test_memavailable_is_used_instead_of_memfree(self):
        from bot.draw_analysis_worker import available_memory_mb
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'meminfo';p.write_text('MemFree: 1024 kB\nMemAvailable: 307200 kB\n')
            self.assertEqual(available_memory_mb(p),300)
            p.write_text('MemAvailable: invalid kB\n')
            self.assertIsNone(available_memory_mb(p))

    def test_low_memory_does_not_scan_queue_or_launch_child(self):
        from unittest.mock import patch
        with patch('bot.draw_analysis_worker.MemoryGate.ready',return_value=False), \
                patch('bot.draw_analysis_worker.candidates',side_effect=AssertionError('must not scan')):
            runner=Mock()
            self.assertEqual(process_one('unused','unused','unused',runner), 'memory_paused')
            runner.assert_not_called()
