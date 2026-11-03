import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bot.commands import parse_command
from bot.watch_devices import format_status,recent_games
from bot.watch_receipts import record_receipt,read_receipts
from bot.watch_jobs import RemoteJobs,Job
from bot.watch_api import WatchApiServer

class WatchDevicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_command_and_argument_rejection(self):
        self.assertEqual(parse_command('观战设备'),{'op':'watch_devices'})
        self.assertEqual(parse_command('观战设备 extra')['op'],'error')

    def test_nine_unique_receipts_with_durable_source_and_server_time(self):
        path=self.root/'watch-receipts.json'
        for i in range(12):record_receipt(path,str(i),'pc-a',['A','B'],900,received_at=1000+i)
        record_receipt(path,'11','wrong-source',['A','B'],900,received_at=9999)
        games=recent_games(self.root,[])
        self.assertEqual(len(games),9);self.assertEqual(games[0]['digest'],'11')
        self.assertEqual(games[0]['worker'],'pc-a');self.assertEqual(games[0]['received_at'],1011)
        self.assertEqual(len(read_receipts(path)),9)

    def test_received_not_reported_is_the_counted_unit_and_evidence_is_excluded(self):
        jobs=RemoteJobs();job=Job({'focus':['A'],'room':{'players':['A','B']}},180)
        job.claimed_by='pc-a';jobs._jobs[job.id]=job
        server=WatchApiServer(jobs,self.root/'replays','test',log=lambda *a:None)
        server.save_upload(job.id,'one.yrp',b'one',['A','B'],900)
        server.save_upload(job.id,'one.yrp.evidence.json',b'{}',['A','B'],900)
        rows=read_receipts(self.root/'watch-receipts.json')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['worker'],'pc-a')
        self.assertEqual(jobs.snapshot()['stats']['uploaded'],1)

    def test_ownership_after_restart_requires_unambiguous_fresh_heartbeat(self):
        jobs=RemoteJobs();jobs.heartbeat('pc-a',['old-job'])
        self.assertEqual(jobs.upload_source('old-job'),'pc-a')
        jobs.heartbeat('pc-b',['old-job'])
        self.assertEqual(jobs.upload_source('old-job'),'')
        self.assertEqual(jobs.upload_source('unknown'),'')

    def test_format_separate_queues_and_local_remote_counts(self):
        s=dict(remote_ok=True,summary=dict(active=3,pending=2),workers=[dict(name='pc-a',online=True,active=2,capacity=6,ip='a')],ips=[dict(ip='a',limit=6,active=5)])
        games=[dict(players=['A','B'],worker='pc-a',received_at=1000)]
        text=format_status(s,games,dict(updated_at=1000,active_observations=1,waiting_rooms=7),1000)
        self.assertIn('正在观战：4',text);self.assertIn('等待观战：7',text);self.assertNotIn('待接单：',text)
        self.assertIn('当前最多再接 1',text);self.assertIn('来源：pc-a',text);self.assertIn('A vs B',text)

    def test_stale_metrics_are_not_displayed_as_zero_or_current(self):
        s=dict(remote_ok=False,workers=[],summary={},ips=[])
        text=format_status(s,[],{},1000)
        self.assertIn('正在观战：未知',text);self.assertIn('等待观战：未知',text)
        self.assertIn('未把旧状态当作当前在线',text)

    def test_legacy_games_do_not_invent_source_or_receipt_time(self):
        g=recent_games(self.root,[dict(digest='old',players=['A','B'],saved_at=900)])
        text=format_status(dict(remote_ok=False),g,{},1000)
        self.assertIn('历史未记录来源',text);self.assertIn('接收时间未记录',text)

    def test_service_uses_statistics_allowlist_and_normal_authorization(self):
        from test_bot import temporary_service
        with temporary_service() as service:
            service.config['statistics']={'private_users':['10000','99999']}
            with patch('bot.watch_devices.device_message',return_value='device-status') as fetch:
                self.assertEqual(service.handle_text('10000','观战设备'),'device-status')
                self.assertIn('没有使用权限',service.handle_text('99999','观战设备'))
                fetch.assert_called_once_with(service)

    def test_receipt_file_is_bounded_and_oversized_input_is_not_loaded(self):
        from bot.watch_receipts import MAX_RECEIPT_BYTES
        p=self.root/'watch-receipts.json'
        for i in range(30): record_receipt(p,str(i),'w'*1000,['n'*1000]*100,900,received_at=1000+i)
        rows=read_receipts(p)
        self.assertEqual(len(rows),9);self.assertLess(p.stat().st_size,MAX_RECEIPT_BYTES)
        self.assertEqual(rows[0]['digest'],'21')
        p.write_bytes(b' '* (MAX_RECEIPT_BYTES+1));self.assertEqual(read_receipts(p),[])

    def test_low_disk_rejects_before_writing_replay(self):
        from types import SimpleNamespace
        from bot.watch_api import InsufficientStorageError
        server=WatchApiServer(RemoteJobs(),self.root/'replays','test',log=lambda *a:None)
        with patch('bot.watch_api.shutil.disk_usage',return_value=SimpleNamespace(free=1024)):
            with self.assertRaises(InsufficientStorageError):server.save_upload('job','x.yrp',b'data',['A','B'],900)
        self.assertFalse((self.root/'replays').exists())

    def test_qq_status_skips_replay_database_scan(self):
        from bot.device_dashboard import DeviceDashboard
        p=DeviceDashboard(self.root/'missing.db',self.root,fetch=lambda:dict(ok=True),include_recordings=False)
        p._recordings=lambda: (_ for _ in ()).throw(AssertionError('must not scan recordings'))
        self.assertTrue(p.snapshot()['recordings_ok'])

    def test_old_job_can_report_after_restart_but_not_twice_or_from_other_device(self):
        jobs=RemoteJobs();jobs.heartbeat('pc-a',['old-job'])
        self.assertFalse(jobs.report('pc-b','old-job','saved'))
        self.assertTrue(jobs.report('pc-a','old-job','saved'))
        self.assertFalse(jobs.report('pc-a','old-job','saved'))
        self.assertEqual(jobs.snapshot()['stats']['reported'],1)

    def test_private_allowlist_blocks_other_authorized_users_and_hides_help(self):
        from test_bot import temporary_service
        with temporary_service() as service:
            service.config['statistics']={'private_users':['10000']}
            with patch('bot.watch_devices.device_message',return_value='device-status') as fetch:
                self.assertIsNone(service.handle_text('10001','观战设备'))
                self.assertIsNone(service.handle_text('99999','观战设备'))
                fetch.assert_not_called()
                self.assertNotIn('观战设备：',service.handle_text('10001','帮助'))
                self.assertNotIn('观战设备：',service.handle_text('99999','帮助'))
                self.assertIn('观战设备：',service.handle_text('10000','帮助'))

    def test_groups_use_statistics_group_allowlist_even_for_admins(self):
        from test_bot import temporary_service
        with temporary_service() as service:
            service.config['statistics']={'private_users':['10000'],'allowed_groups':['12345']}
            with patch('bot.watch_devices.device_message',return_value='device-status') as fetch:
                self.assertEqual(service.handle_text('10001','观战设备','group:12345'),'device-status')
                self.assertIsNone(service.handle_text('10000','观战设备','group:99999'))
                self.assertNotIn('观战设备：',service.handle_text('10000','帮助','group:99999'))
                fetch.assert_called_once()

    def test_explicit_empty_private_list_does_not_restore_default_owner(self):
        from test_bot import temporary_service
        with temporary_service() as service:
            service.config['statistics']={'private_users':[]}
            self.assertFalse(service._statistics_allowed('10000'))
            self.assertIsNone(service.handle_text('10000','观战设备'))
