import asyncio
import unittest

from bot.card_extras import CardExtras
from bot.card_search import CardReply
from bot.qq import OneBotClient
from test_bot import temporary_service
from test_card_extras import FakeCatalog
from test_groups import GroupQQ, group_event
from test_group_without_at import plain


def extras(service, now):
    return CardExtras(service.data_dir, service.card_search, FakeCatalog(), clock=lambda: now[0],
                      choose=lambda cards: cards[0])


class DirectGuessTests(unittest.TestCase):
    def test_private_direct_name_wrong_known_card_alias_and_expiry(self):
        with temporary_service() as service:
            now = [100]
            service.card_extras = extras(service, now)
            self.assertIn('直接发送', service.handle_text('10001', '猜卡'))
            self.assertIn('还没猜对', service.handle_text('10001', '增殖的G'))
            self.assertIn('猜对', service.handle_text('10001', '灰'))
            self.assertFalse(service.card_extras.is_guess_answer('10001', '灰流丽'))
            service.handle_text('10001', '猜卡')
            now[0] += 300
            self.assertFalse(service.card_extras.is_guess_answer('10001', '灰流丽'))

    def test_group_answers_without_at_but_not_chat_or_another_groups_game(self):
        with temporary_service() as service:
            now = [100]
            service.card_extras = extras(service, now)
            service.qq = GroupQQ()
            service.group_commands = True
            asyncio.run(service.on_qq_event(plain('猜卡')))
            count = len(service.qq.group_sent)
            for event in [plain('这题有点难'), plain('灰流丽', group_id=90001),
                          group_event('灰流丽', mention=10002)]:
                asyncio.run(service.on_qq_event(event))
            self.assertEqual(len(service.qq.group_sent), count)
            asyncio.run(service.on_qq_event(plain('灰流丽', user_id=10002)))
            self.assertIsInstance(service.qq.group_sent[-1][1], CardReply)
            self.assertIn('猜对', service.qq.group_sent[-1][1])
            count = len(service.qq.group_sent)
            asyncio.run(service.on_qq_event(plain('灰流丽')))
            self.assertEqual(len(service.qq.group_sent), count)

    def test_card_password_and_other_translation(self):
        with temporary_service() as service:
            service.card_extras = extras(service, [100])
            for answer in ['14558127', '灰流うらら']:
                service.handle_text('10001', '猜卡')
                self.assertIn('猜对', service.handle_text('10001', answer))


class DirectGuessTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_forwards_card_name_only_while_group_question_is_active(self):
        with temporary_service() as service:
            service.card_extras = extras(service, [100])
            service.qq = GroupQQ()
            service.group_commands = True
            client = OneBotClient('ws://local', 'token', service.on_qq_event,
                accept_group_messages=True, allow_unmentioned_commands=True,
                group_extra_accept=service.accept_group_guess)
            await client._dispatch(plain('灰流丽', message_id=1))
            self.assertEqual(service.qq.group_sent, [])
            await client._dispatch(plain('猜卡', message_id=2))
            await client._dispatch(plain('灰流丽', user_id=10002, message_id=3))
            self.assertIn('猜对', service.qq.group_sent[-1][1])
