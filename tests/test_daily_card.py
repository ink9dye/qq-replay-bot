# -*- coding: utf-8 -*-
"""每日一卡：按 QQ 号 + 北京日期固定一张，跨群聊/私聊、跨重启都不变。"""
import asyncio
import itertools
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bot.card_extras import CardExtras
from bot.card_search import CardReply, CardSearch
from bot.commands import parse_command
from bot.daily_card import DailyCards
from bot.help import HELP
from test_bot import temporary_service
from test_groups import GroupQQ, group_event


CARD = {'id': 14558127, 'cid': 12950, 'cn_name': '灰流丽', 'jp_name': '灰流うらら',
        'data': {'type': 33}, 'text': {'types': '怪兽', 'desc': '这个卡名的效果一回合一次。'}}
OTHER = {'id': 23434538, 'cid': 9455, 'cn_name': '增殖的G', 'data': {'type': 33},
         'text': {'types': '怪兽', 'desc': '抽卡'}}
RELEASE = '<i>OCG</i> <span class="pack">TEST-JP001 (2020-04-01)</span>'


class FakeCatalog:
    def load(self):
        return {'12950': CARD, '9455': OTHER}


def make_extras(directory, today, choose):
    search = CardSearch(Path(directory), lambda path: {}, release_fetch=lambda _: RELEASE)
    return CardExtras(Path(directory), search, FakeCatalog(), today=today, choose=choose)


class DailyCardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DailyCards(self.temp.name)
        self.calls = []

    def choose(self, card):
        def pick():
            self.calls.append(card['id'])
            return card
        return pick

    def rows(self):
        with sqlite3.connect(self.store.path) as db:
            return db.execute('SELECT user_id, day FROM daily_cards ORDER BY day, user_id').fetchall()

    def test_same_qq_and_day_is_stable_even_for_a_new_store_instance(self):
        day = date(2026, 11, 7)
        first = self.store.get('10001', day, self.choose(CARD))
        again = DailyCards(self.temp.name).get('10001', day, self.choose(OTHER))
        self.assertEqual((first['id'], again['id']), (CARD['id'], CARD['id']))
        self.assertEqual(self.calls, [CARD['id']])

    def test_qq_number_and_day_are_both_part_of_the_key(self):
        self.assertEqual(self.store.get('10001', date(2026, 11, 7), self.choose(CARD))['id'], CARD['id'])
        self.assertEqual(self.store.get('10002', date(2026, 11, 7), self.choose(OTHER))['id'], OTHER['id'])
        self.assertEqual(self.store.get('10001', date(2026, 11, 8), self.choose(OTHER))['id'], OTHER['id'])
        self.assertEqual(len(self.calls), 3)

    def test_only_today_and_yesterday_are_kept(self):
        self.store.get('10001', date(2026, 11, 5), self.choose(CARD))
        self.store.get('10001', date(2026, 11, 6), self.choose(CARD))
        self.store.get('10001', date(2026, 11, 7), self.choose(CARD))
        self.assertEqual(self.rows(), [('10001', '2026-11-06'), ('10001', '2026-11-07')])

    def test_failed_draw_is_not_remembered(self):
        def broken():
            raise ValueError('catalog unavailable')

        with self.assertRaises(ValueError):
            self.store.get('10001', date(2026, 11, 7), broken)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.get('10001', date(2026, 11, 7), self.choose(CARD))['id'], CARD['id'])

    def test_conversation_keys_are_rejected_instead_of_creating_rows(self):
        for user in ('group:90000', '', '0', '-1', '１０００１'):
            with self.assertRaises(ValueError, msg=user):
                self.store.get(user, date(2026, 11, 7), self.choose(CARD))
        self.assertFalse(self.store.path.exists())


class DailyCardReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.day = [date(2026, 11, 7)]

    def extras(self, choose, today=None):
        return make_extras(self.temp.name, today or (lambda: self.day[0]), choose)

    def test_daily_card_is_the_random_card_format_plus_a_date_header(self):
        extras = self.extras(lambda cards: CARD)
        reply = extras.handle('10001', 'daily_card')
        detail = extras.search._reply_detail(CARD)
        self.assertIsInstance(reply, CardReply)
        self.assertEqual(str(reply), '每日一卡｜2026-11-07（北京时间）\n\n' + detail)
        self.assertEqual(reply.image_url, detail.image_url)
        self.assertIn('灰流丽', reply)
        self.assertIn('OCG TEST-JP001 (2020-04-01)', reply)
        self.assertIn('灰流丽', extras.handle('10001', 'random'))

    def test_same_qq_same_day_never_redraws_and_restart_keeps_the_card(self):
        first = self.extras(lambda cards: CARD).handle('10001', 'daily_card')
        again = self.extras(lambda cards: OTHER).handle('10001', 'daily_card')
        self.assertEqual(first, again)
        self.assertIn('灰流丽', again)
        self.assertNotIn('增殖的G', again)

    def test_other_qq_and_the_next_day_get_their_own_card(self):
        draws = itertools.cycle([CARD, OTHER])
        extras = self.extras(lambda cards: next(draws))
        self.assertIn('灰流丽', extras.handle('10001', 'daily_card'))
        self.assertIn('增殖的G', extras.handle('10002', 'daily_card'))
        self.day[0] = date(2026, 11, 8)
        self.assertIn('灰流丽', extras.handle('10001', 'daily_card'))
        self.assertIn('2026-11-08', extras.handle('10001', 'daily_card'))

    def test_catalog_failure_keeps_the_message_actionable_and_retry_works(self):
        draws = iter([ValueError('catalog unavailable'), CARD])

        def choose(cards):
            result = next(draws)
            if isinstance(result, Exception):
                raise result
            return result

        extras = self.extras(choose)
        self.assertIn('每日一卡暂时无法获取', extras.handle('10001', 'daily_card'))
        self.assertIn('灰流丽', extras.handle('10001', 'daily_card'))

    def test_command_parsing_and_help_list_the_new_command(self):
        self.assertEqual(parse_command('每日一卡'), {'op': 'daily_card'})
        self.assertEqual(parse_command(' 每日一卡 '), {'op': 'daily_card'})
        self.assertEqual(parse_command('每日一卡 灰')['op'], 'error')
        self.assertIn('每日一卡：随机一卡按QQ号固定一天', HELP)
        self.assertIn('北京时间0点换', HELP)


class BothQQ(GroupQQ):
    """群聊 + 私聊都要能发（私聊合并转发在每日一卡里用不到，只需可调用）。"""

    async def send_private_forward(self, user_id, cards, self_id=None):
        return True


class DailyCardRoutingTests(unittest.TestCase):
    """群聊/私聊都把 QQ 号交给 card_extras；免@只在群内生效。"""

    def test_group_and_private_both_bind_to_the_qq_number(self):
        with temporary_service() as service:
            calls = []

            class Recorder:
                def handle(self, user, action, value=''):
                    calls.append((str(user), action, value))
                    return '每日一卡｜2026-11-07（北京时间）\n\n灰流丽'

                def is_guess_answer(self, *args):
                    return False

                def pending_reveals(self):
                    return []

            service.card_extras = Recorder()
            service.qq = BothQQ()
            service.group_commands = True
            asyncio.run(service.on_qq_event(group_event('每日一卡')))
            asyncio.run(service.on_qq_event(group_event('每日一卡', group_id=90001)))
            asyncio.run(service.on_qq_event({'message_type': 'private', 'user_id': 10001,
                                             'self_id': 3528698561, 'message': '每日一卡'}))
            self.assertEqual(calls, [('10001', 'daily_card', '')] * 3)
            self.assertEqual([item[0] for item in service.qq.group_sent], ['90000', '90001'])

    def test_group_message_without_at_is_still_a_command(self):
        with temporary_service() as service:
            service.qq = GroupQQ()
            service.group_commands = True
            service.card_extras = type('Stub', (), {
                'handle': lambda self, user, action, value='': '每日一卡｜2026-11-07（北京时间）',
                'is_guess_answer': lambda self, *args: False,
                'pending_reveals': lambda self: []})()
            event = {'post_type': 'message', 'message_type': 'group', 'self_id': 3528698561,
                     'user_id': 10001, 'group_id': 90000, 'message_id': 7,
                     'message': [{'type': 'text', 'data': {'text': '每日一卡'}}]}
            asyncio.run(service.on_qq_event(event))
            self.assertIn('每日一卡', service.qq.group_sent[0][1])

    def test_end_to_end_group_and_private_share_the_same_card_and_image(self):
        with tempfile.TemporaryDirectory() as directory:
            extras = make_extras(directory, lambda: date(2026, 11, 7), lambda cards: CARD)
            with temporary_service() as service:
                service.card_extras = extras
                service.qq = BothQQ()
                service.group_commands = True
                asyncio.run(service.on_qq_event(group_event('每日一卡')))
                asyncio.run(service.on_qq_event({'message_type': 'private', 'user_id': 10001,
                                                 'self_id': 3528698561, 'message': '每日一卡'}))
                group_reply = service.qq.group_sent[0][1]
                private_reply = service.qq.sent[0][1]
                self.assertIn('每日一卡｜2026-11-07（北京时间）', group_reply)
                self.assertIn('灰流丽', group_reply)
                self.assertTrue(group_reply.image_url)
                self.assertEqual(str(group_reply), str(private_reply))
                self.assertEqual(group_reply.image_url, private_reply.image_url)

    def test_unauthorized_user_is_refused_like_other_commands(self):
        with temporary_service() as service:
            service.allow_all_users = False
            service.help_text = lambda user, conversation: '帮助'
            self.assertIn('没有使用权限', service.handle_text('999', '每日一卡'))
            self.assertIn('没有使用权限', service.handle_text('999', '随机一卡'))


if __name__ == '__main__':
    unittest.main()
