import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from bot.commands import parse_command
from bot.player_history import CST, PlayerHistory, monthly_stats, chart_png
from bot.qq import OneBotClient
from test_bot import temporary_service
from test_groups import GroupQQ, group_event

NOW = datetime(2026, 10, 24, 15, tzinfo=CST)


def game(player='花无十日红', side='a', when='2026-10-01T00:00:00Z', before=1000, after=1012, winner=None, kind='athletic'):
    return {'usernamea': player if side == 'a' else '对手',
            'usernameb': player if side == 'b' else '对手',
            'type': kind, 'start_time': when, 'end_time': when,
            'pta_ex': before, 'ptb_ex': before, 'pta': after, 'ptb': after,
            'winner': player if winner is None else winner}


class HistoryTests(unittest.TestCase):
    def test_cst_month_boundary_both_sides_draw_and_chronology(self):
        rows = [game(when='2026-09-30T16:00:00Z'),
                game(side='b', when='2026-10-03T00:00:00Z', before=1012, after=1010, winner='对手'),
                game(when='2026-10-04T00:00:00Z', before=1010, after=1010, winner='none'),
                game(when='2026-09-30T15:59:59Z'), game(kind='entertain'), game(player='别人')]
        stats = monthly_stats('花无十日红', rows + [rows[0]], NOW)
        self.assertEqual((stats['count'], stats['wins'], stats['draws']), (3, 1, 1))
        self.assertAlmostEqual(stats['rate'], 100/3)
        self.assertEqual(stats['points'], [1000, 1012, 1010, 1010])

    def test_pagination_type_filter_decimal_score_cache_and_render(self):
        calls = []
        def fetch(endpoint, params, timeout):
            calls.append((endpoint, params))
            if endpoint == 'user':
                return {'pt': 1012, 'arena_rank': 196}
            self.assertEqual(params['type'], 1)
            return {'total': 101, 'data': ([game(when=f'2026-10-02T00:{i//60:02}:{i%60:02}Z', after=1012.34) for i in range(100)]
                                           if params['page'] == 1 else [game(when='2026-09-01T00:00:00Z')])}
        history = PlayerHistory(fetch=fetch, now=lambda: NOW, render=lambda *args: b'fake-png')
        reply = history.handle('花无十日红')
        self.assertIn('10月场次：100', reply)
        self.assertIn('当前分数：1012.34', reply)
        self.assertIn('当前排名：196', reply)
        self.assertTrue(reply.image_url.startswith('base64://'))
        self.assertEqual(history.handle('花无十日红'), reply)
        self.assertEqual(len(calls), 3)

    def test_no_history_and_api_failure_are_explicit(self):
        history = PlayerHistory(fetch=lambda e,p,t: {'total': 0, 'data': []}, now=lambda: NOW)
        self.assertIn('本月暂无竞技记录', history.handle('不存在'))
        self.assertIn('请确认玩家名', history.handle('不存在'))
        history = PlayerHistory(fetch=lambda *a: {'wrong': []}, now=lambda: NOW)
        self.assertIn('失败', history.handle('甲'))

    def test_incomplete_pages_never_claim_month_total(self):
        history = PlayerHistory(fetch=lambda e,p,t: ({'pt': 1000} if e == 'user' else
                                 {'total': 999999, 'data': [game()]}), now=lambda: NOW)
        self.assertIn('未能完整', history.handle('花无十日红'))

    def test_auth_before_network_no_subscription_required_and_group_route(self):
        calls = []
        history = PlayerHistory(fetch=lambda e,p,t: calls.append(e) or
                                ({'pt': 1000, 'arena_rank': 3} if e == 'user' else {'total': 0, 'data': []}), now=lambda: NOW)
        with temporary_service() as service:
            service.player_history = history
            self.assertIn('没有使用权限', service.handle_text('999', '历史 花无十日红'))
            self.assertEqual(calls, [])
            self.assertIn('当前排名：3', service.handle_text('10001', '历史 花无十日红'))
            self.assertEqual(service.store.list_subscriptions(), [])
            service.qq = GroupQQ()
            service.group_commands = True

            async def run_group():
                await service.on_qq_event(group_event('历史 花无十日红'))
                await asyncio.gather(*list(service._history_tasks))

            asyncio.run(run_group())
            self.assertTrue(any('当前排名：3' in text for _, text in service.qq.group_sent))
            self.assertEqual(service.qq.sent, [])

    def test_command_full_names_and_help_template(self):
        self.assertEqual(parse_command('历史 "Blue Eyes"'), {'op': 'player_history', 'player': 'Blue Eyes'})
        self.assertEqual(parse_command('历史')['op'], 'error')
        self.assertEqual(parse_command('历史 ' + '甲'*101)['op'], 'error')
        self.assertEqual(parse_command('花无十日红')['op'], 'unknown')

    def test_real_png_renderer(self):
        stats = monthly_stats('甲', [game(player='甲')], NOW)
        png = chart_png('甲', stats, NOW)
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertGreater(len(png), 10000)


class HistoryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_png_delivery_and_failure_never_dump_base64(self):
        history = PlayerHistory(fetch=lambda e,p,t: ({'pt': 1012, 'arena_rank': 3} if e == 'user' else
                                 {'total': 1, 'data': [game()]}), now=lambda: NOW, render=lambda *a: b'png')
        reply = history.handle('花无十日红')
        client = OneBotClient('ws://local', 'token', None)
        client.connected = True
        client.call = AsyncMock(side_effect=[{'retcode': 1}, {'retcode': 0}])
        self.assertTrue(await client.send_private('10001', reply))
        payload = client.call.call_args_list[0].args[1]
        self.assertEqual(payload['message'][1]['type'], 'image')
        self.assertTrue(payload['message'][1]['data']['file'].startswith('base64://'))
        fallback = client.call.call_args_list[1].args[1]['message']
        self.assertNotIn('base64://', fallback)
        self.assertIn('走势图发送失败', fallback)
