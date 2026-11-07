# -*- coding: utf-8 -*-
"""概率运算：无放回抽牌的至少命中概率（超几何分布）。"""
import asyncio
import re
import unittest
from math import comb

from bot.commands import parse_command
from bot.help import HELP
from bot.probability import probability_message
from test_bot import temporary_service
from test_groups import GroupQQ


def percent(text):
    return [float(value) for value in re.findall(r'(\d+(?:\.\d+)?)%', text)]


def event(text, user_id=10001, group_id=90000):
    return {'post_type': 'message', 'message_type': 'group', 'self_id': 3528698561,
            'user_id': user_id, 'group_id': group_id, 'message_id': 3,
            'message': [{'type': 'text', 'data': {'text': text}}]}


class BothQQ(GroupQQ):
    """私聊回复是纯文本，这里只要有个可调用的私聊合并转发入口。"""

    async def send_private_forward(self, user_id, cards, self_id=None):
        return True


class ProbabilityMathTests(unittest.TestCase):
    def test_example_40_5_5_1_uses_the_hypergeometric_distribution(self):
        text = probability_message([40, 5, 5, 1])
        at_least = (1 - comb(35, 5) / comb(40, 5)) * 100
        self.assertIn('牌堆 40 张，其中目标卡 5 张；抽 5 张', text)
        self.assertIn(f'至少命中 1 张：{at_least:.2f}%', text)
        self.assertIn('未达到（命中 0 张）', text)
        self.assertIn('期望命中：0.625 张', text)
        self.assertIn('超几何分布', text)

    def test_at_least_and_below_add_up_to_100(self):
        for values in ([40, 5, 5, 1], [60, 12, 5, 2], [40, 3, 10, 3], [5, 5, 5, 5]):
            values = list(values)
            # 至少命中数不能超过 min(目标数, 抽取数)；越界时按上限算。
            values[3] = min(values[3], values[1], values[2])
            hit, below = percent(probability_message(values))[:2]
            self.assertAlmostEqual(hit + below, 100.0, places=6, msg=str(values))

    def test_drawing_the_whole_deck_is_certain_and_counts_are_consistent(self):
        text = probability_message([40, 5, 40, 5])
        self.assertIn('至少命中 5 张：100.00%', text)
        self.assertIn('未达到（命中 0–4 张）：0%', text)
        self.assertIn('至少命中 1 张：2.50%', probability_message([40, 1, 1, 1]))
        self.assertIn('至少命中 2 张：', probability_message([40, 5, 5, 2]))

    def test_bad_arguments_are_rejected_with_actionable_text(self):
        cases = ([0, 1, 1, 1], [2001, 1, 1, 1], [40, 0, 1, 1], [40, 41, 1, 1],
                 [40, 5, 0, 1], [40, 5, 41, 1], [40, 5, 5, 0], [40, 5, 5, 6])
        for values in cases:
            with self.assertRaises(ValueError, msg=str(values)) as caught:
                probability_message(list(values))
            self.assertIn('要在', str(caught.exception), str(values))
        with self.assertRaisesRegex(ValueError, '格式'):
            probability_message([40, 5, 5])

    def test_command_parsing_and_help_explain_both_new_commands(self):
        self.assertEqual(parse_command('概率运算 40 5 5 1'),
                         {'op': 'probability', 'values': [40, 5, 5, 1]})
        for text in ('概率运算 40 5 5', '概率运算 40 5 5 1 2', '概率运算 a b c d', '概率运算'):
            self.assertEqual(parse_command(text)['op'], 'error', text)
            self.assertIn('格式：概率运算', parse_command(text)['message'])
        self.assertIn('概率运算 总数 目标数 抽取数 至少命中数', HELP)
        self.assertIn('例如：概率运算 40 5 5 1', HELP)
        self.assertIn('每日一卡：', HELP)
        self.assertIn('北京时间0点换', HELP)


class ProbabilityRoutingTests(unittest.TestCase):
    def test_group_and_private_reply_without_at(self):
        with temporary_service() as service:
            service.qq = BothQQ()
            service.group_commands = True
            asyncio.run(service.on_qq_event(event('概率运算 40 5 5 1')))
            self.assertIn('至少命中 1 张', service.qq.group_sent[0][1])
            reply = service.handle_text('10001', '概率运算 40 5 5 1', 'group:90000')
            self.assertEqual(reply, service.qq.group_sent[0][1])
            asyncio.run(service.on_qq_event({'message_type': 'private', 'user_id': 10001,
                                             'self_id': 3528698561, 'message': '概率运算 40 5 5 1'}))
            self.assertIn('至少命中 1 张', service.qq.sent[0][1])

    def test_unauthorized_user_is_refused(self):
        with temporary_service() as service:
            service.allow_all_users = False
            self.assertIn('没有使用权限', service.handle_text('999', '概率运算 40 5 5 1'))


if __name__ == '__main__':
    unittest.main()
