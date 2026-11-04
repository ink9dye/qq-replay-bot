# -*- coding: utf-8 -*-
"""Unit tests for the field-based deck naming (synthetic decks, no database)."""
from __future__ import annotations

import sqlite3
import unittest

from bot import series_naming

def catalog(tags):
    cards = {}
    for code, values in tags.items():
        cards[int(code)] = dict(tags=list(values), complete=True)
    return dict(status='ready', revision='test', cards=cards)


def deck(copies):
    return [dict(code=int(code), copies=int(count)) for code, count in copies.items()]


class NamingTests(unittest.TestCase):
    def config(self, **overrides):
        raw = dict(
            exceptions=[dict(name='混沌', sources=[], add=[900], keywords=[]),
                        dict(name='黑魔导', sources=[], add=[901], keywords=[])],
            keywords={}, ignore_fields=[],
        )
        raw.update(overrides)
        return series_naming.normalize_config(raw)

    def propose(self, tags, copies, **overrides):
        return series_naming.propose(deck(copies), catalog(tags), self.config(**overrides))

    def test_single_field_names_the_deck(self):
        tags = {code: ['系列甲'] for code in range(1, 9)}
        result = self.propose(tags, {code: 1 for code in range(1, 9)})
        self.assertEqual(result['name'], '系列甲')
        self.assertEqual(result['basis']['primary'], ('系列甲', 8))

    def test_pipe_tag_is_normalized_to_its_first_name(self):
        tags = {code: ['水晶机巧|非「机巧」'] for code in range(1, 9)}
        result = self.propose(tags, {code: 1 for code in range(1, 9)})
        self.assertEqual(result['name'], '水晶机巧')
        self.assertEqual(result['basis']['primary'], ('水晶机巧', 8))

    def test_two_fields_put_the_larger_count_last(self):
        tags = {code: ['系列甲'] for code in range(1, 11)}
        tags.update({code: ['系列乙'] for code in range(11, 16)})
        copies = {code: 1 for code in range(1, 11)}
        copies.update({code: 1 for code in range(11, 16)})
        result = self.propose(tags, copies)
        self.assertEqual(result['name'], '系列乙系列甲')

    def test_four_fields_ordered_by_ascending_copies(self):
        tags = {}
        copies = {}
        plan = [(100, '甲', 12), (200, '乙', 9), (300, '丙', 6), (400, '丁', 4)]
        for base, tag, count in plan:
            for offset in range(count):
                tags[base + offset] = [tag]
                copies[base + offset] = 1
        result = self.propose(tags, copies)
        self.assertEqual(result['name'], '丁丙乙甲')
        self.assertEqual(result['basis']['primary'], ('甲', 12))

    def test_chaos_exception_is_prefixed_without_threshold(self):
        tags = {code: ['系列甲'] for code in range(1, 10)}
        tags[900] = []
        copies = {code: 1 for code in range(1, 10)}
        copies[900] = 1
        result = self.propose(tags, copies)
        self.assertEqual(result['name'], '混沌系列甲')
        self.assertEqual(result['exceptions'], [dict(name='混沌', copies=1)])

    def test_dark_magician_exception_is_prefixed(self):
        tags = {code: ['系列乙'] for code in range(11, 19)}
        tags[901] = []
        copies = {code: 1 for code in range(11, 19)}
        copies[901] = 2
        result = self.propose(tags, copies)
        self.assertEqual(result['name'], '黑魔导系列乙')

    def test_below_primary_threshold_is_not_named(self):
        tags = {code: ['系列甲'] for code in range(1, 8)}
        result = self.propose(tags, {code: 1 for code in range(1, 5)})
        self.assertIsNone(result['name'])
        self.assertEqual(result['basis']['reason'], 'below_primary_threshold')

    def test_nested_family_uses_broader_name_when_subfamily_is_small(self):
        tags = {code: ['闪刀'] for code in range(1, 16)}
        for code in range(100, 104):
            tags[code] = ['闪刀', '闪刀姬']
        copies = {code: 1 for code in range(1, 16)}
        copies.update({code: 1 for code in range(100, 104)})
        result = self.propose(tags, copies)
        self.assertEqual(result['basis']['primary'], ('闪刀', 19))
        self.assertEqual(result['name'], '闪刀')

    def test_nested_family_uses_specific_name_when_counts_are_close(self):
        tags = {code: ['三幻魔', '幻魔'] for code in range(1, 20)}
        copies = {code: 1 for code in range(1, 20)}
        result = self.propose(tags, copies)
        self.assertEqual(result['name'], '三幻魔')

    def test_group_rename_and_added_cards_change_the_name(self):
        tags = {code: ['无垢大艺术'] for code in range(1, 9)}
        tags[555] = []
        copies = {code: 1 for code in range(1, 9)}
        copies[555] = 2
        config = dict(
            exceptions=[dict(name='混沌', sources=[], add=[900], keywords=[]),
                        dict(name='黑魔导', sources=[], add=[901], keywords=[])],
            keywords={}, ignore_fields=[],
            groups=[dict(name='新名', sources=['无垢大艺术'], add=[555])],
        )
        result = series_naming.propose(deck(copies), catalog(tags), series_naming.normalize_config(config))
        self.assertEqual(result['name'], '新名')
        self.assertEqual(result['basis']['primary'], ('新名', 10))

    def test_group_remove_takes_cards_out(self):
        tags = {code: ['系列甲'] for code in range(1, 11)}
        copies = {code: 1 for code in range(1, 11)}
        config = dict(
            exceptions=[dict(name='混沌', sources=[], add=[900], keywords=[]),
                        dict(name='黑魔导', sources=[], add=[901], keywords=[])],
            keywords={}, ignore_fields=[],
            groups=[dict(name='系列甲', sources=['系列甲'], remove=[1, 2])],
        )
        result = series_naming.propose(deck(copies), catalog(tags), series_naming.normalize_config(config))
        self.assertEqual(result['basis']['primary'], ('系列甲', 8))

    def test_unmapped_card_uses_keyword_fallback_only(self):
        tags = {code: ['系列甲'] for code in range(1, 9)}
        copies = {code: 1 for code in range(1, 9)}
        copies[777] = 1
        config = dict(
            exceptions=[dict(name='混沌', sources=[], add=[900], keywords=[]),
                        dict(name='黑魔导', sources=[], add=[901], keywords=[])],
            keywords={'系列甲': ['测试关键词']}, ignore_fields=[],
        )
        names = {'777': dict(cn_name='测试关键词怪兽')}
        result = series_naming.propose(deck(copies), catalog(tags), series_naming.normalize_config(config), names)
        self.assertEqual(result['basis']['primary'], ('系列甲', 9))

    def test_ignored_field_does_not_name_the_deck(self):
        tags = {code: ['融合'] for code in range(1, 11)}
        config = dict(
            exceptions=[dict(name='混沌', sources=[], add=[900], keywords=[]),
                        dict(name='黑魔导', sources=[], add=[901], keywords=[])],
            keywords={}, ignore_fields=['融合'],
        )
        result = series_naming.propose(deck({code: 1 for code in range(1, 11)}), catalog(tags),
                                       series_naming.normalize_config(config))
        self.assertIsNone(result['name'])

    def test_empty_exceptions_are_allowed_and_names_only_fields(self):
        config = series_naming.normalize_config({'ignore_fields': [], 'keywords': {}})
        tags = {code: ['系列甲'] for code in range(1, 9)}
        result = series_naming.propose(deck({code: 1 for code in range(1, 9)}), catalog(tags), config)
        self.assertEqual(result['name'], '系列甲')
        self.assertEqual(result['exceptions'], [])

    def test_parent_group_absorbs_keyword_child_without_double_listing(self):
        # 烙印（官方标签）包含 阿不思（卡名关键词，官方标签里没有）：
        # 阿不思的卡也计入烙印，名字里只出现大组合“烙印”。
        tags = {code: ['烙印'] for code in range(1, 7)}
        copies = {code: 1 for code in range(1, 7)}
        copies[500] = 2
        names = {'500': dict(cn_name='阿不思的落胤')}
        config = dict(
            exceptions=[], keywords={}, ignore_fields=[],
            groups=[dict(name='烙印', sources=['烙印'], includes=['阿不思']),
                    dict(name='阿不思', keywords=['阿不思'], keyword_scope='all')],
        )
        result = series_naming.propose(deck(copies), catalog(tags),
                                       series_naming.normalize_config(config), names)
        self.assertEqual(result['name'], '烙印')
        self.assertEqual(result['basis']['primary'], ('烙印', 8))
        self.assertNotIn('阿不思', [item['name'] for item in result['fields']])

    def test_keyword_group_only_matches_named_cards_when_scope_is_all(self):
        tags = {500: []}
        copies = {500: 5}
        names = {'500': dict(cn_name='阿不思的落胤')}
        config = dict(
            exceptions=[], keywords={}, ignore_fields=[],
            primary_threshold=3, secondary_threshold=2,
            groups=[dict(name='阿不思', keywords=['阿不思'], keyword_scope='all')],
        )
        result = series_naming.propose(deck(copies), catalog(tags),
                                       series_naming.normalize_config(config), names)
        self.assertEqual(result['basis']['primary'], ('阿不思', 5))

    def test_subset_only_is_named_by_the_smaller_group(self):
        # 只有 4 张阿不思、没有额外的烙印卡：大小组合张数相同，取更具体的“阿不思”。
        tags = {500: []}
        copies = {500: 4}
        names = {'500': dict(cn_name='阿不思的落胤')}
        config = dict(
            exceptions=[], keywords={}, ignore_fields=[],
            primary_threshold=3, secondary_threshold=2,
            groups=[dict(name='烙印', sources=['烙印'], includes=['阿不思']),
                    dict(name='阿不思', keywords=['阿不思'], keyword_scope='all')],
        )
        result = series_naming.propose(deck(copies), catalog(tags),
                                       series_naming.normalize_config(config), names)
        self.assertEqual(result['name'], '阿不思')

    def test_extra_deck_cards_count_into_fields(self):
        tags = {1: ['甲'], 2: ['甲']}
        config = series_naming.normalize_config(dict(
            exceptions=[], keywords={}, ignore_fields=[],
            primary_threshold=4, secondary_threshold=3,
            groups=[dict(name='甲', sources=['甲'])]))
        result = series_naming.propose(deck({1: 1}), catalog(tags), config,
                                       extra_cards=deck({2: 8}))
        self.assertEqual(result['name'], '甲')
        self.assertEqual(result['basis']['primary'], ('甲', 5))

    def test_three_required_cards_all_present_qualify_below_threshold(self):
        config = series_naming.normalize_config(dict(
            exceptions=[], keywords={}, ignore_fields=[],
            primary_threshold=8, secondary_threshold=4,
            groups=[dict(name='混沌', add=[1, 2, 3])]))
        tags = {1: [], 2: [], 3: []}
        result = series_naming.propose(deck({1: 1, 2: 1, 3: 1}), catalog(tags), config)
        self.assertEqual(result['name'], '混沌')
        missing = series_naming.propose(deck({1: 1, 2: 1}), catalog(tags), config)
        self.assertIsNone(missing['name'])

    def test_small_name_when_only_required_cards_present(self):
        config = series_naming.normalize_config(dict(
            exceptions=[], keywords={}, ignore_fields=[],
            primary_threshold=4, secondary_threshold=3,
            groups=[dict(name='大轴', small_name='小轴', add=[1], optional=[2])]))
        only_small = series_naming.propose(deck({1: 5}), catalog({1: [], 2: []}), config)
        self.assertEqual(only_small['name'], '小轴')
        both = series_naming.propose(deck({1: 2, 2: 5}), catalog({1: [], 2: []}), config)
        self.assertEqual(both['name'], '大轴')
        self.assertEqual(both['basis']['primary'], ('大轴', 7))

    def test_big_core_present_switches_to_big_name_and_absorbs_small(self):
        # 小轴的必选项单独在场叫小轴；大轴自己的核心卡也登场时改叫大轴，小轴名不再重复列。
        tags = {1: ['大轴'], 2: ['小轴']}
        config = dict(
            exceptions=[], keywords={}, ignore_fields=[],
            primary_threshold=4, secondary_threshold=3,
            groups=[dict(name='大轴', sources=['大轴'], includes=['小轴']),
                    dict(name='小轴', sources=['小轴'])],
        )
        only_small = series_naming.propose(deck({2: 5}), catalog(tags),
                                           series_naming.normalize_config(config))
        self.assertEqual(only_small['name'], '小轴')
        both = series_naming.propose(deck({1: 2, 2: 5}), catalog(tags),
                                     series_naming.normalize_config(config))
        self.assertEqual(both['name'], '大轴')
        self.assertEqual(both['basis']['primary'], ('大轴', 7))
        self.assertNotIn('小轴', [item['name'] for item in both['fields']])

    def test_includes_cycle_is_rejected(self):
        config = series_naming.normalize_config(dict(
            exceptions=[], keywords={}, ignore_fields=[],
            groups=[dict(name='甲', includes=['乙']), dict(name='乙', includes=['甲'])]))
        with self.assertRaises(series_naming.ConfigError):
            series_naming.build_registry(catalog({1: ['甲']}), config)

    def test_plan_replaces_old_manual_and_rejects_manual_priority(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('''
CREATE TABLE stat_builds(fingerprint TEXT,deck_type TEXT,classification_source TEXT,
  classification_status TEXT,classification_version TEXT,classification_score REAL,main_complete INTEGER);
CREATE TABLE stat_build_cards(build TEXT,partition TEXT,code INTEGER,copies INTEGER);
''')
        db.execute("INSERT INTO stat_builds VALUES('b1','旧人工名','manual','recognized','old',NULL,1)")
        for code in range(1, 9):
            db.execute("INSERT INTO stat_build_cards VALUES('b1','main',?,1)", (code,))
        tags = {code: ['系列甲'] for code in range(1, 9)}
        override = dict(ignore_fields=[], keywords={}, exceptions=[], min_main_copies=1)
        config = series_naming.normalize_config(dict(override, respect_manual=False))
        row = series_naming.plan(db, 1000.0, config, catalog(tags), {},
                                 builds=['b1'])['rows'][0]
        self.assertEqual(row['action'], 'create')
        self.assertEqual(row['name'], '系列甲')
        with self.assertRaises(series_naming.ConfigError):
            series_naming.normalize_config(dict(override, respect_manual=True))
        config = series_naming.normalize_config(dict(override, require_main_complete=True))
        db.execute("UPDATE stat_builds SET main_complete=0")
        row = series_naming.plan(db, 1000.0, config, catalog(tags), {}, builds=['b1'])['rows'][0]
        self.assertEqual(row['action'], 'clear')
        self.assertEqual(row['reason'], 'main_incomplete')
        with self.assertRaises(series_naming.ConfigError):
            series_naming.normalize_config(dict(override, overwrite_reference=False))
        db.close()

    def test_config_rejects_unknown_keys(self):
        with self.assertRaises(series_naming.ConfigError):
            series_naming.normalize_config({'primary_thresh': 8})

    def test_config_rejects_duplicate_group_names(self):
        with self.assertRaises(series_naming.ConfigError):
            series_naming.normalize_config({
                'exceptions': [dict(name='混沌', sources=[], add=[], keywords=[]),
                               dict(name='黑魔导', sources=[], add=[], keywords=[])],
                'groups': [dict(name='混沌', sources=['X'])],
            })


if __name__ == '__main__':
    unittest.main()

