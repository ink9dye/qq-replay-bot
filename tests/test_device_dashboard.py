import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from bot.device_dashboard import DeviceDashboard
from bot.statistics_portal import StatisticsPortal

class DeviceDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.database=self.root/'bot.sqlite3'
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE replays (id INTEGER PRIMARY KEY,digest TEXT,focus_player TEXT,players_json TEXT,recorded_at REAL,saved_at REAL)')
            db.executemany('INSERT INTO replays VALUES (?,?,?,?,?,?)',[(1,'same','A','["A","B"]',900,950),(2,'same','B','["A","B"]',900,951)])
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
