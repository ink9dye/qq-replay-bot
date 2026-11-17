# -*- coding: utf-8 -*-
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.game_winrate import collect, message, _compute, MIN_GAMES, MIN_MATCHES


def row(digest, game_no, seat, outcome, session):
    return dict(digest=digest, player='甲', build='build',
                observed_game_no=game_no, first_player=seat,
                outcome=outcome, observed_session_id=session)


ROWS = [
    row('d1', 1, 1, 'win', 's1'),      # 主卡局先攻胜；s1 打平无法判定
    row('d2', 2, 0, 'loss', 's1'),     # Side局后攻负
    row('d3', 1, 1, 'win', 's2'),      # 主卡局先攻胜；s2 大局胜（对卡组A）
    row('d4', 2, 1, 'win', 's2'),      # Side局先攻胜
    row('d5', 1, 0, 'loss', 's3'),    # 主卡局后攻负；s3 大局负（对卡组B）
    row('d6', 2, 0, 'loss', 's3'),    # Side局后攻负
    row('d7', 1, 0, 'draw', None),    # 主卡局后攻平；缺场次关联
    row('d8', 2, 1, 'unknown', 's4'),  # 胜负未知
    row('d9', 1, None, 'win', 's5'),   # 座位未知；s5 大局胜（对卡组A）
]

OPPONENTS = {
    'd1': [dict(player='乙', deck_type='卡组A')],
    'd3': [dict(player='乙', deck_type='卡组A')],
    'd4': [dict(player='乙', deck_type='卡组A')],
    'd9': [dict(player='乙', deck_type='卡组A')],
    'd5': [dict(player='丙', deck_type='卡组B')],
    'd6': [dict(player='丙', deck_type='卡组B')],
}

POPULAR = [dict(deck_type='卡组A', players=10, share=50.0),
           dict(deck_type='卡组B', players=5, share=25.0),
           dict(deck_type='卡组C', players=2, share=10.0)]


def compute_rows(tmp, rows=ROWS, opponents=OPPONENTS, popular=POPULAR):
    path = Path(tmp) / 'stats.db'
    sqlite3.connect(path).close()
    detail = (dict(selected=dict(digest='d3', build='latest-build'),
                   person=dict(deck_type='最新卡组'),
                   matches=[dict(item, game_no=item['observed_game_no'],
                                 session_id=item['observed_session_id']) for item in rows])
              if rows else None)
    with patch('bot.statistics_build_detail.query', return_value=detail) as build_query, \
         patch('bot.card_statistics._opponent_rows', return_value=opponents), \
         patch('bot.card_winrate._popular_decks', return_value=popular):
        result = _compute(path, tmp, 0, '甲')
        assert build_query.call_args.kwargs == dict(scope='build')
        return result


class GameWinrateComputeTests(unittest.TestCase):
    def test_sections_total_and_unknowns(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compute_rows(tmp)
        self.assertTrue(result['found'])
        total = result['total']
        self.assertEqual((total['games'], total['wins'], total['losses'], total['draws']),
                         (8, 4, 3, 1))
        sections = result['sections']
        self.assertEqual((sections['main_first']['games'], sections['main_first']['wins']), (2, 2))
        self.assertEqual((sections['main_second']['games'], sections['main_second']['losses'],
                          sections['main_second']['draws']), (2, 1, 1))
        self.assertEqual((sections['side_first']['games'], sections['side_first']['wins']), (1, 1))
        self.assertEqual((sections['side_second']['games'], sections['side_second']['losses']), (2, 2))
        self.assertEqual(result['outcome_unknown'], 1)
        self.assertEqual(result['seat_unknown'], 1)
        self.assertEqual(result['no_session'], 1)

    def test_match_level_against_popular_decks(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compute_rows(tmp)
        # s1 打平 -> undetermined；s4 无已知胜负且缺对手视角 -> ambiguous。
        self.assertEqual(result['undetermined'], 1)
        self.assertEqual(result['ambiguous'], 1)
        rows = result['popular_rows']
        by_deck = {item['deck_type']: item for item in rows}
        self.assertEqual([item['deck_type'] for item in rows],
                         ['卡组A', '卡组B', '卡组C'])
        self.assertEqual((by_deck['卡组A']['games'], by_deck['卡组A']['wins']), (2, 2))
        self.assertTrue(by_deck['卡组A']['low_sample'])
        self.assertEqual((by_deck['卡组B']['games'], by_deck['卡组B']['losses']), (1, 1))
        self.assertTrue(by_deck['卡组B']['low_sample'])
        self.assertEqual(by_deck['卡组C']['games'], 0)
        self.assertIsNone(by_deck['卡组C']['rate'])

    def test_deck_label_uses_first_game_then_fallback(self):
        # 同一场各小局对手命名可能微差（构筑推断固有特性）：以第1局命名为准。
        opponents = dict(OPPONENTS)
        opponents['d4'] = [dict(player='乙', deck_type='卡组A改')]
        with tempfile.TemporaryDirectory() as tmp:
            result = compute_rows(tmp, opponents=opponents)
        by_deck = {item['deck_type']: item for item in result['popular_rows']}
        self.assertEqual(by_deck['卡组A']['games'], 2)  # s2 按第1局 d3 的名字归入卡组A
        self.assertNotIn('卡组A改', by_deck)

    def test_deck_label_falls_back_when_first_game_unnamed(self):
        opponents = dict(OPPONENTS)
        opponents['d3'] = [dict(player='乙', deck_type=None)]
        opponents['d4'] = [dict(player='乙', deck_type='卡组A改')]
        with tempfile.TemporaryDirectory() as tmp:
            result = compute_rows(tmp, opponents=opponents)
        by_deck = {item['deck_type']: item for item in result['popular_rows']}
        self.assertEqual(by_deck['卡组B']['games'], 1)   # s3 不受影响
        # s2 第1局无名、第2局叫“卡组A改”：按回退命名计入，不入“卡组A”。
        self.assertEqual(by_deck['卡组A']['games'], 1)  # 只剩 s5（第1局命中卡组A）
        self.assertNotIn('卡组A改', by_deck)

    def test_ambiguous_opponent_or_deck_excluded(self):
        opponents = dict(OPPONENTS)
        opponents['d5'] = [dict(player='丙', deck_type='卡组B'),
                          dict(player='丁', deck_type='卡组B')]
        with tempfile.TemporaryDirectory() as tmp:
            result = compute_rows(tmp, opponents=opponents)
        self.assertEqual(result['ambiguous'], 2)
        self.assertEqual([item for item in result['popular_rows']
                          if item['deck_type'] == '卡组B'][0]['games'], 0)

    def test_no_views_reports_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compute_rows(tmp, rows=[], opponents={}, popular=[])
        self.assertFalse(result['found'])
        self.assertIn('没有找到 甲 的有效小局', message(result))


class GameWinrateMessageTests(unittest.TestCase):
    def test_message_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compute_rows(tmp)
            text = message(result)
        self.assertIn('对局胜率：甲', text)
        self.assertIn('基准：最新构筑 最新卡组；与当前构筑录像范围一致。', text)
        self.assertNotIn('构筑指纹', text)
        self.assertIn('最新构筑对应录像范围的小局（含同场换备局）', text)
        self.assertIn('总体小局胜率 50.0%（4胜/3负/1平，共8局）', text)
        self.assertIn('主卡局先攻胜率 100.0%（2胜/0负/0平，共2局）（样本少）', text)
        self.assertIn('主卡局后攻胜率 0.0%（0胜/1负/1平，共2局）（样本少）', text)
        self.assertIn('Side局先攻胜率 100.0%（1胜/0负/0平，共1局）（样本少）', text)
        self.assertIn('Side局后攻胜率 0.0%（0胜/2负/0平，共2局）（样本少）', text)
        self.assertIn('胜负未知 1局：不计入任何胜率。', text)
        self.assertIn('先后攻未知 1局：仅计入总体。', text)
        self.assertIn('对前百饼图占比最高的3个具体卡组（大局）：', text)
        self.assertIn('1. 卡组A（饼图占比50.0%，10人） 大局胜率 100.0%（2胜/0负，2场）', text)
        self.assertIn('2. 卡组B（饼图占比25.0%，5人） 大局胜率 0.0%（0胜/1负，1场）（样本少）', text)
        self.assertIn('3. 卡组C（饼图占比10.0%，2人）：暂无有效大局样本', text)
        self.assertIn('另有 1场无法判定、1场对手或卡组不唯一，未计入大局。', text)
        self.assertIn('另有 1局缺少场次关联，只计入小局分项。', text)
        self.assertIn('主卡局=该场第1局', text)
        self.assertIn('小局不足%d局、大局不足%d场标“样本少”' % (MIN_GAMES, MIN_MATCHES), text)

    def test_message_empty_sections(self):
        payload = dict(player='乙', found=True,
                       total=dict(games=0, wins=0, losses=0, draws=0, rate=None,
                                  low_sample=False),
                       sections={key: dict(games=0, wins=0, losses=0, draws=0, rate=None,
                                           low_sample=False)
                                 for key, _ in
                                 (('main_first', ''), ('main_second', ''),
                                  ('side_first', ''), ('side_second', ''))},
                       outcome_unknown=0, seat_unknown=0, no_session=0,
                       popular_rows=[], popular_limit=5, undetermined=0, ambiguous=0)
        text = message(payload)
        self.assertIn('总体小局胜率：暂无胜负明确的样本', text)
        self.assertIn('主卡局先攻胜率：暂无有效样本', text)
        self.assertIn('Side局后攻胜率：暂无有效样本', text)
        self.assertIn('暂无已识别的具体卡组。', text)


class GameWinrateScopeIntegrationTests(unittest.TestCase):
    def setUp(self):
        import json
        import test_card_statistics as C
        C.CardStatisticsTests.setUp(self)
        self.now = C.NOW
        db = self.store._db
        # 第1/4条同场，4为换备后不同主卡；第2条是旧构筑且名称相同。
        for index, (session, number) in enumerate((('s1', 1), ('old', 1),
                                                  ('s3', 1), ('s1', 2), ('latest', 1))):
            db.execute('INSERT INTO stat_evidence VALUES(?,?,?,?)', (
                self.rows[index]['digest'], 'scope-fixture', 'live_server',
                json.dumps(dict(capture=dict(session_id=session, game_no=number,
                                             captured_from_start=True)))))
        old = dict(db.execute('SELECT b.* FROM stat_builds b JOIN stat_views v '
                              'ON b.fingerprint=v.build WHERE v.digest=? AND v.player=?',
                              (self.rows[1]['digest'], '甲')).fetchone())
        original = old['fingerprint']
        old['fingerprint'] = 'old-main-same-name'
        columns = list(old)
        db.execute('INSERT INTO stat_builds (' + ','.join(columns) + ') VALUES (' +
                   ','.join('?' for _ in columns) + ')', [old[c] for c in columns])
        db.execute('INSERT INTO stat_build_cards SELECT ?,partition,code+100,copies '
                   'FROM stat_build_cards WHERE build=?', (old['fingerprint'], original))
        db.execute('UPDATE stat_views SET build=? WHERE digest=? AND player=?',
                   (old['fingerprint'], self.rows[1]['digest'], '甲'))
        db.commit()

    def test_latest_build_matches_recordings_and_keeps_side_games(self):
        from bot.statistics_build_detail import query
        detail = query(self.store._db, self.replays, self.now, '甲', scope='build')
        self.assertEqual(detail['selected']['digest'], self.rows[4]['digest'])
        digests = {r['digest'] for r in detail['matches']}
        self.assertEqual(digests, {self.rows[i]['digest'] for i in (0, 2, 3, 4)})
        popular = [dict(deck_type='对手B', players=10, share=50.0)]
        with patch('bot.card_winrate._popular_decks', return_value=popular):
            result = _compute(self.store.path, self.replays, self.now, '甲')
        self.assertEqual(result['basis_build']['digest'], detail['selected']['digest'])
        self.assertEqual(result['total']['games'], 3)
        self.assertEqual(result['total']['wins'], 2)
        self.assertEqual(result['outcome_unknown'], 1)  # 同场换备局保留
        self.assertEqual(result['sections']['main_first']['games'], 3)
        self.assertEqual(result['popular_rows'][0]['games'], 2)
        self.assertEqual(result['popular_rows'][0]['wins'], 2)

    def test_no_reliable_first_game_does_not_use_all_player_games(self):
        self.store._db.execute('DELETE FROM stat_evidence')
        self.store._db.commit()
        result = _compute(self.store.path, self.replays, self.now, '甲')
        self.assertFalse(result['found'])


class GameWinrateCollectTests(unittest.TestCase):
    def test_collect_validates_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'stats.db'
            sqlite3.connect(path).close()
            with patch('bot.game_winrate._compute', return_value=dict(player='甲', found=False)) as compute:
                first = collect(path, tmp, 0, '甲')
                second = collect(path, tmp, 0, ' 甲 ')
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(first['player'], '甲')
            self.assertEqual(second['player'], '甲')
        with self.assertRaises(ValueError):
            collect('x', '.', 0, '')
        with self.assertRaises(ValueError):
            collect('x', '.', 0, '名' * 101)


if __name__ == '__main__':
    unittest.main()
