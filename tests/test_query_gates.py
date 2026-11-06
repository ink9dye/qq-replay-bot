# -*- coding: utf-8 -*-
"""轻/重查询分类：搜索不被构筑占用。"""
import unittest

from bot.query_api import QueryHandlerMixin


class GateClassificationTests(unittest.TestCase):
    def test_heavy_paths(self):
        for path in ("/query-api/player/%E7%94%B2/decks",
                     "/query-api/player/%E7%94%B2/replays",
                     "/query-api/player/%E7%94%B2/history",
                     "/query-api/player/%E7%94%B2/pt",
                     "/query-api/card/89631139",
                     "/query-api/command"):
            self.assertTrue(QueryHandlerMixin._is_heavy(path), path)

    def test_light_paths(self):
        for path in ("/query-api/status", "/query-api/me", "/query-api/top100",
                     "/query-api/subscriptions", "/query-api/search",
                     "/query-api/charts/pie", "/query-api/login", "/query-api/refresh"):
            self.assertFalse(QueryHandlerMixin._is_heavy(path), path)


if __name__ == "__main__":
    unittest.main()
