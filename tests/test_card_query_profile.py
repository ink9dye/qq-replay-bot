import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bot.card_search import CardReply, CardSearch
from bot.commands import parse_command
from bot.service import BotService
from bot.card_usage_pie import resolve_card


def card(code, name):
    return {'id': code, 'cn_name': name, 'text': {'desc': '效果'}}


class CardQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 100
        self.rows = [card(123, '测试甲'), card(456, '测试乙')]
        self.search = CardSearch(Path(self.tmp.name),
                                 fetch=lambda _: {'result': self.rows, 'next': 0},
                                 clock=lambda: self.now)
        self.patcher = patch('bot.card_search.card_image_payload', return_value='base64://image')
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.service = BotService.__new__(BotService)
        self.service.card_search = self.search
        self.service.write_log = Mock()
        self.service._statistics_command_lock = None
        self.service._card_usage_reply = Mock(return_value=CardReply('使用量', 'base64://usage'))
        self.service._card_flow_reply = Mock(return_value=CardReply('流向', image_urls=['base64://flow']))
        self.service._card_winrate_reply = Mock(return_value='胜率')

    def test_parse(self):
        self.assertEqual(parse_command('卡片 测试 卡'), {'op': 'card_profile', 'name': '测试 卡'})
        self.assertEqual(parse_command('卡片')['op'], 'error')
        self.assertEqual(parse_command('卡片 ' + '卡' * 101)['op'], 'error')

    def test_each_stat_resumes_after_selection_with_exact_code(self):
        for purpose in ('card_usage', 'card_flow', 'card_winrate'):
            with self.subTest(purpose=purpose):
                reply = self.service._card_query_reply('group:1:u', 'search', '测试', purpose)
                self.assertIn('候选', reply)
                self.service._card_query_reply('group:1:u', 'select', '2')
                getattr(self.service, '_' + purpose + '_reply').assert_called_with('456')

    def test_unique_hit_profile_has_all_sections_and_images(self):
        self.rows = [card(123, '测试甲')]
        reply = self.service._card_query_reply('u', 'search', '测试', 'card_profile')
        self.assertEqual(len(reply.forward_cards), 5)
        self.assertEqual(reply.forward_cards[1]['images'], ['base64://image'])
        self.assertEqual(reply.forward_cards[2]['images'], ['base64://usage'])
        self.assertEqual(reply.forward_cards[3]['images'], ['base64://flow'])
        self.service._card_winrate_reply.assert_called_once_with('123')

    def test_partial_failure_keeps_other_sections(self):
        self.rows = [card(123, '测试甲')]
        self.service._card_flow_reply.side_effect = ValueError('broken')
        reply = self.service._card_query_reply('u', 'search', '测试', 'card_profile')
        self.assertIn('读取失败', reply)
        self.assertIn('胜率', reply)

    def test_purpose_survives_paging_but_isolated_and_reset_by_new_search(self):
        self.rows = [card(i, '测试' + str(i)) for i in range(1, 9)]
        self.service._card_query_reply('a', 'search', '测试', 'card_usage')
        self.service._card_query_reply('b', 'search', '测试', 'card_flow')
        self.service._card_query_reply('a', 'next')
        self.service._card_query_reply('a', 'select', '6')
        self.service._card_usage_reply.assert_called_once_with('6')
        self.service._card_query_reply('b', 'select', '1')
        self.service._card_flow_reply.assert_called_once_with('1')
        self.service._card_query_reply('a', 'search', '新搜索')
        reply = self.service._card_query_reply('a', 'select', '1')
        self.assertIsNone(reply.purpose)
        self.assertTrue(reply.selected_card)
        self.now += 301
        self.assertIn('过期', self.service._card_query_reply('b', 'select', '1'))

    def test_local_fallback_retains_profile_purpose(self):
        self.search._request = Mock(side_effect=OSError('offline'))
        with patch('bot.statistics_local_cards.local_index', return_value={
                '123': card(123, '测试甲'), '456': card(456, '测试乙')}):
            self.service._card_query_reply('a', 'search', '测试', 'card_profile')
            reply = self.service._card_query_reply('a', 'select', '2')
        self.assertEqual(len(reply.forward_cards), 5)
        self.service._card_usage_reply.assert_called_once_with('456')

    def test_numeric_resolution_does_not_change_selected_identity(self):
        with patch('bot.card_usage_pie._card_index', return_value={'123': card(123, '测试甲')}):
            self.assertEqual(resolve_card(self.tmp.name, '123'), (123, '测试甲'))
            self.assertEqual(resolve_card(self.tmp.name, '456'), (456, '#456'))

    def test_slow_job_sends_one_forward_record(self):
        self.rows = [card(123, '测试甲')]
        reply = self.service._card_query_reply('a', 'search', '测试', 'card_profile')
        self.service.handle_text = Mock(return_value=reply)
        send, forward = AsyncMock(), AsyncMock(return_value=True)
        asyncio.run(self.service._slow_text_job('a', '卡片 测试', '', 'a', send, forward, 'bot'))
        forward.assert_awaited_once_with('a', reply.forward_cards, 'bot')
        send.assert_not_awaited()

    def test_forward_failure_falls_back_with_images(self):
        self.rows = [card(123, '测试甲')]
        reply = self.service._card_query_reply('a', 'search', '测试', 'card_profile')
        self.service.handle_text = Mock(return_value=reply)
        send, forward = AsyncMock(), AsyncMock(side_effect=OSError('unsupported'))
        asyncio.run(self.service._slow_text_job('a', '卡片 测试', '', 'a', send, forward, 'bot'))
        self.assertEqual(send.await_count, 5)
        self.assertEqual(send.await_args_list[1].args[1].image_urls, ['base64://image'])

    def test_command_dispatch_and_bare_number_resume(self):
        from test_bot import temporary_service
        with temporary_service() as service:
            service.card_search = self.search
            service._card_usage_reply = Mock(return_value='选定后的统计')
            self.assertIn('候选', service.handle_text('10001', '使用量 测试', 'group:1'))
            self.assertEqual(service.handle_text('10001', '2', 'group:1'), '选定后的统计')
            service._card_usage_reply.assert_called_once_with('456')


if __name__ == '__main__':
    unittest.main()
