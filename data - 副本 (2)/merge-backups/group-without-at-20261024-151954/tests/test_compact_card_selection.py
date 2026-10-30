import asyncio
import unittest
from pathlib import Path

from bot.commands import parse_command
from bot.card_search import CardSearch, CardReply
from test_bot import temporary_service
from test_groups import GroupQQ, group_event


class SelectionTests(unittest.TestCase):
    def test_both_forms_select_the_same_candidate_without_hijacking_other_names(self):
        self.assertEqual(parse_command('选卡1'), parse_command('选卡 1'))
        self.assertEqual(parse_command(' 选卡12 '), {'op': 'card_select', 'index': '12'})
        self.assertEqual(parse_command('选卡12345')['op'], 'error')
        self.assertEqual(parse_command('选卡1号')['op'], 'unknown')
        self.assertEqual(parse_command('1')['op'], 'unknown')

    def test_group_followup_with_at_selects_same_users_candidate(self):
        with temporary_service() as service:
            service.group_commands = True
            service.qq = GroupQQ()
            result = {'result': [{'id': 59400890, 'cn_name': '毁灭之黑魔术师',
                                  'text': {'desc': '效果'}}], 'next': 1}
            service.card_search = CardSearch(service.data_dir, fetch=lambda _: result)
            asyncio.run(service.on_qq_event(group_event('查卡 毁灭之黑')))
            self.assertIn('群内每一步都需先@工具号', service.qq.group_sent[-1][1])
            asyncio.run(service.on_qq_event(group_event('选卡1', user_id=10002)))
            self.assertIn('不存在', service.qq.group_sent[-1][1])
            asyncio.run(service.on_qq_event(group_event('选卡1', message_id=3)))
            reply = service.qq.group_sent[-1][1]
            self.assertIsInstance(reply, CardReply)
            self.assertIn('毁灭之黑魔术师', reply)
            self.assertEqual(service.qq.sent, [])
