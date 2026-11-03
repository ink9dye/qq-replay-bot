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


if __name__ == '__main__':
    unittest.main()
