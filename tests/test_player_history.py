import asyncio
import shutil
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

import test_bot
from bot.commands import parse_command
from bot.player_history import CST, PlayerHistory, chart_png, monthly_chart_png, monthly_stats
from bot.qq import OneBotClient
from test_bot import temporary_service
from test_groups import GroupQQ, group_event

NOW = datetime(2026, 10, 24, 15, tzinfo=CST)
MONTH_NOW = datetime(2026, 11, 8, 12, tzinfo=CST)


def game(player='花无十日红', side='a', when='2026-10-01T00:00:00Z', before=1000, after=1012,
         winner=None, kind='athletic'):
    return {'usernamea': player if side == 'a' else '对手',
            'usernameb': player if side == 'b' else '对手',
            'type': kind, 'start_time': when, 'end_time': when,
            'pta_ex': before, 'ptb_ex': before, 'pta': after, 'ptb': after,
            'winner': player if winner is None else winner}


def sample_rows():
    """14 场当月竞技：首场赛前 1216.65，末场结算 1320.26，12 胜 2 负 = 85.71%。"""
    rows, score = [], 1216.65
    final = 1320.26
    step = (final - score) / 14
    for index in range(14):
        after = round(score + step, 2) if index < 13 else final
        rows.append(game(player='KCI', when=f'2026-11-{index // 2 + 1:02}T0{index % 2}:00:00Z',
                         before=round(score, 2), after=after,
                         winner='对手' if index in (2, 9) else 'KCI'))
        score = after
    rows.append(game(player='KCI', when='2026-10-20T00:00:00Z', before=1200, after=1216.65))
    rows.sort(key=lambda row: row['start_time'], reverse=True)
    return rows


class HistoryTests(unittest.TestCase):
    def test_recent_window_uses_last_100_and_decimal_score(self):
        rows = [game(when=f'2026-10-02T00:{i // 60:02}:{i % 60:02}Z', after=1012.34) for i in range(100)]

        def fetch(endpoint, params, timeout):
            if endpoint == 'user':
                return {'pt': 1012, 'arena_rank': 196}
            return {'total': 101, 'data': rows if params.get('page', 1) == 1 else []}

        history = PlayerHistory(fetch=fetch, now=lambda: NOW, render=lambda *args: b'fake-png')
        reply = history.handle('花无十日红')
        self.assertIn('最近 100 局：100胜0负，胜率 100.00%', reply)
        self.assertIn('当前分数：1012', reply)
        self.assertIn('当前排名：196', reply)
        self.assertTrue(reply.image_url.startswith('base64://'))

    def test_monthly_reply_matches_sample_template(self):
        history = PlayerHistory(fetch=lambda endpoint, params, timeout: (
            {'pt': 1333, 'arena_rank': 43} if endpoint == 'user' else {'total': 15, 'data': sample_rows()}),
            now=lambda: MONTH_NOW, render_monthly=lambda *args: b'fake-png')
        reply = history.handle_monthly('KCI')
        self.assertEqual(str(reply), '玩家：KCI\n11月场次：14\n11月胜率：85.71%\n'
                                     '当前分数：1320.26\n当前排名：43')
        self.assertTrue(reply.image_url.startswith('base64://'))

    def test_monthly_boundary_and_pagination_stop(self):
        calls = []

        def fetch(endpoint, params, timeout):
            calls.append((endpoint, params.get('page')))
            if endpoint == 'user':
                return {'arena_rank': 43}
            if params.get('page') == 1:
                return {'total': 60, 'data': [
                    game(player='甲', when=f'2026-11-03T00:{i:02}:00Z', after=1100 + i) for i in range(50)]}
            return {'total': 60, 'data': [
                game(player='甲', when='2026-10-31T16:00:00Z', before=1000, after=1001),   # 北京 11-01 00:00，算当月
                game(player='甲', when='2026-10-31T15:59:59Z', before=999, after=1000),    # 北京 10-31 23:59:59，不算
                game(player='甲', when='2026-10-20T00:00:00Z', before=990, after=999)]}

        history = PlayerHistory(fetch=fetch, now=lambda: MONTH_NOW, render_monthly=lambda *args: b'png')
        reply = history.handle_monthly('甲')
        self.assertIn('\n11月场次：51\n', reply)
        self.assertEqual([page for endpoint, page in calls if endpoint == 'history'], [1, 2])

    def test_monthly_refuses_incomplete_pages(self):
        def fetch(endpoint, params, timeout):
            if endpoint == 'user':
                return {'arena_rank': 43}
            page = params.get('page', 1)
            return {'total': 1000, 'data': [
                game(player='甲', when=f'2026-11-05T{page:02}:{i:02}:00Z', after=1100 + i) for i in range(50)]}

        history = PlayerHistory(fetch=fetch, now=lambda: MONTH_NOW, render_monthly=lambda *args: b'png')
        history.MONTH_MAX_PAGES = 2
        reply = history.handle_monthly('甲')
        self.assertIn('未能完整取得历史', reply)

    def test_monthly_no_games_and_api_failure_are_explicit(self):
        history = PlayerHistory(fetch=lambda endpoint, params, timeout: (
            {'pt': 1300, 'arena_rank': 0} if endpoint == 'user' else {'total': 0, 'data': []}),
            now=lambda: MONTH_NOW, render_monthly=lambda *args: b'png')
        reply = history.handle_monthly('甲')
        self.assertIn('11月场次：0', reply)
        self.assertIn('11月胜率：暂无', reply)
        self.assertIn('当前分数：1300', reply)
        self.assertIn('当前排名：未上榜', reply)
        self.assertIn('本月暂无竞技记录', reply)
        self.assertIsNone(getattr(reply, 'image_url', None))

        broken = PlayerHistory(fetch=lambda *args: {'wrong': []}, now=lambda: MONTH_NOW,
                               render_monthly=lambda *args: b'png')
        self.assertIn('失败', broken.handle_monthly('甲'))

    def test_monthly_command_parsing(self):
        self.assertEqual(parse_command('当月历史 "Blue Eyes"'),
                         {'op': 'player_history', 'player': 'Blue Eyes', 'monthly': True})
        self.assertEqual(parse_command('历史 "Blue Eyes"'),
                         {'op': 'player_history', 'player': 'Blue Eyes'})
        self.assertEqual(parse_command('当月历史')['op'], 'error')
        self.assertEqual(parse_command('当月历史 ' + '甲' * 101)['op'], 'error')
        self.assertEqual(parse_command('花无十日红')['op'], 'unknown')

    def test_auth_before_network_and_group_route_for_both_modes(self):
        calls = []
        history = PlayerHistory(fetch=lambda e, p, t: calls.append(e) or
                                ({'pt': 1000, 'arena_rank': 3} if e == 'user' else {'total': 0, 'data': []}),
                                now=lambda: MONTH_NOW, render_monthly=lambda *a: b'png')
        # 本机沙箱拒绝在随机临时目录里再建目录；固定到仓库内路径。
        root = Path(__file__).resolve().parent / '_service-root'
        shutil.rmtree(root, ignore_errors=True)
        try:
            with mock.patch.object(test_bot.tempfile, 'mkdtemp', return_value=str(root)):
                with temporary_service() as service:
                    service.player_history = history
                    self.assertIn('没有使用权限', service.handle_text('999', '当月历史 花无十日红'))
                    self.assertEqual(calls, [])
                    self.assertIn('当前排名：3', service.handle_text('10001', '历史 花无十日红'))
                    self.assertIn('当前排名：3', service.handle_text('10001', '当月历史 花无十日红'))
                    self.assertEqual(service.store.list_subscriptions(), [])
                    service.qq = GroupQQ()
                    service.group_commands = True

                    async def run_group():
                        await service.on_qq_event(group_event('历史 花无十日红'))
                        await service.on_qq_event(group_event('当月历史 花无十日红'))
                        await asyncio.gather(*list(service._history_tasks))

                    asyncio.run(run_group())
                    self.assertTrue(any('当前排名：3' in text for _, text in service.qq.group_sent))
                    self.assertEqual(service.qq.sent, [])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_help_lists_new_command(self):
        from bot.help import HELP
        self.assertIn('当月历史 玩家名', HELP)
        self.assertIn('历史 玩家名：最近竞技战绩', HELP)

    def test_real_png_renderers(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest('本机未安装 matplotlib，渲染在服务器验证')
        rows = [game(player='甲', when=f'2026-11-0{i}T00:00:00Z', after=1000 + i) for i in range(1, 6)]
        stats = monthly_stats('甲', rows, MONTH_NOW)
        for render in (chart_png, monthly_chart_png):
            png = render('甲', stats, MONTH_NOW)
            self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertGreater(len(png), 10000)


class HistoryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_png_delivery_and_failure_never_dump_base64(self):
        history = PlayerHistory(fetch=lambda e, p, t: ({'pt': 1012, 'arena_rank': 3} if e == 'user' else
                                 {'total': 1, 'data': [game()]}), now=lambda: NOW,
                                render_monthly=lambda *a: b'png')
        reply = history.handle_monthly('花无十日红')
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
