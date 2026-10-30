import asyncio
import unittest

from bot.qq import OneBotClient, group_command_text
from bot.share import ShareServer
from bot.watch_core import ReplayWriter, _rank_cache
from test_bot import FakeQQ, temporary_service


def group_event(text, user_id=10001, message_id=1, group_id=90000, mention=3528698561):
    return {
        'post_type': 'message', 'message_type': 'group', 'self_id': 3528698561,
        'user_id': user_id, 'group_id': group_id, 'message_id': message_id,
        'message': [
            {'type': 'at', 'data': {'qq': str(mention)}},
            {'type': 'text', 'data': {'text': ' ' + text}},
        ],
    }


class GroupQQ(FakeQQ):
    def __init__(self):
        super().__init__()
        self.notices = []
        self.group_sent = []
        self.group_fail = False

    async def send_group(self, group_id, text):
        self.group_sent.append((str(group_id), text))
        return not self.group_fail

    async def send_group_forward(self, group_id, cards, self_id=None):
        self.group_sent.append((str(group_id), cards))
        return not self.group_fail

    async def send_group_notice(self, group_id, user_id, text):
        self.notices.append((str(group_id), str(user_id), text))
        return True


class GroupTests(unittest.TestCase):
    def test_only_at_tool_triggers_and_cq_string_is_supported(self):
        self.assertEqual(group_command_text(group_event('订阅 "亚翁" 前100 推送')), '订阅 "亚翁" 前100 推送')
        self.assertEqual(group_command_text(group_event('')), '帮助')
        self.assertIsNone(group_command_text(group_event('帮助', mention='all')))
        self.assertIsNone(group_command_text(group_event('帮助', mention=10002)))
        event = group_event('帮助')
        event['message'] = [{'type': 'text', 'data': {'text': '订阅 亚翁 前100 推送'}}]
        self.assertIsNone(group_command_text(event))
        event['message'] = '[CQ:at,qq=3528698561] 订阅 亚翁 前100 推送'
        self.assertEqual(group_command_text(event), '订阅 亚翁 前100 推送')
        event.pop('self_id')
        self.assertIsNone(group_command_text(event))

    def test_group_commands_create_personal_subscription_and_reply_in_group(self):
        with temporary_service() as service:
            service.group_commands = True
            service.qq = GroupQQ()
            asyncio.run(service.on_qq_event(group_event('订阅 "亚翁" 前100 推送')))
            sub = service.store.get_subscription('10001', '亚翁')
            self.assertEqual(sub['rank_top'], 100)
            self.assertTrue(sub['notify'])
            self.assertIsNone(service.store.get_subscription('90000', '亚翁'))
            self.assertEqual(service.qq.group_sent[0][0], '90000')
            self.assertIn('已订阅', service.qq.group_sent[0][1])
            asyncio.run(service.on_qq_event(group_event('录像 亚翁', message_id=2)))
            self.assertIn('/s/', service.qq.group_sent[-1][1])
            self.assertTrue(all('/s/' not in text and '亚翁' not in text for _, _, text in service.qq.notices))
            asyncio.run(service.on_qq_event(group_event('录像 亚翁', user_id=10002, message_id=3)))
            self.assertEqual(service.qq.group_sent[-1][0], '90000')
            self.assertIn('默认仅查看该玩家对手排名前100', service.qq.group_sent[-1][1])
            self.assertEqual(service.qq.sent, [])
            asyncio.run(service.on_qq_event(group_event('取消订阅 亚翁', user_id=10002, message_id=4)))
            self.assertIsNotNone(service.store.get_subscription('10001', '亚翁'))

    def test_failed_group_reply_retains_subscription_and_never_falls_back_to_dm(self):
        with temporary_service() as service:
            service.group_commands = True
            service.qq = GroupQQ()
            service.qq.group_fail = True
            asyncio.run(service.on_qq_event(group_event('订阅 亚翁 全部 推送')))
            self.assertIsNotNone(service.store.get_subscription('10001', '亚翁'))
            asyncio.run(service.on_qq_event(group_event('录像 亚翁', message_id=2)))
            self.assertIn('/s/', service.qq.group_sent[-1][1])
            self.assertEqual(service.qq.sent, [])
            self.assertEqual(service.qq.notices, [])

    def test_no_at_or_disabled_feature_does_not_change_subscriptions(self):
        with temporary_service() as service:
            service.qq = GroupQQ()
            asyncio.run(service.on_qq_event(group_event('订阅 亚翁 全部 推送')))
            service.group_commands = True
            asyncio.run(service.on_qq_event(group_event('订阅 亚翁 全部 推送', mention='all')))
            self.assertEqual(service.store.list_subscriptions(), [])
            self.assertEqual(service.qq.sent, [])
            self.assertEqual(service.qq.notices, [])

    def test_replay_after_group_subscription_is_pushed_only_to_subscriber(self):
        with temporary_service() as service:
            service.group_commands = True
            service.qq = GroupQQ()
            asyncio.run(service.on_qq_event(group_event('订阅 亚翁 前100 推送')))
            service.qq.sent.clear()
            service.qq.notices.clear()
            service.qq.group_sent.clear()
            _rank_cache['对手'] = 50
            _rank_cache['亚翁'] = 3
            writer = ReplayWriter(service.replay_root, ['亚翁'])
            writer.on_saved = service.on_replay_saved
            writer.save(b'group-subscribed-replay', ['亚翁', '对手'])
            self.assertEqual(asyncio.run(service.deliver()), 1)
            self.assertEqual(service.qq.sent[0][0], '10001')
            self.assertIn('/s/', service.qq.sent[0][1])
            self.assertEqual(service.qq.notices, [])
            self.assertEqual(service.qq.group_sent, [])

    def test_share_port_zero_is_ephemeral_and_duplicate_server_is_rejected(self):
        with temporary_service() as service:
            self.assertEqual(service.share.bind_port, 0)
            service.share_server.start()
            self.assertGreater(service.share.bind_port, 0)
            duplicate = ShareServer(service.share)
            try:
                with self.assertRaises(OSError):
                    duplicate.start()
            finally:
                duplicate.stop()


class GroupTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_gates_groups_and_deduplicates_by_conversation(self):
        seen = []

        async def handler(event):
            seen.append((event['message_type'], event.get('group_id')))

        client = OneBotClient('ws://local', 'token', handler, accept_group_messages=True)
        event = group_event('帮助')
        await client._dispatch(event)
        await client._dispatch(event)
        await client._dispatch(group_event('帮助', group_id=90001))
        await client._dispatch(group_event('帮助', message_id=2, mention='all'))
        await client._dispatch(group_event('帮助', message_id=3, user_id=3528698561))
        await client._dispatch({**event, 'message_type': 'private', 'message': '帮助', 'group_id': None})
        self.assertEqual(seen, [('group', 90000), ('group', 90001), ('private', None)])

    async def test_private_send_never_includes_group_id(self):
        calls = []
        client = OneBotClient('ws://local', 'token', None)
        client.connected = True

        async def call(action, params):
            calls.append((action, params))
            return {'retcode': 0}

        client.call = call
        await client.send_private(10001, 'https://example.org/s/private-link')
        self.assertEqual(calls[0][0], 'send_private_msg')
        self.assertNotIn('group_id', calls[0][1])
        self.assertTrue(calls[0][1]['auto_escape'])
        await client.send_group_notice(90000, 10001, '处理结果已私发')
        self.assertEqual(calls[1][0], 'send_group_msg')
        self.assertEqual(calls[1][1]['message'][0], {'type': 'at', 'data': {'qq': '10001'}})


if __name__ == '__main__':
    unittest.main()
