import asyncio
import unittest
from types import SimpleNamespace

from bot.qq import OneBotClient, group_command_text
from bot.card_search import CardSearch, CardReply
from test_groups import GroupQQ, group_event
from test_bot import temporary_service


def plain(text, **kwargs):
    event = group_event(text, **kwargs)
    event['message'] = [{'type': 'text', 'data': {'text': text}}]
    return event


class NoAtTests(unittest.TestCase):
    def test_only_recognizable_formatted_commands_trigger_without_at(self):
        for text in ['查卡 毁灭之黑', '选卡1', '选卡 1', '历史 花无十日红',
                     '录像 雨雨雨1', '订阅 雨雨雨1 前100 推送', '我的订阅', '帮助']:
            self.assertEqual(group_command_text(plain(text), True), text)
        for text in ['今天打什么', '雨雨雨1', '这个历史挺有意思', '1', '']:
            self.assertIsNone(group_command_text(plain(text), True))
        self.assertIsNone(group_command_text(group_event('查卡 白龙', mention='all'), True))
        self.assertIsNone(group_command_text(group_event('查卡 白龙', mention=10002), True))

    def test_unmentioned_selection_remains_scoped_to_group_and_user(self):
        with temporary_service() as service:
            service.group_commands = True
            service.qq = GroupQQ()
            service.card_search = CardSearch(service.data_dir, fetch=lambda _: {
                'result': [{'id': 59400890, 'cn_name': '毁灭之黑魔术师', 'text': {'desc': '效果'}}], 'next': 1})
            asyncio.run(service.on_qq_event(plain('查卡 毁灭之黑')))
            self.assertIn('候选', service.qq.group_sent[-1][1])
            asyncio.run(service.on_qq_event(plain('选卡1', user_id=10002)))
            self.assertIn('不存在', service.qq.group_sent[-1][1])
            asyncio.run(service.on_qq_event(plain('选卡1', group_id=90001)))
            self.assertIn('不存在', service.qq.group_sent[-1][1])
            asyncio.run(service.on_qq_event(plain('选卡1')))
            self.assertIsInstance(service.qq.group_sent[-1][1], CardReply)
            self.assertEqual(service.qq.sent, [])

    def test_history_subscription_replay_and_plain_chat(self):
        with temporary_service() as service:
            service.group_commands = True
            service.qq = GroupQQ()
            service.player_history = SimpleNamespace(handle=lambda player: '历史结果：' + player)
            asyncio.run(service.on_qq_event(plain('历史 花无十日红')))
            self.assertEqual(service.qq.group_sent[-1][1], '历史结果：花无十日红')
            asyncio.run(service.on_qq_event(plain('订阅 雨雨雨1 前100 静默')))
            self.assertIsNotNone(service.store.get_subscription('10001', '雨雨雨1'))
            asyncio.run(service.on_qq_event(plain('录像 雨雨雨1')))
            self.assertIn('/s/', service.qq.group_sent[-1][1])
            count = len(service.qq.group_sent)
            asyncio.run(service.on_qq_event(plain('普通聊天')))
            self.assertEqual(len(service.qq.group_sent), count)

    def test_group_switch_still_disables_plain_commands(self):
        with temporary_service() as service:
            service.qq = GroupQQ()
            service.group_commands = False
            asyncio.run(service.on_qq_event(plain('帮助')))
            self.assertEqual(service.qq.group_sent, [])
            service.group_commands = True
            service.group_without_at = False
            asyncio.run(service.on_qq_event(plain('帮助')))
            self.assertEqual(service.qq.group_sent, [])


class NoAtTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_delivers_formatted_commands_without_at(self):
        seen = []
        async def handler(event):
            seen.append(event)
        client = OneBotClient('ws://local', 'token', handler, accept_group_messages=True,
                              allow_unmentioned_commands=True)
        await client._dispatch(plain('查卡 白龙', message_id=1))
        await client._dispatch(plain('选卡1', message_id=2))
        await client._dispatch(plain('普通聊天', message_id=3))
        await client._dispatch(group_event('查卡 白龙', message_id=4, mention='all'))
        self.assertEqual(len(seen), 2)
