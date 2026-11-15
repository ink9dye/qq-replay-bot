import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bot.card_usage_pie import collect, _selected_sessions


def view(build, session, game, stamp):
    return dict(player='p', build=build, digest=build, observed_session_id=session,
                observed_match_players=['p', 'opponent'], observed_game_no=game,
                observed_identity_confirmed=True, recorded_at=stamp)


class SelectedMatchTests(unittest.TestCase):
    def collect_rows(self, views, selected):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'bot.sqlite3'
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE stat_build_cards(build TEXT, partition TEXT, code INTEGER, copies INTEGER)')
                db.executemany('INSERT INTO stat_build_cards VALUES(?,?,?,?)',
                               [('old1', 'main', 123, 1), ('old2', 'main', 123, 2),
                                ('new2', 'main', 123, 3)])
                db.commit()
            with patch('bot.statistics_dashboard.population', return_value=(
                    dict(views=views), selected, [dict(player='p', rank=1), dict(player='missing', rank=2)])), \
                 patch('bot.card_usage_pie.resolve_card', return_value=(123, 'card')), \
                 patch('bot.card_usage_pie.identity_codes', return_value=[123]):
                return collect(database, folder, 100, 'card')

    def test_latest_incomplete_match_does_not_drop_valid_player_or_mix_side(self):
        first = view('old1', 'old', 1, 1)
        payload = self.collect_rows([first, view('old2', 'old', 2, 2),
                                     view('new2', 'new', 2, 3)], {'p': first})
        self.assertEqual((payload['current'], payload['total_players'], payload['excluded']), (1, 2, 1))
        self.assertEqual(payload['main_dist'][1], 1)
        self.assertEqual(payload['side_dist'][1], 1)
        self.assertEqual(payload['total_dist'][2], 1)

    def test_duplicate_first_records_follow_shared_selected_build(self):
        first = view('old1', 'old', 1, 1)
        payload = self.collect_rows([first, view('old2', 'old', 1, 2)], {'p': first})
        self.assertEqual(payload['current'], 1)
        self.assertEqual(payload['main_dist'][1], 1)

    def test_no_confirmed_first_stays_excluded(self):
        payload = self.collect_rows([view('new2', 'new', 2, 3)], {})
        self.assertEqual((payload['current'], payload['excluded']), (0, 2))
        self.assertEqual(sum(payload['main_dist'].values()), 0)

    def test_digest_fallback_does_not_mix_unlinked_games(self):
        first = view('old1', None, 1, 1)
        other = view('old2', None, 2, 2)
        self.assertEqual(_selected_sessions([first, other], {'p': first}), {'p': [first]})


if __name__ == '__main__':
    unittest.main()
