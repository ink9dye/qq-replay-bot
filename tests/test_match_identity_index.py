import json
import sqlite3
import unittest

from bot.statistics_match_identity import _read_capture_metadata, capture_metadata
from bot.statistics_match_index import initialize, lookup, refresh


class MatchIdentityIndexTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(
            'CREATE TABLE stat_games(digest TEXT PRIMARY KEY); '
            'CREATE TABLE stat_evidence(digest,payload_json); '
            'CREATE TABLE stat_views(digest,player); '
            'CREATE TABLE stat_files(digest,replay_id); '
            'CREATE TABLE replay_sessions(replay_id,session_id,game_no);')
        initialize(self.db)
        self.a, self.b, self.c = 'a' * 64, 'b' * 64, 'c' * 64

    def add(self, digest, session, number, previous=None, pair=('A', 'B'), full=True):
        capture = dict(session_id=session, game_no=number, captured_from_start=full)
        if previous is not None:
            capture['previous_game'] = dict(digest=previous, game_no=number - 1)
        self.db.execute('INSERT OR IGNORE INTO stat_games VALUES(?)', (digest,))
        self.db.execute('INSERT INTO stat_evidence VALUES(?,?)',
                        (digest, json.dumps(dict(capture=capture))))
        for player in pair:
            self.db.execute('INSERT INTO stat_views VALUES(?,?)', (digest, player))

    def assert_same_as_fresh(self, digests):
        stored = capture_metadata(self.db, digests)
        fresh = _read_capture_metadata(self.db, set(digests))
        self.assertEqual(stored, fresh)

    def test_unindexed_database_still_reads_evidence(self):
        self.add(self.a, 'recorder', 1)
        self.assertEqual(capture_metadata(self.db, [self.a])[self.a], ('recorder', 1, True))
        # 查询路径不写库：没回填就继续走旧路径，行为与改动前一致。
        self.assertEqual(lookup(self.db, [self.a]), ({}, set()))

    def test_indexed_value_matches_evidence(self):
        self.add(self.a, 'recorder', 1)
        self.add(self.b, 'other', 2, self.a)
        refresh(self.db, [self.a, self.b])
        found, indexed = lookup(self.db, [self.a, self.b])
        self.assertEqual(indexed, {self.a, self.b})
        self.assertEqual(found[self.a], ('recorder', 1, True))
        self.assertEqual(found[self.b], ('recorder', 2, True))
        self.assert_same_as_fresh([self.a, self.b])

    def test_duplicate_observers_unify_to_one_session(self):
        self.add(self.a, 'recorder-z', 1)
        self.add(self.a, 'recorder-a', 1)
        refresh(self.db, [self.a])
        self.assertEqual(capture_metadata(self.db, [self.a])[self.a], ('recorder-a', 1, True))

    def test_conflicting_numbers_are_recorded_as_absent(self):
        self.add(self.a, 'x', 1)
        self.add(self.a, 'y', 2)
        refresh(self.db, [self.a])
        self.assertNotIn(self.a, capture_metadata(self.db, [self.a]))
        _, indexed = lookup(self.db, [self.a])
        # 明确“算过但没有可靠身份”，避免每次查询重复推导。
        self.assertEqual(indexed, {self.a})

    def test_late_parent_refreshes_children(self):
        self.add(self.b, 'child-recorder', 2, self.a)
        refresh(self.db, [self.b])
        # 首局还没入库：后一局只能保留自己观察到的场次。
        self.assertEqual(capture_metadata(self.db, [self.b])[self.b], ('child-recorder', 2, True))
        self.add(self.a, 'first-recorder', 1)
        refresh(self.db, [self.a])
        self.assertEqual(capture_metadata(self.db, [self.b])[self.b], ('first-recorder', 2, True))
        self.assert_same_as_fresh([self.a, self.b])

    def test_purged_game_drops_identity_row(self):
        self.add(self.a, 'recorder', 1)
        refresh(self.db, [self.a])
        self.db.execute('DELETE FROM stat_games WHERE digest=?', (self.a,))
        self.assertEqual(lookup(self.db, [self.a]), ({}, set()))


if __name__ == '__main__':
    unittest.main()


class IngestWritesIdentityTests(unittest.TestCase):
    """入库即写索引：导入、证据变更、查询三条路径要看到同一份身份。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from bot.store import Store
        from test_deck_links import pack_replay
        self.pack = pack_replay
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'db.sqlite3')
        self.addCleanup(self.store.close)
        self.replays = self.root / 'replays'
        self.replays.mkdir()
        self.now = 1800000000.0

    def source(self):
        import hashlib
        import os
        blob = self.pack(['甲', '乙'], [([1, 1, 2], [9]), ([3, 4], [8])])
        digest = hashlib.sha256(blob).hexdigest()
        path = self.replays / '甲' / (digest + '.yrp')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(blob)
        os.utime(path, (self.now - 100, self.now - 100))
        row, _ = self.store.add_replay(digest, '甲', str(path.relative_to(self.replays)),
                                       ['甲', '乙'], {}, self.now - 200, saved_at=self.now - 100)
        return row, path

    def report(self, row, version=4):
        return dict(version=version, digest=row['digest'], playback='completed',
                    winner=0, result_source='live_server', first_player=1,
                    players=[dict(name='甲', main=[1, 1, 2], extra=[9], used_cards=[],
                                  extra_appearances=[], draw_record_complete=True,
                                  initial_draws_accounted=True, drawn_cards=[]),
                             dict(name='乙', main=[3, 4], extra=[8], used_cards=[],
                                  extra_appearances=[], draw_record_complete=True,
                                  initial_draws_accounted=True, drawn_cards=[])])

    def row(self, digest):
        return self.store._db.execute('SELECT * FROM stat_match_identity WHERE digest=?',
                                      (digest,)).fetchone()

    def test_import_indexes_identity_and_query_reads_it(self):
        from bot.statistics_match_identity import capture_metadata
        row, path = self.source()
        report = self.report(row)
        report['evidence'] = dict(digest=row['digest'], source='live_server', game_no=1,
                                  session_id='match-one', captured_from_start=True)
        self.store.import_replay_facts(row, path, report, self.now)
        stored = self.row(row['digest'])
        self.assertEqual((stored['session_id'], stored['game_no'], stored['resolved']),
                         ('match-one', 1, 1))
        self.assertEqual(capture_metadata(self.store._db, [row['digest']])[row['digest']],
                         ('match-one', 1, True))

    def test_new_evidence_invalidates_then_reindexes(self):
        from bot.statistics_match_identity import capture_metadata
        row, path = self.source()
        report = self.report(row)
        report['evidence'] = dict(digest=row['digest'], source='live_server', game_no=1,
                                  session_id='match-one', captured_from_start=True)
        self.store.import_replay_facts(row, path, report, self.now)
        # 第二份证据给出不同局次：无法唯一确定，索引必须作废而不是沿用旧局次。
        report2 = self.report(row, 5)
        report2['evidence'] = dict(digest=row['digest'], source='live_server', game_no=2,
                                   session_id='match-two', captured_from_start=True)
        self.store.import_replay_facts(row, path, report2, self.now)
        stored = self.row(row['digest'])
        self.assertEqual(stored['resolved'], 0)
        self.assertNotIn(row['digest'], capture_metadata(self.store._db, [row['digest']]))
