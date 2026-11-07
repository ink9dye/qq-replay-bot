import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot import statistics_quality as Q
import test_statistics_facts as F

NOW = F.NOW
SCHEMA = '''
CREATE TABLE stat_games(digest TEXT PRIMARY KEY, recorded_at REAL, received_at REAL,
                        expires_at REAL, report_version INTEGER);
CREATE TABLE stat_views(digest TEXT NOT NULL, player TEXT NOT NULL, player_index INTEGER,
                        build TEXT, outcome TEXT, result_source TEXT, first_player INTEGER,
                        usage_complete INTEGER, draw_complete INTEGER, report_version INTEGER);
CREATE TABLE stat_evidence(digest TEXT, kind TEXT, source TEXT, payload_json TEXT);
CREATE TABLE stat_files(replay_id INTEGER, digest TEXT);
CREATE TABLE replay_sessions(replay_id INTEGER PRIMARY KEY, session_id TEXT, game_no INTEGER);
'''


def add_game(db, digest, outcomes, capture=None):
    db.execute('INSERT INTO stat_games VALUES(?,?,?,?,?)', (digest, NOW, NOW, NOW + 86400, 4))
    for index, outcome in enumerate(outcomes):
        db.execute('INSERT INTO stat_views VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (digest, '甲' if index == 0 else '乙', index, 'b' + digest,
                    outcome, None, 1, 1, 1, 4))
    if capture is not None:
        db.execute('INSERT INTO stat_evidence VALUES(?,?,?,?)',
                   (digest, 'capture', 'test', json.dumps({'capture': capture})))


def capture(session, number):
    return {'session_id': session, 'game_no': number, 'captured_from_start': True}


def roster(names=()):
    names = list(names) + ['玩家' + str(i) for i in range(100 - len(names))]
    return [dict(username=name, pt=2000 - i, id=None) for i, name in enumerate(names)]


class QualityComputeTests(unittest.TestCase):
    def setUp(self):
        Q.enable(False)
        Q._CACHE.update(key=None, at=0.0, excluded=frozenset(), stats={})
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA)

    def build(self):
        add_game(self.db, 'd1', ['win', 'loss'], capture('s1', 1))
        add_game(self.db, 'd2', ['loss', 'win'], capture('s1', 2))
        add_game(self.db, 'd3', ['unknown', 'unknown'], capture('s1', 3))
        add_game(self.db, 'd4', ['unknown', 'unknown'])
        add_game(self.db, 'd5', ['win', 'loss'])
        add_game(self.db, 'd6', ['loss', 'win'], capture('s2', 1))
        add_game(self.db, 'd7', ['unknown', 'unknown'], capture(None, 2))
        self.db.commit()

    def test_match_unknown_siblings_and_standalone_are_excluded(self):
        self.build()
        excluded, counters = Q.compute(self.db)
        self.assertEqual(excluded, frozenset({'d1', 'd2', 'd3', 'd4', 'd5', 'd7'}))
        self.assertEqual(counters['bad_sessions'], 1)
        self.assertEqual(counters['match_digests'], 3)
        self.assertEqual(counters['unknown_game_number_digests'], 2)
        self.assertEqual(counters['standalone_unknown_digests'], 1)
        self.assertEqual(counters['excluded_digests'], 6)

    def test_cache_picks_up_new_unknown_game(self):
        self.build()
        Q.enable(True)
        self.assertEqual(len(Q.excluded_digests(self.db)), 6)
        add_game(self.db, 'd8', ['unknown', 'unknown'])
        self.db.commit()
        self.assertIn('d8', Q.excluded_digests(self.db))
        Q.enable(False)

    def test_disabled_filter_returns_nothing(self):
        self.build()
        self.assertEqual(Q.excluded_digests(self.db), frozenset())

    def test_identity_failure_fails_open_and_is_not_cached(self):
        self.build()
        Q.enable(True)
        with mock.patch.object(Q, 'compute', side_effect=sqlite3.OperationalError('locked')) as compute:
            self.assertEqual(Q.excluded_digests(self.db), frozenset())
            self.assertEqual(Q.excluded_digests(self.db), frozenset())
        self.assertEqual(compute.call_count, 2)
        Q.enable(False)


class QualityEligibleViewsTests(unittest.TestCase):
    def setUp(self):
        Q.enable(False)
        Q._CACHE.update(key=None, at=0.0, excluded=frozenset(), stats={})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.replays = self.root / 'replays'
        self.replays.mkdir()
        from bot.store import Store
        self.store = Store(self.root / 'db.sqlite3')
        self.addCleanup(self.store.close)
        self.row, self.path = F.StatisticsFactsTests.source(self)
        self.store.import_replay_facts(self.row, self.path, None, NOW)
        self.store.replace_top100(roster(['甲', '乙']), NOW)

    def tearDown(self):
        Q.enable(False)
        Q._CACHE.update(key=None, at=0.0, excluded=frozenset(), stats={})

    def views(self):
        return self.store.current_top100_views(self.replays, NOW, False)['views']

    def test_eligible_views_applies_quality_filter(self):
        Q.enable(True)
        with mock.patch.object(Q, 'excluded_digests', return_value=frozenset()):
            self.assertEqual(len(self.views()), 2)
        with mock.patch.object(Q, 'excluded_digests', return_value=frozenset({self.row['digest']})):
            basis = self.store.current_top100_views(self.replays, NOW, False)
        self.assertEqual(basis['views'], [])
        self.assertEqual(basis['diagnostics']['quality_filter']['excluded_views'], 2)


if __name__ == '__main__':
    unittest.main()
