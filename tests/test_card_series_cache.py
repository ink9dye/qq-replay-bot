# -*- coding: utf-8 -*-
"""系列标注的按构筑缓存：同版本命中、版本变化重算、输出不变。"""
import unittest

from bot import card_series


class _Db:
    def __init__(self):
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append(sql)
        if "stat_build_series" in sql:
            return [dict(build="b1", series="S")]
        if "stat_build_cards" in sql:
            return [dict(build="b1", partition="main", code=1, copies=3)]
        return []


def _catalog(revision="r1"):
    return dict(status="ready", revision=revision,
                cards={1: dict(tags=["T"], complete=True)})


def _rows():
    return [dict(build="b1", classification_source="auto", main_complete=True,
                 extra_complete=True, series_status="known")]


class CardSeriesCacheTests(unittest.TestCase):
    def setUp(self):
        card_series._BUILD_CACHE.clear()

    def test_same_revision_is_cached_and_output_identical(self):
        first = card_series.annotate(_Db(), _rows(), _catalog())
        self.assertEqual(first[0]["series"], ["S", "T"])
        self.assertEqual(first[0]["series_membership"], {"T": dict(main_copies=3, extra_copies=0)})
        self.assertEqual(len(card_series._BUILD_CACHE), 1)
        second = card_series.annotate(_Db(), _rows(), _catalog())
        self.assertEqual(second, first)

    def test_new_revision_recomputes(self):
        card_series.annotate(_Db(), _rows(), _catalog("r1"))
        card_series.annotate(_Db(), _rows(), _catalog("r2"))
        self.assertEqual(len(card_series._BUILD_CACHE), 2)

    def test_manual_build_is_not_served_from_reference_cache(self):
        rows = _rows()
        rows[0]["classification_source"] = "manual"
        card_series.annotate(_Db(), rows, _catalog("r1"))
        again = card_series.annotate(_Db(), _rows(), _catalog("r1"))
        # 手工分类的行走 manual 分支，不写入参考缓存，也不会被 cache 命中影响
        self.assertEqual(again[0]["series"], ["S", "T"])


if __name__ == "__main__":
    unittest.main()
