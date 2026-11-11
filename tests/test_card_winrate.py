import unittest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from bot.card_winrate import message, _compute


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
                 patch('bot.card_identity.identity', return_value=dict(codes=[1], canonical_card=1)), \
                 patch('bot.card_identity.facts', return_value=facts), \
                 patch('bot.card_statistics._opponent_rows', return_value=opponents) as lookup:
                result = _compute(path, tmp, 0, 1)
            lookup.assert_called_once()
        self.assertEqual(result['matchups']['base']['games'], 4)
        self.assertEqual(result['matchups']['base']['rate'], 50)
        first, second, unknown = (result['order'][key] for key in ('first', 'second', 'unknown'))
        self.assertEqual((first['games'], first['rate']), (1, 100))
        self.assertEqual((second['games'], second['losses'], second['draws'], second['rate']), (2, 1, 1, 0))
        self.assertEqual((unknown['games'], unknown['rate']), (1, 100))
        text = message(dict(result, code=1, name='测试卡'))
        self.assertIn('先攻使用后胜率 100.0%（1胜/0负/0平，共1局）（样本少）', text)
        self.assertIn('后攻使用后胜率 0.0%（0胜/1负/1平，共2局）（样本少）', text)
        self.assertIn('先后攻未知 1局：仅计入总体。', text)


if __name__ == '__main__':
    unittest.main()
