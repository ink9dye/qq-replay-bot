import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from bot import statistics_dashboard as dashboard, statistics_deck_naming as naming
from bot.statistics_portal import StatisticsPortal
from bot.statistics_roster_version import snapshot_version


class RosterConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'bot.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE value(label TEXT); INSERT INTO value VALUES(NULL);
            CREATE TABLE stat_current_top100(player TEXT,rank INTEGER,points INTEGER);
            INSERT INTO stat_current_top100 VALUES('chatLDS ',1,10);
            CREATE TABLE stat_classification_state(singleton INTEGER,revision INTEGER);
            INSERT INTO stat_classification_state VALUES(1,1);''')
        self.portal = object.__new__(StatisticsPortal)
        self.portal.database = self.path
        self.portal._query_cache = {}
        self.portal._query_cache_lock = threading.Lock()
        self.portal._query_compute_lock = threading.RLock()
        naming._LIST_CACHE.clear()
        self.addCleanup(naming._LIST_CACHE.clear)

    def request(self, callback):
        return self.portal._cached_query_reply(('roster',),callback,ttl=300,stamp_seconds=86400)

    def test_wal_rename_invalidates_ranking_but_unchanged_request_hits(self):
        calls = []
        def read(db):
            calls.append(1)
            return db.execute('SELECT label FROM value').fetchone()[0]
        self.assertIsNone(json.loads(self.request(read)[2]))
        self.request(read)
        self.assertEqual(len(calls),1)
        main_time = self.path.stat().st_mtime_ns
        self.db.execute("UPDATE value SET label='recognized'"); self.db.commit()
        self.assertEqual(main_time,self.path.stat().st_mtime_ns)
        self.assertEqual(json.loads(self.request(read)[2]),'recognized')
        self.assertEqual(len(calls),2)

    def test_membership_update_invalidates_ranking(self):
        def read(db):
            return [r[0] for r in db.execute('SELECT player FROM stat_current_top100')]
        self.assertEqual(json.loads(self.request(read)[2]),['chatLDS '])
        self.db.execute("UPDATE stat_current_top100 SET player='new player'"); self.db.commit()
        self.assertEqual(json.loads(self.request(read)[2]),['new player'])

    def test_sidecar_changes_invalidate_both_caches(self):
        count = []
        def read(db):
            count.append(1)
            return len(count)
        with patch.object(naming,'_latest_views',return_value={}):
            previous = naming.list_rows(self.db,1000,'100')
            stamp = snapshot_version(self.path)
            self.request(read)
            for filename in ('first-game-confirmations.sqlite3-wal',
                             'watch-upload-sessions.sqlite3-wal','series-naming.json'):
                (self.path.parent / filename).write_bytes(b'changed')
                self.assertNotEqual(stamp,snapshot_version(self.path))
                current = naming.list_rows(self.db,1000,'100')
                self.assertIsNot(previous,current)
                self.request(read)
                previous,stamp = current,snapshot_version(self.path)
        self.assertEqual(len(count),4)

    def test_change_during_computation_is_not_reused_as_current(self):
        def read(db):
            label = db.execute('SELECT label FROM value').fetchone()[0]
            if label is None:
                self.db.execute("UPDATE value SET label='new'"); self.db.commit()
            return label
        self.assertIsNone(json.loads(self.request(read)[2]))
        self.assertEqual(json.loads(self.request(read)[2]),'new')

    def test_both_pages_share_trimmed_player_and_build(self):
        row = dict(player='chatLDS ',digest='d',build='b',recorded_at=1,expires_at=9999,
                   deck_type='recognized',classification_status='recognized',series=[],series_status='known')
        basis = dict(views=[row],roster=dict(members=[dict(player='chatLDS ',rank=1,points=10)]))
        with patch.object(dashboard,'eligible_views',return_value=basis), \
                patch.object(dashboard,'identity_metadata',return_value={}), \
                patch('bot.top100.eligible_views',return_value=basis):
            _,selected,people = dashboard.population(self.db,self.path.parent,1000,resolve_rank=False)
            named = naming.list_rows(self.db,1000,'100',replay_root=self.path.parent)
        self.assertEqual(set(selected),{'chatLDS'})
        self.assertEqual(people[0]['player'],named['rows'][0]['player'])
        self.assertEqual(people[0]['deck_type'],named['rows'][0]['deck_type'])
        self.assertEqual(named['generated_at'],1000)
