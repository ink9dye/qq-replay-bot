import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock
from bot.device_dashboard import DeviceDashboard
from bot.statistics_portal import StatisticsPortal

class DeviceDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.database=self.root/'bot.sqlite3'
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE replays (id INTEGER PRIMARY KEY,digest TEXT,focus_player TEXT,players_json TEXT,recorded_at REAL,saved_at REAL)')
            db.executemany('INSERT INTO replays VALUES (?,?,?,?,?,?)',[(1,'same','A','["A","B"]',900,950),(2,'same','B','["A","B"]',900,951)])
            db.commit()
        self.now=1000
        self.fetch=Mock(return_value=dict(ok=True,workers={'a':dict(ip='1.2.3.4',online=True,active=2,capacity=6),'b':dict(ip='1.2.3.4',online=True,active=3,capacity=6),'old':dict(ip='5.6.7.8',online=False,active=4,capacity=6)},ip_cap=6,pending=1,token='DO_NOT_EXPOSE'))
        self.dashboard=DeviceDashboard(self.database,self.root,token='SECRET',fetch=self.fetch,clock=lambda:self.now)

    def test_ip_limit_deduplication_and_no_credentials(self):
        s=self.dashboard.snapshot()
        self.assertEqual(s['summary'],dict(online=2,active=5,capacity=6,pending=1))
        self.assertEqual(len(s['recordings']),1)
        self.assertEqual(set(s['recordings'][0]['focus']),{'A','B'})
        self.assertNotIn('SECRET',json.dumps(s));self.assertNotIn('DO_NOT_EXPOSE',json.dumps(s))

    def test_cache_avoids_refetch_and_repeat_database_work(self):
        first=self.dashboard.snapshot();self.now+=2
        self.dashboard._recordings=Mock(side_effect=AssertionError('uncached database read'))
        self.assertIs(self.dashboard.snapshot(),first);self.fetch.assert_called_once()

    def test_remote_failure_retains_sample_but_marks_stale(self):
        self.dashboard.snapshot();self.now+=6;self.fetch.side_effect=TimeoutError()
        s=self.dashboard.snapshot()
        self.assertFalse(s['remote_ok']);self.assertEqual(s['status_at'],1000)
        self.assertEqual(len(s['workers']),3);self.assertTrue(s['error'])

    def test_startup_without_remote_is_not_a_zero_online_success(self):
        self.fetch.side_effect=TimeoutError()
        s=self.dashboard.snapshot();self.assertFalse(s['remote_ok']);self.assertIsNone(s['status_at'])
        self.assertTrue(s['recordings_ok'])

    def test_database_failure_is_not_reported_as_empty_history(self):
        self.database.unlink();s=self.dashboard.snapshot()
        self.assertFalse(s['recordings_ok']);self.assertTrue(s['remote_ok'])
        self.assertIsNone(s['ingest'])

    def test_ingest_summary_uses_bounded_tail_and_hour_buckets(self):
        now=1000000
        database=self.root/'ingest.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE replays (id INTEGER PRIMARY KEY,digest TEXT,focus_player TEXT,'
                       'players_json TEXT,recorded_at REAL,saved_at REAL)')
            db.executemany('INSERT INTO replays VALUES (?,?,?,?,?,?)',
                [(1,'a','A','["A"]',0,now-120),(2,'b','B','["B"]',0,now-3600),(3,'c','C','["C"]',0,now-2*3600),
                 (4,'d','D','["D"]',0,now-30*3600),(5,'e','E','["E"]',0,None)])
            db.commit()
        dashboard=DeviceDashboard(database,self.root,fetch=lambda:dict(ok=True),clock=lambda:now)
        ingest=dashboard.snapshot()['ingest']
        self.assertEqual(ingest['last_saved_at'],now-120)
        self.assertAlmostEqual(ingest['age_seconds'],120)
        self.assertEqual(ingest['count_1h'],2);self.assertEqual(ingest['count_6h'],3);self.assertEqual(ingest['count_24h'],3)
        self.assertEqual(len(ingest['hourly']),24)
        self.assertEqual(sum(row['n'] for row in ingest['hourly']),3)
        self.assertEqual(ingest['hourly'][-1]['t'],(now//3600)*3600)
        self.assertEqual(ingest['hourly'][-1]['n'],1)

    def test_ingest_without_replays_is_not_a_fake_zero_time(self):
        dashboard=DeviceDashboard(self.root/'empty.sqlite3',self.root,fetch=lambda:dict(ok=True),clock=lambda:1000)
        with closing(sqlite3.connect(self.root/'empty.sqlite3')) as db:
            db.execute('CREATE TABLE replays (id INTEGER PRIMARY KEY,digest TEXT,focus_player TEXT,'
                       'players_json TEXT,recorded_at REAL,saved_at REAL)')
            db.commit()
        ingest=dashboard.snapshot()['ingest']
        self.assertIsNone(ingest['last_saved_at']);self.assertIsNone(ingest['age_seconds'])
        self.assertEqual(ingest['count_1h'],0);self.assertEqual(len(ingest['hourly']),24)

    def test_uploaded_counter_reaches_the_page_payload(self):
        self.fetch.return_value=dict(ok=True,workers={},stats=dict(offered=3,claimed=2,reported=1,uploaded=4,failed=1))
        self.assertEqual(self.dashboard.snapshot()['stats']['uploaded'],4)

    def _fake_room(self, uploaded=0):
        return dict(job='j1',worker='a',room='M#1',title='房间',players=['A','B'],
                    claimed_at=self.now-30,uploaded=uploaded,last_upload_at=0,focus=['A'],reason='test')

    def test_room_disappears_without_upload_is_marked_failed(self):
        self.fetch.return_value=dict(ok=True,workers={'a':dict(ip='1.2.3.4',online=True,active=1,capacity=2)},
                                     rooms=[self._fake_room()],ip_cap=6,pending=0)
        self.assertEqual(len(self.dashboard.snapshot()['rooms']),1)
        self.now+=6
        self.fetch.return_value=dict(ok=True,workers={'a':dict(ip='1.2.3.4',online=True,active=0,capacity=2)},
                                     rooms=[],ip_cap=6,pending=0)
        s=self.dashboard.snapshot()
        self.assertEqual(s['ended'][0]['verdict'],'未能传回录像')

    def test_room_disappears_with_upload_is_marked_returned(self):
        self.fetch.return_value=dict(ok=True,workers={'a':dict(ip='1.2.3.4',online=True,active=1,capacity=2)},
                                     rooms=[self._fake_room(uploaded=2)],ip_cap=6,pending=0)
        self.assertEqual(self.dashboard.snapshot()['rooms'][0]['uploaded'],2)
        self.now+=6
        self.fetch.return_value=dict(ok=True,workers={'a':dict(ip='1.2.3.4',online=True,active=0,capacity=2)},
                                     rooms=[],ip_cap=6,pending=0)
        s=self.dashboard.snapshot()
        self.assertEqual(s['ended'][0]['verdict'],'已回传 2 个')

    def test_receipt_fills_upload_count_after_job_disappears(self):
        self.fetch.return_value=dict(ok=True,workers={'a':dict(ip='1.2.3.4',online=True,active=1,capacity=2)},
                                     rooms=[self._fake_room()],ip_cap=6,pending=0)
        self.dashboard.snapshot()
        (self.root/'watch-receipts.json').write_text(json.dumps([
            dict(digest='d1',worker='a',players=['A','B'],received_at=self.now+1)]),encoding='utf-8')
        self.now+=6
        self.fetch.return_value=dict(ok=True,workers={'a':dict(ip='1.2.3.4',online=True,active=0,capacity=2)},
                                     rooms=[],ip_cap=6,pending=0)
        s=self.dashboard.snapshot()
        self.assertEqual(s['ended'][0]['verdict'],'已回传 1 个')

    def test_owner_auth_and_redirect_allowlist(self):
        p=StatisticsPortal(self.database,self.root,lambda:self.now,'x'*43);p.device_dashboard=self.dashboard
        h={'Host':'127.0.0.1:8788'}
        self.assertEqual(p.route('/statistics/api/devices',h,'127.0.0.1')[0],403)
        reply=p.route('/statistics/access/'+'x'*43+'?next=/statistics/devices',h,'127.0.0.1')
        self.assertEqual(reply[3]['Location'],'/statistics/devices')
        h['Cookie']=reply[3]['Set-Cookie'].split(';')[0]
        self.assertEqual(p.route('/statistics/api/devices',h,'127.0.0.1')[0],200)
        self.assertEqual(p.route('/statistics/devices',h,'127.0.0.1')[0],200)
        h['X-Forwarded-For']='1.2.3.4'
        self.assertEqual(p.route('/statistics/api/devices',h,'127.0.0.1')[0],403)
