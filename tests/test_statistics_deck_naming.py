# -*- coding: utf-8 -*-
"""卡组命名页列表：缓存必须跟着数据库版本走，改名后 5 分钟内也要立刻可见。"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bot import statistics_deck_naming as naming


def view(deck):
    return dict(player='甲', build='b1', digest='d1', recorded_at=100.0,
                deck_type=deck, classification_status='recognized',
                classification_source='auto_series')


class NamingListCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'bot.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE stat_classification_state(singleton INTEGER, revision INTEGER)')
            db.execute('INSERT INTO stat_classification_state VALUES(1,1)')
            db.commit()
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        naming._LIST_CACHE.clear()
        self.addCleanup(naming._LIST_CACHE.clear)

    def bump(self):
        self.db.execute('UPDATE stat_classification_state SET revision=revision+1')
        self.db.commit()

    def test_rename_is_visible_without_waiting_for_the_ttl(self):
        roster = {'甲': dict(points=1, rank=1)}
        with patch.object(naming, '_roster', return_value=roster), \
                patch.object(naming, '_latest_views', return_value={'甲': view('旧名字')}):
            first = naming.list_rows(self.db, 1000.0, '100')
        self.assertEqual(first['rows'][0]['deck_type'], '旧名字')
        self.bump()
        with patch.object(naming, '_roster', return_value=roster), \
                patch.object(naming, '_latest_views', return_value={'甲': view('新名字')}):
            second = naming.list_rows(self.db, 1000.0, '100')
        self.assertEqual(second['rows'][0]['deck_type'], '新名字')

    def test_same_version_still_uses_the_cache(self):
        roster = {'甲': dict(points=1, rank=1)}
        with patch.object(naming, '_roster', return_value=roster), \
                patch.object(naming, '_latest_views', return_value={'甲': view('旧名字')}) as fetch:
            naming.list_rows(self.db, 1000.0, '100')
            naming.list_rows(self.db, 1000.0, '100')
        self.assertEqual(fetch.call_count, 1)

    def test_top100_list_uses_same_replay_context_as_rankings(self):
        roster = {'甲': dict(points=1, rank=1)}
        replay_root = Path(self.tmp.name) / 'replays'
        with patch.object(naming, '_roster', return_value=roster), \
                patch.object(naming, '_latest_views', return_value={'甲': view(None)}) as fetch:
            result = naming.list_rows(self.db, 1000.0, '100', replay_root=replay_root)
        self.assertEqual(fetch.call_args.args[2], replay_root)
        self.assertEqual(result['recognized'], 0)

    def test_shared_latest_selection_keeps_unknown_first_game_over_named_side_game(self):
        replay_root = Path(self.tmp.name) / 'replays'
        first = dict(view(None), player='甲', digest='first', recorded_at=100,
                     observed_session_id='same', observed_match_players=('甲','乙'), observed_game_no=1)
        side = dict(view('不该当主卡名'), player='甲', digest='side', recorded_at=200,
                    observed_session_id='same', observed_match_players=('甲','乙'), observed_game_no=2)
        with patch.object(naming, '_roster', return_value={'甲': {}}), \
                patch('bot.top100.eligible_views', return_value={'views':[first,side]}) as eligible:
            result = naming._latest_views(self.db, 1000, replay_root, only={'甲'})
        self.assertIsNone(result['甲']['deck_type'])
        self.assertEqual(result['甲']['digest'], 'first')
        self.assertFalse(eligible.call_args.kwargs['check_files'])
        self.assertFalse(eligible.call_args.kwargs['annotate_series'])


class NamingSnapshotTests(unittest.TestCase):
    """后台快照必须与“每次请求重算”的列表/详情完全一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'bot.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                CREATE TABLE stat_classification_state(singleton INTEGER, revision INTEGER);
                INSERT INTO stat_classification_state VALUES(1,1);
                CREATE TABLE stat_current_top100(player TEXT,rank INTEGER,points INTEGER);
                INSERT INTO stat_current_top100 VALUES('甲',1,10);
                INSERT INTO stat_current_top100 VALUES('乙',2,9);
                CREATE TABLE stat_build_cards(build TEXT,partition TEXT,code INTEGER,copies INTEGER);
                INSERT INTO stat_build_cards VALUES('b1','main',100,3);
                CREATE TABLE stat_build_series(build TEXT,series TEXT);
                INSERT INTO stat_build_series VALUES('b1','烙印');''')
            db.commit()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        naming._LIST_CACHE.clear()
        self.addCleanup(naming._LIST_CACHE.clear)
        self.replay_root = Path(self.tmp.name) / 'replays'
        self.roster = {'甲': dict(rank=1, points=10), '乙': dict(rank=2, points=9)}
        self.roster_views = {'甲': view('烙印')}
        self.all_views = {'甲': view('旧名字'), '乙': dict(view(None), player='乙', build='b2', digest='d2'),
                          '丙': dict(view('珠泪'), player='丙', build='b3', digest='d3')}
        self.unnamed = {'乙': dict(view(None), player='乙', build='b2', digest='d2')}
        self.patches = [
            patch.object(naming, '_roster', return_value=self.roster),
            patch.object(naming, '_latest_views',
                         side_effect=lambda db, now, root=None, only=None:
                         self.roster_views if only else self.all_views),
            patch.object(naming, '_valid_build_views', return_value=self.unnamed),
            patch.object(naming, '_outside_ranks', return_value={'丙': 7}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def snapshot(self):
        return naming.snapshot(self.db, self.tmp.name, 1000.0, self.replay_root)

    def test_list_rows_reads_snapshot_without_recomputing(self):
        stats = self.snapshot()
        for scope, kwargs in (('100', {}), ('200', {}), ('all', {}), ('unnamed', {}),
                              ('100', {'query': '甲'}), ('100', {'deck_query': '烙'}),
                              ('200', {'query': '丙'})):
            naming._LIST_CACHE.clear()
            direct = naming.list_rows(self.db, 1000.0, scope, replay_root=self.replay_root, **kwargs)
            naming._LIST_CACHE.clear()
            from_snapshot = naming.list_rows(None, 1000.0, scope, replay_root=self.replay_root,
                                             stats=stats, **kwargs)
            self.assertEqual(direct, from_snapshot, (scope, kwargs))
        self.assertEqual([row['player'] for row in from_snapshot['rows']], ['丙'])

    def test_snapshot_keeps_top100_and_full_population_apart(self):
        stats = self.snapshot()
        self.assertEqual(set(stats['roster_views']), {'甲'})
        self.assertEqual(stats['roster_views']['甲']['deck_type'], '烙印')
        self.assertEqual(stats['all_views']['甲']['deck_type'], '旧名字')
        self.assertEqual(set(stats['unnamed']), {'乙'})
        self.assertEqual(stats['outside_ranks'], {'丙': 7})

    def test_detail_reads_snapshot_without_recomputing(self):
        stats = self.snapshot()
        data = self.tmp.name
        with patch.object(naming, '_names', return_value={}), \
                patch('bot.deck_sort.load_card_data', return_value={}):
            direct = naming.detail(self.db, data, 1000.0, '甲', self.replay_root)
            from_snapshot = naming.detail(self.db, data, 1000.0, '甲', self.replay_root, stats=stats)
            outside = naming.detail(self.db, data, 1000.0, '丙', self.replay_root, stats=stats)
        self.assertEqual(direct, from_snapshot)
        self.assertEqual(direct['deck_type'], '烙印')
        self.assertEqual([card['copies'] for card in direct['cards']['main']], [3])
        self.assertEqual(outside['rank'], 7)
        self.assertEqual(outside['deck_type'], '珠泪')

    def test_roster_player_outside_first_game_is_not_revived(self):
        """榜内玩家若在前100 口径里没有视角，详情不能改读全量视角。"""
        self.roster_views = {}
        stats = self.snapshot()
        with patch.object(naming, '_names', return_value={}), \
                patch('bot.deck_sort.load_card_data', return_value={}):
            result = naming.detail(self.db, self.tmp.name, 1000.0, '甲', self.replay_root, stats=stats)
        self.assertFalse(result['observed'])
        self.assertEqual(result['cards'], {'main': [], 'extra': [], 'side': []})


if __name__ == '__main__':
    unittest.main()
