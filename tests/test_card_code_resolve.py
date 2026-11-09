import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from bot.card_usage_pie import collect, resolve_card


def _data_dir(root, cards):
    cache = Path(root) / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    (cache / 'cards.json').write_text(json.dumps(cards, ensure_ascii=False), encoding='utf-8')


def _database(root, rows):
    path = Path(root) / 'bot.sqlite3'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE stat_build_cards(build TEXT, partition TEXT, code INTEGER, copies INTEGER)')
    db.executemany('INSERT INTO stat_build_cards VALUES(?,?,?,?)', rows)
    db.commit()
    db.close()
    return path


SAME_NAME = {
    '1': {'cid': 1, 'id': 71599345, 'cn_name': '决斗者之力'},
    '2': {'cid': 2, 'id': 101307067, 'cn_name': '决斗者之力'},
}


class ResolveCardByDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _resolve(self, rows, query='决斗者之力', use_db=True):
        _data_dir(self.root, SAME_NAME)
        path = _database(self.root, rows)
        db = sqlite3.connect(path)
        try:
            return resolve_card(str(self.root), query, db if use_db else None)
        finally:
            db.close()

    def test_falls_back_to_code_that_has_records(self):
        # 先行号有数据、正式号是空号：旧逻辑取最小卡号 71599345，图表全是 0。
        self.assertEqual(self._resolve([('b1', 'main', 101307067, 3)]),
                         (101307067, '决斗者之力'))

    def test_keeps_smallest_code_when_it_has_records(self):
        self.assertEqual(self._resolve([('b1', 'main', 71599345, 2)]),
                         (71599345, '决斗者之力'))

    def test_keeps_smallest_code_when_nothing_recorded(self):
        self.assertEqual(self._resolve([]), (71599345, '决斗者之力'))

    def test_without_database_keeps_old_behaviour(self):
        self.assertEqual(self._resolve([('b1', 'main', 101307067, 3)], use_db=False),
                         (71599345, '决斗者之力'))


USAGE_SCHEMA = '''
CREATE TABLE stat_current_top100(player TEXT, rank INTEGER);
CREATE TABLE stat_views(player TEXT, build TEXT, digest TEXT);
CREATE TABLE stat_files(digest TEXT, replay_id INTEGER);
CREATE TABLE replays(id INTEGER, recorded_at REAL);
CREATE TABLE stat_builds(fingerprint TEXT, main_complete INTEGER, side_complete INTEGER);
CREATE TABLE stat_build_cards(build TEXT, partition TEXT, code INTEGER, copies INTEGER);
'''


class UsageMergesPrintsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_usage_counts_all_codes_of_the_same_card(self):
        _data_dir(self.root, SAME_NAME)
        (self.root / 'card-equivalence.json').write_text(json.dumps({
            'version': 1,
            'groups': [dict(canonical=71599345, codes=[71599345, 101307067],
                            name='决斗者之力')],
        }, ensure_ascii=False), encoding='utf-8')
        now = time.time()
        db = sqlite3.connect(self.root / 'bot.sqlite3')
        db.executescript(USAGE_SCHEMA)
        db.executemany('INSERT INTO stat_current_top100 VALUES(?,?)', [('p1', 1), ('p2', 2)])
        db.executemany('INSERT INTO stat_views VALUES(?,?,?)',
                       [('p1', 'b1', 'd1'), ('p2', 'b2', 'd2')])
        db.executemany('INSERT INTO stat_files VALUES(?,?)', [('d1', 1), ('d2', 2)])
        db.executemany('INSERT INTO replays VALUES(?,?)', [(1, now - 3600), (2, now - 3600)])
        db.executemany('INSERT INTO stat_builds VALUES(?,?,?)', [('b1', 1, 0), ('b2', 1, 0)])
        db.executemany('INSERT INTO stat_build_cards VALUES(?,?,?,?)',
                       [('b1', 'main', 101307067, 3), ('b2', 'main', 71599345, 2)])
        db.commit()
        db.close()
        payload = collect(self.root / 'bot.sqlite3', self.root, now, '决斗者之力')
        self.assertEqual(payload['code'], 71599345)
        self.assertEqual(payload['covered'], 2)
        self.assertEqual(payload['main_total'], 5)
        self.assertEqual(payload['main_dist'], {0: 0, 1: 0, 2: 1, 3: 1})


if __name__ == '__main__':
    unittest.main()
