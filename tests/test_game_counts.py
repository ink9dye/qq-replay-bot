import json
import sqlite3
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
from bot.commands import parse_command
from bot.game_counts import GameCounter,current_ranks

class GameCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.db=self.root/'bot.sqlite3';self.now=datetime(2026,11,3,1,tzinfo=timezone(timedelta(hours=8))).timestamp();self.midnight=self.now-3600
        with sqlite3.connect(self.db) as db:
            db.executescript('CREATE TABLE replays(id INTEGER PRIMARY KEY,digest TEXT,players_json TEXT,recorded_at REAL,saved_at REAL);CREATE TABLE stat_views(digest TEXT,player TEXT,build TEXT);CREATE TABLE stat_builds(fingerprint TEXT PRIMARY KEY,deck_type TEXT);')
            rows=[(1,'g1','["A","B"]',self.now-7200,self.midnight-1),
                  (2,'g1','["A","B"]',self.now-7200,self.midnight+1),
                  (3,'g2','["A","C"]',self.now-600,self.midnight+5),
                  (4,'g3','["C","D"]',self.now-300,self.midnight+10),
                  (5,'old','["A","B"]',self.now-20*86400,self.midnight+10)]
            db.executemany('INSERT INTO replays VALUES(?,?,?,?,?)',rows)
            db.executemany('INSERT INTO stat_builds VALUES(?,?)',[('b1','xxyy'),('b2','xxzz'),('b3','other')])
            db.executemany('INSERT INTO stat_views VALUES(?,?,?)',[('g1','A','b1'),('g1','B','b2'),('g2','A','b2'),('g3','C','b3')])
        self.counter=GameCounter(self.db,self.root,clock=lambda:self.now,rank_provider=lambda *a:({'A':1,'B':150,'C':300,'D':0},self.now))

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

    def test_current_rank_any_side_and_internal_are_not_same(self):
        self.assertEqual(self.counter.query(limit=200)['count'],2)
        self.assertEqual(self.counter.query(limit=200,internal=True)['count'],1)

    def test_unknown_rank_is_not_assumed_outside_current200(self):
        self.counter.rank_provider=lambda *a:({'A':1},self.now)
        r=self.counter.query(limit=200);self.assertEqual((r['count'],r['unknown']),(2,1))
        r=self.counter.query(limit=200,internal=True);self.assertEqual((r['count'],r['unknown']),(0,3))

    def test_cache_does_not_requery_database_and_is_bounded(self):
        result=self.counter.query()
        with patch('bot.game_counts.sqlite3.connect',side_effect=AssertionError('repeated database query')):
            self.assertIs(self.counter.query(),result)
        for i in range(20):self.counter.query(str(i))
        self.assertLessEqual(len(self.counter._cache),16)

    def test_fresh_room_rank_samples_are_reused_but_expired_are_not(self):
        p=self.root/'capture-status.json';p.write_text(json.dumps(dict(updated_at=self.now,rank_samples=[dict(player='A',rank=150,checked_at=self.now-20),dict(player='B',rank=180,checked_at=self.now-121)])),encoding='utf-8')
        with sqlite3.connect(self.db) as db:ranks,_=current_ranks(db,self.root,self.now)
        self.assertEqual(ranks,{'A':150})

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
