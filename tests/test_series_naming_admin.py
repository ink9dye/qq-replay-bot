# -*- coding: utf-8 -*-
"""组合编辑后端测试：配置保存/备份、组增删卡、改名、包含关系、例外切换。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot import series_naming_admin as admin
from bot.card_series import SEMANTICS


def reference(cards):
    return dict(version=1, semantics=SEMANTICS,
                cards={str(code): dict(tags=list(tags), complete=True) for code, tags in cards.items()})


def database():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
CREATE TABLE stat_current_top100(rank INTEGER,player TEXT,points REAL);
CREATE TABLE stat_views(player TEXT,build TEXT,digest TEXT);
CREATE TABLE stat_games(digest TEXT,recorded_at REAL,expires_at REAL);
CREATE TABLE stat_builds(fingerprint TEXT,deck_type TEXT,classification_source TEXT,
  classification_status TEXT,classification_version TEXT,classification_score REAL,main_complete INTEGER);
CREATE TABLE stat_build_cards(build TEXT,partition TEXT,code INTEGER,copies INTEGER);
''')
    db.execute("INSERT INTO stat_current_top100 VALUES(1,'甲',1500)")
    db.execute("INSERT INTO stat_views VALUES('甲','b1','d1')")
    db.execute("INSERT INTO stat_games VALUES('d1',100.0,9999999999.0)")
    db.execute("INSERT INTO stat_builds VALUES('b1',NULL,'unclassified','unknown',NULL,NULL,1)")
    db.execute("INSERT INTO stat_build_cards VALUES('b1','main',100,3)")
    db.execute("INSERT INTO stat_build_cards VALUES('b1','main',101,2)")
    return db


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / 'cache').mkdir()
        (self.data / 'card-series-reference.json').write_text(json.dumps(reference({
            100: ['烙印'], 101: ['烙印'], 102: ['阿不思'], 200: [], 201: [],
        }), ensure_ascii=False), encoding='utf-8')
        (self.data / 'cache' / 'cards.json').write_text(json.dumps({
            '68468459': dict(id=68468459, cn_name='阿不思的落胤'),
            '200': dict(id=200, cn_name='阿不思的落胤'),
            '201': dict(id=201, cn_name='烙印开幕'),
        }, ensure_ascii=False), encoding='utf-8')
        self.db = database()
        self.addCleanup(self.db.close)

    def save(self, payload):
        return admin.update_group(None, self.data, 1000.0, payload)

    def test_create_add_cards_and_list_usage(self):
        self.save(dict(op='create', name='烙印', sources=['烙印']))
        self.save(dict(op='add_cards', name='烙印', cards=[200]))
        result = admin.list_groups(self.db, self.data, 1000.0)
        row = next(item for item in result['rows'] if item['name'] == '烙印')
        self.assertEqual(row['cards_total'], 3)
        self.assertEqual(row['used_builds'], 1)
        detail = admin.group_detail(self.db, self.data, 1000.0, '烙印')
        codes = sorted(card['code'] for card in detail['cards'])
        self.assertEqual(codes, [100, 101, 200])
        self.assertTrue(detail['builds'])

    def test_representative_must_be_member_and_can_be_changed(self):
        self.save(dict(op='create', name='烙印', sources=['烙印']))
        self.save(dict(op='representative', name='烙印', card=101))
        detail = admin.group_detail(self.db, self.data, 1000.0, '烙印')
        self.assertEqual(detail['representative'], 101)
        with self.assertRaises(ValueError):
            self.save(dict(op='representative', name='烙印', card=200))
        self.save(dict(op='representative', name='烙印', card=None))
        detail = admin.group_detail(self.db, self.data, 1000.0, '烙印')
        self.assertIsNone(detail['representative'])

    def test_card_terms_accept_name_and_code(self):
        self.save(dict(op='create', name='阿不思组', keywords=['阿不思'], keyword_scope='all'))
        self.save(dict(op='add_cards', name='阿不思组', cards=['阿不思的落胤', '201']))
        detail = admin.group_detail(self.db, self.data, 1000.0, '阿不思组')
        codes = sorted(card['code'] for card in detail['cards'])
        self.assertEqual(codes, [200, 201])

    def test_rename_and_duplicate_rejected(self):
        self.save(dict(op='create', name='烙印', sources=['烙印']))
        self.save(dict(op='save', before='烙印', name='烙印2', sources=['烙印']))
        with self.assertRaises(ValueError):
            self.save(dict(op='create', name='烙印2', sources=['烙印']))
        detail = admin.group_detail(self.db, self.data, 1000.0, '烙印2')
        self.assertEqual(detail['cards_total'], 2)

    def test_includes_cycle_rejected(self):
        self.save(dict(op='create', name='甲', includes=['乙']))
        with self.assertRaises(ValueError):
            self.save(dict(op='create', name='乙', includes=['甲']))

    def test_exception_toggle_moves_group(self):
        self.save(dict(op='create', name='混沌', sources=['阿不思']))
        self.save(dict(op='save', name='混沌', sources=['阿不思'], exception=True))
        raw = admin.load_raw(self.data)
        self.assertEqual([item['name'] for item in raw.get('exceptions') or []], ['混沌'])
        detail = admin.group_detail(self.db, self.data, 1000.0, '混沌')
        self.assertTrue(detail['exception'])
        self.save(dict(op='save', name='混沌', sources=['阿不思'], exception=False))
        raw = admin.load_raw(self.data)
        self.assertEqual(raw.get('exceptions') or [], [])
        self.assertEqual([item['name'] for item in raw.get('groups') or []], ['混沌'])

    def test_remove_from_derived_group_freezes_sources(self):
        self.save(dict(op='remove_cards', name='烙印', cards=[100]))
        raw = admin.load_raw(self.data)
        self.assertEqual(raw['groups'][0]['name'], '烙印')
        self.assertEqual(raw['groups'][0]['sources'], ['烙印'])
        self.assertEqual(raw['groups'][0]['remove'], [100])
        detail = admin.group_detail(self.db, self.data, 1000.0, '烙印')
        self.assertEqual(sorted(card['code'] for card in detail['cards']), [101])

    def test_thresholds_and_backup(self):
        self.save(dict(op='create', name='烙印', sources=['烙印']))
        self.save(dict(op='thresholds', primary_threshold=7, secondary_threshold=3, max_fields=3))
        config = admin.series_naming.load_config(self.data)
        self.assertEqual(config['primary_threshold'], 7)
        self.assertEqual(config['secondary_threshold'], 3)
        self.assertEqual(config['max_fields'], 3)
        backups = list((self.data / 'config-backups').glob('series-naming.*.json'))
        self.assertTrue(backups)


if __name__ == '__main__':
    unittest.main()
