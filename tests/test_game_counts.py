import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
from bot.commands import parse_command
from bot.game_counts import GameCounter

class GameCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.db=self.root/'bot.sqlite3';self.now=datetime(2026,11,3,1,tzinfo=timezone(timedelta(hours=8))).timestamp();self.midnight=self.now-3600
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript('CREATE TABLE replays(id INTEGER PRIMARY KEY,digest TEXT,players_json TEXT,recorded_at REAL,saved_at REAL);CREATE TABLE stat_views(digest TEXT,player TEXT,build TEXT);CREATE TABLE stat_builds(fingerprint TEXT PRIMARY KEY,deck_type TEXT);')
            rows=[(1,'g1','["A","B"]',self.now-7200,self.midnight-1),
                  (2,'g1','["A","B"]',self.now-7200,self.midnight+1),
                  (3,'g2','["A","C"]',self.now-600,self.midnight+5),
                  (4,'g3','["C","D"]',self.now-300,self.midnight+10),
                  (5,'old','["A","B"]',self.now-29*86400,self.midnight+10)]
            db.executemany('INSERT INTO replays VALUES(?,?,?,?,?)',rows)
            db.executemany('INSERT INTO stat_builds VALUES(?,?)',[('b1','xxyy'),('b2','xxzz'),('b3','other')])
            db.executemany('INSERT INTO stat_views VALUES(?,?,?)',[('g1','A','b1'),('g1','B','b2'),('g2','A','b2'),('g3','C','b3')])
            db.commit()
        self.counter=GameCounter(self.db,self.root,clock=lambda:self.now)

    def test_parse_total_deck_participation_and_internal(self):
        self.assertEqual(parse_command('统计对局'),dict(op='game_counts',keyword=''))
        self.assertEqual(parse_command('统计对局 xx'),dict(op='game_counts',keyword='xx'))
        self.assertEqual(parse_command('统计对局 前200 内战'),dict(op='game_counts',limit=200,internal=True))
        self.assertEqual(parse_command('统计对局 前200'),dict(op='game_counts',limit=200,internal=False))

    def test_unique_games_today_uses_first_ingest_not_duplicate_copy(self):
        r=self.counter.query();self.assertEqual((r['count'],r['today']),(3,2))

    def test_literal_substring_counts_game_once_even_when_both_decks_match(self):
        r=self.counter.query('xx');self.assertEqual((r['count'],r['today']),(2,1))
        self.assertEqual(self.counter.query('%')['count'],0)

    def test_inner_counts_use_shared_views_and_first_ingest(self):
        from bot import statistics_rank_scope as scope
        views = [dict(digest='g1', player='A', observed_game_no=1),
                 dict(digest='g1', player='B', observed_game_no=1),
                 dict(digest='g2', player='A', observed_game_no=1),
                 dict(digest='g3', player='C', observed_game_no=1),
                 dict(digest='old', player='C', observed_game_no=None)]
        pairs = {'g1': {'A', 'B'}, 'g2': {'A', 'Other'},
                 'g3': {'C', 'Other'}, 'old': {'C', 'Other'}}
        roster = {'members': [dict(player='A', rank=1)]}

        def eligible(db, replay_root, now, **options):
            self.assertEqual(replay_root, self.root / 'replays')
            self.assertFalse(options['with_diagnostics'])
            return {'roster': roster, 'views': scope.filter_rows(db, views, roster, now, pairs=pairs)}

        with patch('bot.top100.eligible_views', side_effect=eligible) as load, \
             patch.object(scope, '_stored_ranks', return_value={}), \
             patch.object(scope, 'ranks_for', return_value={'B': 200, 'C': 201}):
            result = self.counter.query(limit=200, internal=True)
            self.counter.query(limit=200, internal=True)
        load.assert_called_once()
        self.assertEqual((result['count'], result['views'], result['today'], result['unknown']),
                         (1, 2, 0, 1))

    def test_cache_does_not_requery_database_and_is_bounded(self):
        result=self.counter.query()
        with patch('bot.game_counts.sqlite3.connect',side_effect=AssertionError('repeated database query')):
            self.assertIs(self.counter.query(),result)
        for i in range(20):self.counter.query(str(i))
        self.assertLessEqual(len(self.counter._cache),16)

    def test_card_report_and_qq_agree_under_the_same_rank_scope(self):
        from bot.statistics_card_report import query
        from bot.statistics_rank_scope import population_scope
        from test_card_statistics import CardStatisticsTests, NOW

        fixture = CardStatisticsTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        counter = GameCounter(fixture.store.path, fixture.root, clock=lambda: NOW)
        with patch('bot.statistics_rank_scope.ranks_for', return_value={'乙': 200}), \
             patch('bot.statistics_rank_scope._stored_ranks', return_value={'乙': (200, NOW)}):
            result = counter.query(limit=200, internal=True)
            with population_scope('200', '200'):
                report = query(fixture.store._db, fixture.replays, NOW, metric='opening')
        self.assertEqual((report['selected_games'], report['selected_samples']), (5, 10))
        self.assertEqual((result['count'], result['views']), (5, 10))

    def test_top100_uses_official_roster_and_agrees_with_card_report(self):
        from bot.statistics_card_report import query
        from bot.statistics_rank_scope import population_scope
        from test_card_statistics import CardStatisticsTests, NOW
        from test_top100 import roster

        fixture = CardStatisticsTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.store.replace_top100(roster(['甲', '乙']), NOW)
        counter = GameCounter(fixture.store.path, fixture.root, clock=lambda: NOW)
        with patch('bot.statistics_rank_scope.ranks_for', side_effect=AssertionError('rank lookup')):
            result = counter.query(limit=100, internal=True)
            with population_scope('100', '100'):
                report = query(fixture.store._db, fixture.replays, NOW, metric='opening')
        self.assertEqual((report['selected_games'], report['selected_samples']), (5, 10))
        self.assertEqual((result['count'], result['views']), (5, 10))
        self.assertEqual(result['unknown'], 0)

    def test_top100_without_roster_does_not_report_zero(self):
        with patch('bot.top100.eligible_views', return_value={'roster': {'available': False}, 'views': []}):
            with self.assertRaisesRegex(ValueError, 'roster unavailable'):
                self.counter.query(limit=100, internal=True)

    def test_ranked_participation_matches_web_own_range_and_dedupes(self):
        from bot.statistics_card_report import query
        from bot.statistics_rank_scope import population_scope
        from test_card_statistics import CardStatisticsTests, NOW

        fixture = CardStatisticsTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        counter = GameCounter(fixture.store.path, fixture.root, clock=lambda: NOW)
        for limit, ranks, expected in ((100, {}, (5, 5)), (200, {'乙': 200}, (5, 10)),
                                       (200, {}, (5, 5))):
            counter._cache.clear()
            with self.subTest(limit=limit, ranks=ranks), \
                 patch('bot.statistics_rank_scope.ranks_for', return_value=ranks), \
                 patch('bot.statistics_rank_scope._stored_ranks', return_value={}):
                result = counter.query(limit=limit)
                with population_scope(str(limit), 'all'):
                    report = query(fixture.store._db, fixture.replays, NOW, metric='opening')
                self.assertEqual((report['selected_games'], report['selected_samples']), expected)
                self.assertEqual((result['count'], result['views']), expected)
                self.assertEqual(result['unknown'], 0)

    def test_count_command_uses_same_visibility_check_as_statistics(self):
        from test_bot import temporary_service
        with temporary_service() as service:
            service.config['statistics']={'private_users':['10000'],'allowed_groups':['12345']}
            with patch('bot.game_counts.message',return_value='counts') as count:
                self.assertEqual(service.handle_text('10000','统计对局'),'counts')
                self.assertIsNone(service.handle_text('10001','统计对局'))
                self.assertEqual(service.handle_text('10001','统计对局 前200','group:12345'),'counts')
                self.assertIsNone(service.handle_text('10000','统计对局','group:99999'))
                self.assertNotIn('统计对局：',service.handle_text('10001','帮助'))
                self.assertEqual(count.call_count,2)
