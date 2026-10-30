# -*- coding: utf-8 -*-
import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock
from zipfile import ZipFile

from bot.card_extras import CardCatalog, CardExtras, PagedReply
from bot.card_search import CardReply, CardSearch
from bot.commands import parse_command
from bot.qq import OneBotClient
from test_bot import temporary_service
from test_groups import GroupQQ, group_event


CARD = {'id': 14558127, 'cid': 12950, 'cn_name': '灰流丽', 'jp_name': '灰流うらら',
        'data': {'type': 33}, 'text': {'types': '怪兽', 'desc': '这个卡名的效果一回合一次。灰流丽的效果。'}}
OTHER = {'id': 23434538, 'cid': 9455, 'cn_name': '增殖的G', 'data': {'type': 33},
         'text': {'types': '怪兽', 'desc': '抽卡'}}


def table(limited=None, semi=None):
    return {'forbidden': {}, 'limited': limited or {}, 'semi_limited': semi or {}}


class FakeCatalog:
    def load(self):
        return {'12950': CARD, '9455': OTHER}


class ExtrasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 100
        self.limits = {'ja': dict(table(semi={'12950': '灰流うらら'}), date='2026-10-01'),
                       'old': {'ja': {'2020-04-01': table(limited={'9455': '増殖するG'}),
                                      '2020-01-01': table()}}}
        self.search = CardSearch(Path(self.temp.name), lambda path: self.limits)
        self.extras = CardExtras(Path(self.temp.name), self.search, FakeCatalog(),
                                 clock=lambda: self.now, today=lambda: date(2026, 10, 24), choose=lambda cards: cards[0])

    def test_current_card_cid_alias_and_unlimited(self):
        text = self.extras.handle('u', 'limits_card', '灰')
        self.assertIn('准限制（2张）', text)
        self.assertIn('2026-10-01', text)
        self.assertIn('无限制', self.extras.handle('u', 'limits_card', '23434538'))
        self.assertNotIn('无限制', self.extras.handle('u', 'limits_card', '错误卡名'))

    def test_current_and_historical_full_tables_are_complete_and_translated(self):
        current = self.extras.handle('u', 'limits_table')
        self.assertIsInstance(current, PagedReply)
        self.assertIn('灰流丽', current)
        history = self.extras.handle('u', 'limits_table', '2020-04')
        self.assertIn('2020-04-01', history)
        self.assertIn('增殖的G', history)
        self.assertIn('2020-04-01', self.extras.handle('u', 'limits_table', '2020-05'))
        self.assertIn('未收录', self.extras.handle('u', 'limits_table', '1900-01'))
        self.assertIn('尚未到来', self.extras.handle('u', 'limits_table', '2099-01'))
        self.assertIn('月份格式', self.extras.handle('u', 'limits_table', '2020-13'))

    def test_announced_future_table_does_not_override_current(self):
        self.limits['old']['ja']['2026-10-01'] = table(limited={'12950': '灰流うらら'})
        self.limits['ja']['date'] = '2027-01-01'
        text = self.extras.handle('u', 'limits_card', '灰')
        self.assertIn('2026-10-01', text)
        self.assertIn('限制（1张）', text)
        self.assertNotIn('准限制', text)

    def test_missing_banlist_never_reports_unlimited(self):
        self.limits['ja'].pop('limited')
        text = self.extras.handle('u', 'limits_card', '灰')
        self.assertIn('无法获取有效数据', text)
        self.assertNotIn('OCG：无限制', text)

    def test_guess_masks_name_wrong_answer_alias_win_and_expiry(self):
        prompt = self.extras.handle('u', 'guess_start')
        self.assertNotIn('灰流丽', prompt)
        self.assertIn('隐藏', prompt)
        self.assertIn('已有', self.extras.handle('u', 'guess_start'))
        self.assertIn('还没猜对', self.extras.handle('u', 'guess_answer', '白龙'))
        self.assertIn('猜对', self.extras.handle('u', 'guess_answer', '灰'))
        self.assertIn('没有进行中', self.extras.handle('u', 'guess_reveal'))
        self.extras.handle('u', 'guess_start')
        self.now += 300
        self.assertIn('过期', self.extras.handle('u', 'guess_answer', '灰'))

    def test_random_uses_the_same_detail_format_as_card_search_including_release_date(self):
        release = '<i>OCG</i> <span class="pack">TEST-JP001 (2020-04-01)</span>'
        search = CardSearch(Path(self.temp.name), lambda path: self.limits, release_fetch=lambda _: release)
        extras = CardExtras(Path(self.temp.name), search, FakeCatalog(), clock=lambda: self.now,
                            today=lambda: date(2026, 12, 1), choose=lambda cards: CARD)
        text = extras.handle('u', 'random')
        self.assertIn('灰流丽', text)
        self.assertIn('OCG TEST-JP001 (2020-04-01)', text)
        self.assertEqual(text, search._reply_detail(CARD))

    def test_random_and_reveal_have_images_and_sessions_are_separate(self):
        self.assertIsInstance(self.extras.handle('u', 'random'), CardReply)
        self.extras.handle('u', 'guess_start')
        self.assertIn('没有进行中', self.extras.handle('v', 'guess_reveal'))
        self.assertIsInstance(self.extras.handle('u', 'guess_reveal'), CardReply)

    def test_commands_and_format_errors(self):
        self.assertEqual(parse_command('禁限 OCG 增殖的G'), {'op': 'limits_card', 'value': '增殖的G'})
        self.assertEqual(parse_command('禁限表 OCG 2020-04'), {'op': 'limits_table', 'value': '2020-04'})
        self.assertEqual(parse_command('禁限表'), {'op': 'limits_table', 'value': ''})
        for value in ('禁限 MD 灰', '禁限表 OCG 2020', '禁限', '猜卡答案', '猜卡 abc'):
            self.assertEqual(parse_command(value)['op'], 'error', value)

    def test_group_game_is_shared_but_other_groups_and_private_are_isolated(self):
        with temporary_service() as service:
            service.card_extras = self.extras
            service.group_commands = True
            service.qq = GroupQQ()
            asyncio.run(service.on_qq_event(group_event('猜卡')))
            asyncio.run(service.on_qq_event(group_event('猜卡答案 灰', user_id=10002, group_id=90001)))
            self.assertIn('没有进行中', service.qq.group_sent[-1][1])
            self.assertIn('没有进行中', service.handle_text('10001', '揭晓猜卡'))
            asyncio.run(service.on_qq_event(group_event('猜卡答案 灰', user_id=10002)))
            self.assertIn('猜对', service.qq.group_sent[-1][1])
            asyncio.run(service.on_qq_event(group_event('禁限表 OCG')))
            self.assertIsInstance(service.qq.group_sent[-1][1], list)
            self.assertEqual(service.qq.sent, [])

    def test_unauthorized_user_cannot_start_or_reveal_group_game(self):
        with temporary_service() as service:
            service.card_extras = self.extras
            self.assertIn('没有使用权限', service.handle_text('999', '猜卡', 'group:1'))
            self.assertEqual(self.extras.games, {})


class CatalogTests(unittest.TestCase):
    def test_verified_download_cache_and_md5_unchanged_skips_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = json.dumps({'12950': CARD}).encode('utf-8')
            checksum = hashlib.md5(payload).hexdigest().encode('ascii')
            output = BytesIO()
            with ZipFile(output, 'w') as archive:
                archive.writestr('cards.json', payload)
            calls = []
            def fetch(path):
                calls.append(path)
                return b'"' + checksum + b'"' if path.endswith('md5') else output.getvalue()
            now = [2_000_000_000]
            catalog = CardCatalog(directory, fetch, lambda: now[0])
            self.assertIn('12950', catalog.load())
            catalog.load()
            self.assertEqual(calls, ['cards.zip.md5', 'cards.zip'])
            now[0] += 86401
            catalog.load()
            self.assertEqual(calls, ['cards.zip.md5', 'cards.zip', 'cards.zip.md5'])
            self.assertEqual((Path(directory) / 'cache/baige/cards.json').read_bytes(), payload)

    def test_bad_checksum_never_replaces_existing_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            output = BytesIO()
            with ZipFile(output, 'w') as archive:
                archive.writestr('cards.json', b'{}')
            catalog = CardCatalog(directory, lambda path: b'0' * 32 if path.endswith('md5') else output.getvalue())
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                catalog.load()
            self.assertFalse((Path(directory) / 'cache/baige/cards.json').exists())


class GroupSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_card_and_forward_only_target_group(self):
        client = OneBotClient('ws://local', '', None)
        client.connected = True
        client.call = AsyncMock(return_value={'retcode': 0})
        await client.send_group(90000, CardReply('卡', 'https://example.com/1.jpg'))
        action, params = client.call.call_args.args
        self.assertEqual(action, 'send_group_msg')
        self.assertNotIn('user_id', params)
        self.assertEqual(params['message'][1]['type'], 'image')
        await client.send_group_forward(90000, ['全表'], 123)
        action, params = client.call.call_args.args
        self.assertEqual(action, 'send_group_forward_msg')
        self.assertEqual(params['group_id'], 90000)
        self.assertNotIn('user_id', params)


if __name__ == '__main__':
    unittest.main()
