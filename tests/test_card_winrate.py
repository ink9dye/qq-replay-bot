import unittest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from bot.card_winrate import message, _compute, _popular_decks, _popular_matchups


def payload(rows, base=None):
    return dict(code=14558127, name='灰流丽', matchups=dict(
        base=base if base is not None else dict(games=2, wins=1, losses=1, draws=0, rate=50.0),
        rows=rows, min_games=10, fallback_games=5, limit=5))


class CardWinrateMessageTests(unittest.TestCase):
    def test_message_shows_overall_then_top_rows(self):
        text = message(payload([
            dict(deck_type='甲卡组', games=12, wins=9, losses=3, draws=0, rate=75.0,
                 low_sample=False),
            dict(deck_type='乙卡组', games=6, wins=3, losses=3, draws=0, rate=50.0,
                 low_sample=True),
        ]))
        self.assertIn('单卡胜率：灰流丽（14558127）', text)
        self.assertIn('使用后胜率 50.0%（1胜/1负/0平，共2局；双方当前前100）', text)
        self.assertIn('对前百卡组最高的2个（使用后）：', text)
        self.assertIn('1. 甲卡组 75.0%（9胜/3负/0平，12局）', text)
        self.assertIn('2. 乙卡组 50.0%（3胜/3负/0平，6局）（样本少）', text)
        self.assertIn('每行至少10局', text)

    def test_message_handles_no_samples(self):
        text = message(payload([], base=dict(games=0, wins=0, losses=0, draws=0, rate=None)))
        self.assertIn('暂无有效样本', text)
        self.assertIn('暂无达到5局的对手卡组', text)
        self.assertIn('先攻使用后胜率：暂无有效样本', text)
        self.assertIn('后攻使用后胜率：暂无有效样本', text)

    def test_order_uses_each_game_and_preserves_overall_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'stats.db'
            with sqlite3.connect(path) as db:
                db.executescript("CREATE TABLE stat_current_top100(player TEXT);"
                                 "INSERT INTO stat_current_top100 VALUES('甲'),('乙');"
                                 "CREATE TABLE stat_build_cards(build TEXT,code INTEGER,"
                                 "partition TEXT,copies INTEGER);"
                                 "INSERT INTO stat_build_cards VALUES('build',1,'main',3);")
            db.close()
            # 同一玩家不同小局换先后攻；未知座位与平局保留，未使用/榜外对手剔除。
            rows = [dict(digest=str(i), player='甲', build='build',
                         first_player=seat, outcome=outcome)
                    for i, (seat, outcome) in enumerate([
                        (1, 'win'), (0, 'loss'), (0, 'draw'), (None, 'win'),
                        (1, 'win'), (0, 'win'), (1, None)])]
            facts = {(r['digest'], '甲'): dict(used=0 if i == 4 else 1)
                     for i, r in enumerate(rows)}
            opponents = {r['digest']: [dict(player='榜外' if i == 5 else '乙',
                                            deck_type='对手卡组')]
                         for i, r in enumerate(rows)}
            with patch('bot.card_winrate._basis', return_value=rows), \
                 patch('bot.card_winrate._popular_decks', return_value=[
                     dict(deck_type='对手卡组', players=20, share=20)]), \
                 patch('bot.card_identity.identity', return_value=dict(codes=[1], canonical_card=1)), \
                 patch('bot.card_identity.facts', return_value=facts), \
                 patch('bot.card_statistics._opponent_rows', return_value=opponents) as lookup:
                result = _compute(path, tmp, 0, 1)
            lookup.assert_called_once()
        self.assertEqual(result['matchups']['base']['games'], 4)
        self.assertEqual(result['matchups']['base']['rate'], 50)
        self.assertEqual(result['popular_matchups']['rows'][0]['games'], 4)
        self.assertEqual(result['popular_matchups']['rows'][0]['rate'], 50)
        first, second, unknown = (result['order'][key] for key in ('first', 'second', 'unknown'))
        self.assertEqual((first['games'], first['rate']), (1, 100))
        self.assertEqual((second['games'], second['losses'], second['draws'], second['rate']), (2, 1, 1, 0))
        self.assertEqual((unknown['games'], unknown['rate']), (1, 100))
        text = message(dict(result, code=1, name='测试卡'))
        self.assertIn('先攻使用后胜率 100.0%（1胜/0负/0平，共1局）（样本少）', text)
        self.assertIn('后攻使用后胜率 0.0%（0胜/1负/1平，共2局）（样本少）', text)
        self.assertIn('先后攻未知 1局：仅计入总体。', text)

    def test_popular_decks_use_pie_people_and_exact_labels(self):
        # 名册中未观测玩家不进分母；未识别进分母，不占具体卡组名额。
        labels = ['混沌烙印'] * 4 + ['混沌影依'] * 3 + ['乙'] * 2 + ['丙', '丁', '戊'] + [None] * 5
        people = [dict(observed=True, deck_type=name) for name in labels]
        people.append(dict(observed=False))
        with patch('bot.statistics_dashboard.population', return_value=({}, {}, people)) as population:
            result = _popular_decks(None, '.', 10)
        population.assert_called_once_with(None, Path('.'), 10.0, resolve_rank=False)
        self.assertEqual([row['deck_type'] for row in result], ['混沌烙印', '混沌影依', '乙', '丁', '丙'])
        self.assertAlmostEqual(result[0]['share'], 400 / 17)
        self.assertEqual(result[0]['players'], 4)
        with patch('bot.statistics_dashboard.population', return_value=({}, {}, [])):
            self.assertEqual(_popular_decks(None, '.', 10), [])

    def test_popular_matchups_keep_low_and_zero_samples_and_scope(self):
        with sqlite3.connect(':memory:') as db:
            db.executescript("CREATE TABLE stat_current_top100(player TEXT);"
                             "INSERT INTO stat_current_top100 VALUES('a'),('b');")
            selected, facts, opponents = [], {}, {}
            # 未使用、未知胜负、榜外与不唯一对手均不能计作胜率样本。
            cases = [('win', 1, 'b'), ('draw', 1, 'b'), ('loss', 1, 'b'),
                     ('win', 0, 'b'), ('unknown', 1, 'b'), ('win', 1, 'outside'),
                     ('win', 1, 'ambiguous')]
            for i, (outcome, used, opponent) in enumerate(cases):
                digest = str(i)
                selected.append(dict(player='a', digest=digest, copies=1, outcome=outcome))
                facts[(digest, 'a')] = dict(used=used)
                opponents[digest] = [dict(player=opponent, deck_type='热门混轴')]
                if opponent == 'ambiguous':
                    opponents[digest].append(dict(player='b', deck_type='热门混轴'))
            decks = [dict(deck_type='热门混轴', players=20, share=40),
                     dict(deck_type='无使用样本', players=10, share=20)]
            with patch('bot.card_winrate._popular_decks', return_value=decks):
                result = _popular_matchups(db, '.', 10, selected, facts, opponents)
        row, empty = result['rows']
        self.assertEqual((row['wins'], row['losses'], row['draws'], row['games']), (1, 1, 1, 3))
        self.assertAlmostEqual(row['rate'], 100 / 3)
        self.assertTrue(row['low_sample'])
        self.assertEqual(empty['games'], 0)
        self.assertIsNone(empty['rate'])
        data = dict(payload([]), popular_matchups=result)
        text = message(data)
        self.assertIn('前百饼图占比最高的2个具体卡组', text)
        self.assertIn('热门混轴（饼图占比40.0%，20人） 33.3%', text)
        self.assertIn('3局）（样本少）', text)
        self.assertIn('无使用样本（饼图占比20.0%，10人）：暂无有效样本', text)
        self.assertLess(text.index('热门混轴'), text.index('无使用样本'))


if __name__ == '__main__':
    unittest.main()
