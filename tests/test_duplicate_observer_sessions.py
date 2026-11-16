import json
import sqlite3
import unittest
from bot.statistics_match_identity import capture_metadata, inherit_first_game_types
from bot.statistics_population import latest_views


class DuplicateObserverSessionsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE stat_evidence(digest,payload_json); '
                             'CREATE TABLE stat_views(digest,player); '
                             'CREATE TABLE stat_files(digest,replay_id); '
                             'CREATE TABLE replay_sessions(replay_id,session_id,game_no);')
        self.a, self.b, self.c = 'a'*64, 'b'*64, 'c'*64

    def add(self, digest, session, number, previous=None, pair=('A', 'B'), full=True):
        capture = dict(session_id=session, game_no=number, captured_from_start=full)
        if previous is not None:
            capture['previous_game'] = dict(digest=previous, game_no=number-1)
        self.db.execute('INSERT INTO stat_evidence VALUES(?,?)',
                        (digest, json.dumps(dict(capture=capture))))
        for p in pair:
            if not self.db.execute('SELECT 1 FROM stat_views WHERE digest=? AND player=?',
                                   (digest,p)).fetchone():
                self.db.execute('INSERT INTO stat_views VALUES(?,?)', (digest,p))

    def test_same_replay_two_observer_sessions_is_one_first_game(self):
        self.add(self.a, 'recorder-z', 1)
        self.add(self.a, 'recorder-a', 1)
        self.assertEqual(capture_metadata(self.db, [self.a])[self.a], ('recorder-a', 1, True))

    def test_split_session_chain_resolves_even_for_single_requested_later_game(self):
        self.add(self.a, 'first-recorder', 1)
        self.add(self.b, 'other-recorder', 2, self.a)
        self.add(self.c, 'third-recorder', 3, self.b)
        one = capture_metadata(self.db, [self.c])
        all_games = capture_metadata(self.db, [self.a, self.b, self.c])
        self.assertEqual(one[self.c], ('first-recorder', 3, True))
        self.assertEqual(one[self.c], all_games[self.c])

    def test_ambiguous_first_and_later_games_restore_same_match_selection(self):
        self.add(self.a, 'z', 1)
        self.add(self.a, 'a', 1)
        self.add(self.b, 'z', 2, self.a)
        self.add(self.c, 'a', 3, self.b)
        rows = [dict(digest=d,player='A',deck_type='first' if n == 1 else 'side',
                     recorded_at=n,build=str(n))
                for n,d in enumerate([self.a,self.b,self.c],1)]
        inherit_first_game_types(self.db, rows)
        self.assertEqual([r['observed_session_id'] for r in rows], ['a']*3)
        self.assertEqual(rows[2]['classification_source_digest'], self.a)
        self.assertEqual(latest_views({'views':rows})['A']['digest'], self.a)

    def test_conflicting_numbers_remain_unresolved(self):
        self.add(self.a, 'x', 1)
        self.add(self.a, 'y', 2)
        self.assertNotIn(self.a, capture_metadata(self.db,[self.a]))

    def test_incomplete_duplicate_does_not_become_complete(self):
        self.add(self.a, 'x', 1)
        self.add(self.a, 'y', 1, full=False)
        self.assertNotIn(self.a, capture_metadata(self.db,[self.a]))

    def test_different_opponents_do_not_join(self):
        self.add(self.a, 'x', 1)
        self.add(self.b, 'y', 2, self.a, pair=('A','C'))
        self.assertEqual(capture_metadata(self.db,[self.a,self.b])[self.b][0], 'y')

    def test_missing_predecessor_does_not_guess(self):
        self.add(self.b, 'y', 2, self.a)
        self.assertEqual(capture_metadata(self.db,[self.b])[self.b], ('y', 2, True))

    def test_multiple_predecessors_do_not_join(self):
        self.add(self.a, 'x', 1)
        self.add(self.c, 'z', 1)
        self.add(self.b, 'y', 2, self.a)
        self.add(self.b, 'y', 2, self.c)
        self.assertEqual(capture_metadata(self.db,[self.b])[self.b][0], 'y')

    def test_unrelated_sessions_keep_existing_identity(self):
        self.add(self.a, 'x', 1)
        self.add(self.b, 'y', 1)
        result=capture_metadata(self.db,[self.a,self.b])
        self.assertEqual(result[self.a],('x',1,True))
        self.assertEqual(result[self.b],('y',1,True))


if __name__ == '__main__':
    unittest.main()
