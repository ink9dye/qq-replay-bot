import unittest

from bot.card_winrate import message


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


if __name__ == '__main__':
    unittest.main()
