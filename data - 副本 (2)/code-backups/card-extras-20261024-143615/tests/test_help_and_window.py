import asyncio
import json
import os
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

from bot.commands import parse_command
from bot.help import HELP_ITEMS, help_cards
from bot.qq import OneBotClient
from bot.replay_window import REPLAY_WINDOW_SECONDS
from bot.watch_core import ReplayWriter, _rank_cache
from test_bot import link_token, temporary_service
from test_groups import GroupQQ, group_event


class ForwardQQ(GroupQQ):
    def __init__(self, forward_ok=True):
        super().__init__()
        self.forward_ok = forward_ok
        self.forwards = []

    async def send_private_forward(self, user_id, cards, self_id):
        self.forwards.append((str(user_id), cards, str(self_id)))
        return self.forward_ok


class HelpTests(unittest.TestCase):
    def test_private_help_is_a_forward_with_actionable_templates_and_examples(self):
        with temporary_service() as service:
            service.qq = ForwardQQ()
            asyncio.run(service.on_qq_event({'message_type': 'private', 'user_id': 10001,
                                            'self_id': 3528698561, 'message': '帮助'}))
            receiver, cards, sender = service.qq.forwards[0]
            self.assertEqual((receiver, sender), ('10001', '3528698561'))
            self.assertEqual(service.qq.sent, [])
            self.assertEqual(cards, help_cards())
            for card in cards[1:-1]:
                for label in ['模板：', '作用：', '例子（可照发）：']:
                    self.assertIn(label, card)
            for _, _, _, example, _ in HELP_ITEMS[:-1]:
                self.assertNotEqual(parse_command(example)['op'], 'error')
            self.assertIn('保存满7天', cards[-1])
            self.assertIn('5小时', cards[-1])
            self.assertEqual(service.store.list_subscriptions(), [])

    def test_group_help_is_forwarded_privately_and_group_only_gets_receipt(self):
        with temporary_service() as service:
            service.qq = ForwardQQ()
            service.group_commands = True
            asyncio.run(service.on_qq_event(group_event('帮助')))
            self.assertEqual(service.qq.forwards[0][0], '10001')
            self.assertEqual(len(service.qq.notices), 1)
            self.assertIn('已私发', service.qq.notices[0][2])
            self.assertNotIn('模板', service.qq.notices[0][2])

    def test_forward_failure_falls_back_to_complete_labeled_text(self):
        with temporary_service() as service:
            service.qq = ForwardQQ(forward_ok=False)
            asyncio.run(service.on_qq_event({'message_type': 'private', 'user_id': 10001,
                                            'self_id': 3528698561, 'message': '帮助'}))
            self.assertIn('合并转发帮助暂时发送失败', service.qq.sent[0][1])
            self.assertIn('例子（可照发）：', service.qq.sent[0][1])


class ForwardTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_nodes_use_tool_identity_and_text_segments_and_only_private_receiver(self):
        client = OneBotClient('ws://local', 'token', None)
        client.call = AsyncMock(return_value={'retcode': 0})
        self.assertTrue(await client.send_private_forward(10001, help_cards(), 3528698561))
        action, payload = client.call.call_args.args
        self.assertEqual(action, 'send_private_forward_msg')
        self.assertEqual(payload['user_id'], 10001)
        self.assertNotIn('group_id', payload)
        self.assertGreater(client.call.call_args.kwargs['timeout'], client.timeout)
        self.assertEqual([node['data']['content'][0]['data']['text'] for node in payload['messages']], help_cards())
        self.assertTrue(all(node['data']['uin'] == '3528698561' for node in payload['messages']))

    async def test_missing_self_id_is_resolved_from_actual_login_info(self):
        client = OneBotClient('ws://local', 'token', None)
        client.call = AsyncMock(side_effect=[{'retcode': 0, 'data': {'user_id': 3528698561}}, {'retcode': 0}])
        self.assertTrue(await client.send_private_forward(10001, ['帮助']))
        self.assertEqual(client.call.call_args_list[0].args[0], 'get_login_info')
        self.assertEqual(client.call.call_args_list[1].args[1]['messages'][0]['data']['uin'], '3528698561')


def save_replay(service, player, body):
    _rank_cache[player], _rank_cache['对手'] = 12, 88
    writer = ReplayWriter(service.replay_root, [player])
    writer.on_saved = service.on_replay_saved
    writer.save(body, [player, '对手'])
    return service.store.replays_for_player(player)[-1]


class ReplayWindowTests(unittest.TestCase):
    def test_exact_seven_day_boundary_rechecks_live_link_directory_file_and_zip(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            start = service.clock.now
            old = save_replay(service, '甲', b'old-game')
            service.clock.now += REPLAY_WINDOW_SECONDS - 3600
            recent = save_replay(service, '甲', b'recent-game')
            self.assertEqual(len(service.visible_replays('10001', '甲')), 2)
            token = link_token(service.handle_text('10001', '录像 甲'))
            link = service.store.get_link(token)
            before, _, body = service.share.page(token, 'file', str(old['id']))
            self.assertEqual((before, body), (200, b'old-game'))
            service.clock.now = start + REPLAY_WINDOW_SECONDS
            self.assertEqual([r['id'] for r in service.visible_replays('10001', '甲')], [recent['id']])
            self.assertGreater(link['expires_at'], service.clock.now)
            status, _, body = service.share.page(token, 'dir')
            self.assertEqual(status, 200)
            self.assertNotIn(f'/file/{old["id"]}"'.encode(), body)
            self.assertIn(f'/file/{recent["id"]}"'.encode(), body)
            self.assertEqual(service.share.page(token, 'file', str(old['id']))[0], 410)
            self.assertEqual(service.share.page(token, 'file', str(recent['id']))[0], 200)
            status, _, body = service.share.page(token, 'zip')
            self.assertEqual(status, 200)
            with zipfile.ZipFile(BytesIO(body)) as archive:
                self.assertEqual([archive.read(name) for name in archive.namelist()], [b'recent-game'])
            token2 = link_token(service.handle_text('10001', '录像 甲'))
            self.assertEqual(service.store.get_link(token2)['replay_ids'], [recent['id']])
            self.assertEqual(len(list(service.replay_root.rglob('*.yrp'))), 2)
            self.assertEqual(service.store.get_link(token)['expires_at'], link['expires_at'])

    def test_queued_replay_older_than_a_week_is_skipped_without_push(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 推送')
            replay = save_replay(service, '甲', b'queued-game')
            service.clock.now += REPLAY_WINDOW_SECONDS
            service.qq = GroupQQ()
            self.assertEqual(asyncio.run(service.deliver()), 0)
            self.assertEqual(service.qq.sent, [])
            self.assertEqual(service.store.notification_status('10001', replay['id']), 'skipped')

    def test_cleanup_deletes_old_replays_and_decks(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            old = save_replay(service, '甲', b'old-game')
            deck_dir = service.deck_dir
            old_deck = deck_dir / 'old.ydk'
            old_deck.write_text('#main\n1\n', encoding='utf-8')
            old_time = service.clock.now - REPLAY_WINDOW_SECONDS - 60
            os.utime(old_deck, (old_time, old_time))
            service.clock.now += REPLAY_WINDOW_SECONDS + 60
            recent = save_replay(service, '甲', b'recent-game')
            recent_deck = deck_dir / 'recent.ydk'
            recent_deck.write_text('#main\n2\n', encoding='utf-8')
            os.utime(recent_deck, (service.clock.now, service.clock.now))
            self.assertEqual(len(service.store.replays_for_player('甲')), 2)
            removed = service.cleanup_old_files()
            self.assertEqual(removed, 2)
            self.assertEqual([r['id'] for r in service.store.replays_for_player('甲')], [recent['id']])
            self.assertFalse(old_deck.exists())
            self.assertTrue(recent_deck.exists())
            self.assertEqual(len(list(service.replay_root.rglob('*.yrp'))), 1)

    def test_multiple_players_and_users_keep_conditions_links_zip_and_switches_separate(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 前100 推送')
            service.handle_text('10001', '订阅 乙 全部 静默')
            service.handle_text('10002', '订阅 乙 前10 推送')
            a = save_replay(service, '甲', b'player-a')
            b = save_replay(service, '乙', b'player-b')
            first = link_token(service.handle_text('10001', '录像 甲'))
            second = link_token(service.handle_text('10001', '录像 乙'))
            self.assertEqual(service.store.get_link(first)['replay_ids'], [a['id']])
            self.assertEqual(service.store.get_link(second)['replay_ids'], [b['id']])
            self.assertNotEqual(first, second)
            self.assertEqual(service.share.page(first, 'file', str(b['id']))[0], 404)
            self.assertEqual(service.share.page(second, 'file', str(a['id']))[0], 404)
            for token, expected in [(first, b'player-a'), (second, b'player-b')]:
                _, _, body = service.share.page(token, 'zip')
                with zipfile.ZipFile(BytesIO(body)) as archive:
                    self.assertEqual([archive.read(name) for name in archive.namelist()], [expected])
            self.assertEqual(service.visible_replays('10002', '乙'), [])
            service.handle_text('10001', '静默 甲')
            self.assertEqual(service.store.get_subscription('10001', '乙')['rank_top'], None)
            self.assertTrue(service.store.get_subscription('10002', '乙')['notify'])
            service.handle_text('10001', '取消订阅 甲')
            self.assertIsNotNone(service.store.get_subscription('10001', '乙'))
            self.assertIsNotNone(service.store.get_subscription('10002', '乙'))

    def test_even_a_legacy_snapshot_cannot_mix_another_players_files(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            service.handle_text('10001', '订阅 乙 全部 静默')
            a = save_replay(service, '甲', b'a')
            b = save_replay(service, '乙', b'b')
            link = service.share.create_link('10001', '甲', [a['id'], b['id']], service.clock.now)
            token = link['token']
            self.assertEqual(service.share.page(token, 'file', str(b['id']))[0], 404)
            _, _, body = service.share.page(token, 'zip')
            with zipfile.ZipFile(BytesIO(body)) as archive:
                self.assertEqual([archive.read(name) for name in archive.namelist()], [b'a'])


if __name__ == '__main__':
    unittest.main()
