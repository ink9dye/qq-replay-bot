# -*- coding: utf-8 -*-
"""前200 人口筛选：快照/现查名次能进样本，未知名次按不合格处理。

2026-11-07 线上 500 的回归用例：filter_rows 里用局部变量 shadow 了模块函数
current()，导致 scope=current() 抛 UnboundLocalError。这里直接调用 filter_rows，
能挡住同类改坏。
"""
import sqlite3
import time
import unittest

from bot import statistics_rank_scope as scope


class FilterRowsPopulationTest(unittest.TestCase):
    def setUp(self):
        scope._CACHE.clear()
        scope._RETRY.clear()
        self._fetch = scope.fetch_profile
        scope.fetch_profile = lambda player: {'pt': 1000.0, 'arena_rank': 150}
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE stat_views(digest TEXT, player TEXT)')
        self.db.executemany('INSERT INTO stat_views VALUES(?,?)', [('d1', '甲'), ('d1', '乙')])
        self.state = {'members': [dict(player='甲', rank=1)]}

    def tearDown(self):
        scope.fetch_profile = self._fetch
        scope._CACHE.clear()
        scope._RETRY.clear()
        self.db.close()

    def test_top200_keeps_player_resolved_on_demand(self):
        rows = [dict(digest='d1', player='乙')]
        with scope.population_scope('200', '200'):
            kept = scope.filter_rows(self.db, rows, self.state, time.time())
        self.assertEqual([row['player'] for row in kept], ['乙'])
        self.assertEqual(kept[0]['rank'], 150)

    def test_top100_drops_player_outside_roster(self):
        rows = [dict(digest='d1', player='乙')]
        with scope.population_scope('100', '100'):
            kept = scope.filter_rows(self.db, rows, self.state, time.time())
        self.assertEqual(kept, [])

    def test_no_scope_returns_rows_untouched(self):
        rows = [dict(digest='d1', player='乙')]
        self.assertEqual(scope.filter_rows(self.db, rows, self.state, time.time()), rows)


if __name__ == '__main__':
    unittest.main()
