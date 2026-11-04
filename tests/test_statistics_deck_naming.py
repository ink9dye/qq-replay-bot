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


if __name__ == '__main__':
    unittest.main()
