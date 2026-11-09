import unittest
from unittest.mock import AsyncMock

from bot.commands import parse_command
from bot.qq import OneBotClient


class NewCommandParseTests(unittest.TestCase):
    def test_bind_and_default_self_commands(self):
        self.assertEqual(parse_command('绑定mc'), {'op': 'bind_mc', 'player': None})
        self.assertEqual(parse_command('解绑mc'), {'op': 'unbind_mc'})
        self.assertEqual(parse_command('我的绑定'), {'op': 'my_binding'})
        self.assertEqual(parse_command('历史'), {'op': 'player_history', 'player': None})
        self.assertEqual(parse_command('当月历史'),
                         {'op': 'player_history', 'player': None, 'monthly': True})
        self.assertEqual(parse_command('构筑'), {'op': 'deck_build', 'player': None})
        self.assertEqual(parse_command('录像'), {'op': 'replays', 'player': None})
        self.assertEqual(parse_command('换备'), {'op': 'side_plan', 'player': None})
        self.assertEqual(parse_command('玩家'), {'op': 'player_profile', 'player': None})

    def test_spaced_subscription_names(self):
        self.assertEqual(parse_command('订阅 久 远'),
                         {'op': 'subscribe', 'player': '久 远', 'rank_top': None,
                          'notify': False, 'defaulted': True})
        self.assertEqual(parse_command('订阅 久 远 全部 推送'),
                         {'op': 'subscribe', 'player': '久 远', 'rank_top': None,
                          'notify': True, 'defaulted': False})
        self.assertEqual(parse_command('订阅 技 能 抽 取')['player'], '技 能 抽 取')
        self.assertEqual(parse_command('订阅 推送')['player'], '推送')


class ForwardNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_supports_image_then_falls_back_to_text(self):
        client = OneBotClient('ws://local', 'token', None)
        client.call = AsyncMock(side_effect=[{'retcode': 1}, {'retcode': 0}])
        ok = await client._send_forward(
            'send_private_forward_msg', 'user_id', 10001,
            [{'text': '标题', 'images': ['base64://abc']}], '123')
        self.assertTrue(ok)
        self.assertEqual(client.call.await_count, 2)
        payload = client.call.call_args.args[1]
        content = payload['messages'][0]['data']['content']
        self.assertEqual([segment['type'] for segment in content], ['text'])


if __name__ == '__main__':
    unittest.main()
