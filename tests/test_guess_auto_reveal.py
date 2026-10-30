import asyncio
import unittest

from bot.card_extras import CardExtras
from bot.card_search import CardReply
from test_card_extras import FakeCatalog
from test_bot import temporary_service
from test_groups import GroupQQ


def initialize(service):
    now = [100]
    service.card_extras = CardExtras(service.data_dir, service.card_search, FakeCatalog(),
                                    clock=lambda: now[0], choose=lambda cards: cards[0])
    service.qq = GroupQQ()
    service.group_commands = True
    return now


class AutoRevealTests(unittest.TestCase):
    def test_reveal_at_exactly_five_minutes_to_original_group_and_private_once(self):
        with temporary_service() as service:
            now = initialize(service)
            service.handle_text('10001', '猜卡', 'group:90000')
            service.handle_text('10002', '猜卡')
            now[0] += 299.99
            self.assertEqual(asyncio.run(service.deliver_guess_reveals()), 0)
            now[0] += .01
            self.assertEqual(asyncio.run(service.deliver_guess_reveals()), 2)
            self.assertEqual(service.qq.group_sent[0][0], '90000')
            self.assertEqual(service.qq.sent[0][0], '10002')
            self.assertIsInstance(service.qq.group_sent[0][1], CardReply)
            self.assertIn('自动公布答案', service.qq.group_sent[0][1])
            self.assertEqual(asyncio.run(service.deliver_guess_reveals()), 0)

    def test_correct_answer_or_manual_reveal_cancels_timeout_message(self):
        with temporary_service() as service:
            now = initialize(service)
            service.handle_text('10001', '猜卡', 'group:90000')
            service.handle_text('10002', '灰流丽', 'group:90000')
            service.handle_text('10001', '猜卡')
            service.handle_text('10001', '揭晓猜卡')
            now[0] += 300
            self.assertEqual(asyncio.run(service.deliver_guess_reveals()), 0)
            self.assertEqual(service.qq.group_sent, [])
            self.assertEqual(service.qq.sent, [])

    def test_other_commands_do_not_delete_expired_answer_before_delivery(self):
        with temporary_service() as service:
            now = initialize(service)
            service.handle_text('10001', '猜卡')
            now[0] += 300
            service.card_extras.handle('10002', 'random')
            self.assertEqual(asyncio.run(service.deliver_guess_reveals()), 1)
            self.assertEqual(service.qq.sent[0][0], '10001')

    def test_disconnect_and_send_failure_retain_pending_answer(self):
        with temporary_service() as service:
            now = initialize(service)
            service.handle_text('10001', '猜卡', 'group:90000')
            now[0] += 300
            service.qq.connected = False
            self.assertEqual(asyncio.run(service.deliver_guess_reveals()), 0)
            service.qq.connected = True
            service.qq.group_fail = True
            self.assertEqual(asyncio.run(service.deliver_guess_reveals()), 0)
            self.assertEqual(len(service.card_extras.pending_reveals()), 1)
            service.qq.group_fail = False
            self.assertEqual(asyncio.run(service.deliver_guess_reveals()), 1)
            self.assertEqual(service.card_extras.pending_reveals(), [])

    def test_parallel_delivery_does_not_duplicate_timeout_reply(self):
        with temporary_service() as service:
            now = initialize(service)
            service.handle_text('10001', '猜卡')
            now[0] += 300
            async def together():
                return await asyncio.gather(service.deliver_guess_reveals(), service.deliver_guess_reveals())
            self.assertEqual(sum(asyncio.run(together())), 1)
            self.assertEqual(len(service.qq.sent), 1)
