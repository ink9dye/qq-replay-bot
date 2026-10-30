import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bot.card_search import CardReply, CardSearch
from bot.commands import parse_command
from bot.qq import OneBotClient
from test_bot import temporary_service


def card(name, number=89631139):
    return {'id': number, 'cn_name': name, 'text': {'types': '怪兽', 'desc': '效果文本'}}


class CardTests(unittest.TestCase):
    def test_release_records_and_cache_on_exact_and_selected_card(self):
        from bot.card_search import release_lines
        page = '<i>OCG</i><span class="pack">BACH-JP044 <a>(2021-10-16)</a></span>'
        self.assertEqual(release_lines(page), ['OCG BACH-JP044 (2021-10-16)'])
        self.assertEqual(release_lines(page.replace('2021-10-16','2021-02-30')), [])
        calls=[]
        self.search.release_fetch=lambda code:(calls.append(code) or page)
        self.result={'result':[card('测试卡', 123)],'next':0}
        reply=self.search.handle('a','search','123')
        self.assertIn('OCG BACH-JP044 (2021-10-16)',reply)
        self.search.handle('a','search','123')
        self.assertEqual(calls,[123])
        self.result={'result':[card('测试甲',124),card('测试乙',125)],'next':0}
        self.search.handle('b','search','测试')
        self.assertIn('OCG BACH-JP044 (2021-10-16)',self.search.handle('b','select','1'))

    def test_release_failure_does_not_hide_card_and_is_negatively_cached(self):
        calls=[]
        def fail(code):
            calls.append(code)
            raise TimeoutError()
        self.search.release_fetch=fail
        self.result={'result':[card('测试卡',123)],'next':0}
        reply=self.search.handle('a','search','123')
        self.assertIn('效果文本',reply)
        self.assertIn('本次读取失败',reply)
        self.search.handle('a','search','123')
        self.assertEqual(calls,[123])
        self.now+=31
        self.search.handle('a','search','123')
        self.assertEqual(calls,[123,123])

    def test_real_card_release_markup_and_public_cache_survives_new_instance(self):
        page = '<i>OCG</i><span class="pack">BLZD-JP016 <a>(2026-01-24)</a></span><i>TCG</i><span class="pack">BLZD-EN016 <a>(2026-05-08)</a></span>'
        directory=Path(self.temp.name)
        original=CardSearch(directory,self.fetch,lambda:100,release_fetch=lambda _:page,wall_clock=lambda:2000)
        reply=original._reply_detail(card('Synthetic',19144622))
        self.assertIn('OCG BLZD-JP016 (2026-01-24)',reply)
        calls=[]
        fresh=CardSearch(directory,self.fetch,lambda:100,release_fetch=lambda code:calls.append(code),wall_clock=lambda:2001)
        self.assertIn('TCG BLZD-EN016 (2026-05-08)',fresh._reply_detail(card('Synthetic',19144622)))
        self.assertEqual(calls,[])

    def test_expired_success_kept_on_refresh_failure(self):
        page='<i>OCG</i><span class="pack">BLZD-JP016 (2026-01-24)</span>'
        current=[2000]
        search=CardSearch(Path(self.temp.name),self.fetch,lambda:current[0],release_fetch=lambda _:page,wall_clock=lambda:current[0])
        search._reply_detail(card('Synthetic',19144622))
        current[0]+=86401
        def fail(_):raise TimeoutError()
        search.release_fetch=fail
        reply=search._reply_detail(card('Synthetic',19144622))
        self.assertIn('OCG BLZD-JP016 (2026-01-24)',reply)
        self.assertIn('已缓存记录',reply)
        self.assertNotIn('本次读取失败',reply)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.result = {'result': [card('闪刀姬' + str(i), i + 1) for i in range(8)], 'next': 8}
        self.now = 100
        self.search = CardSearch(Path(self.temp.name), self.fetch, lambda: self.now)

    def fetch(self, path):
        self.calls.append(path)
        return self.result

    def test_partial_candidates_paging_selection_isolation_and_expiry(self):
        reply = self.search.handle('a', 'search', '闪刀')
        self.assertIn('5. 闪刀姬4', reply)
        self.assertNotIn('6. 闪刀姬5', reply)
        self.assertIn('不存在', self.search.handle('b', 'select', '1'))
        self.assertIn('6. 闪刀姬5', self.search.handle('a', 'next'))
        self.assertIn('闪刀姬5', self.search.handle('a', 'select', '6'))
        self.assertIn('范围', self.search.handle('a', 'select', '99'))
        self.assertIn('范围', self.search.handle('a', 'select', '0'))
        self.now += 300
        self.assertIn('过期', self.search.handle('a', 'select', '1'))

    def test_exact_hit_beats_other_related_cards_and_contains_image(self):
        self.result = {'result': [card('青眼白龙'), card('罪 青眼白龙', 9433350)], 'next': 2}
        reply = self.search.handle('a', 'search', '青眼白龙')
        self.assertIsInstance(reply, CardReply)
        self.assertIn('效果文本', reply)
        self.assertTrue(reply.image_url.endswith('/89631139.jpg'))
        self.assertNotIn('候选', reply)

    def test_builtin_and_custom_alias_reload_and_cache(self):
        self.result = {'result': [card('灰流丽')], 'next': 0}
        self.search.handle('a', 'search', '灰')
        self.search.handle('b', 'search', '灰')
        self.assertEqual(len(self.calls), 1)
        self.search.alias_path.write_text(json.dumps({'我的灰': '灰流丽'}), encoding='utf-8')
        self.assertIsInstance(self.search.handle('a', 'search', '我的灰'), CardReply)
        self.assertEqual(len(self.calls), 1)
        self.search.alias_path.write_text('bad json', encoding='utf-8')
        self.assertIn('JSON', self.search.handle('a', 'search', '灰'))

    def test_network_error_empty_result_and_malformed_response(self):
        self.search.fetch = lambda path: (_ for _ in ()).throw(OSError('offline'))
        self.assertIn('稍后重试', self.search.handle('a', 'search', '白龙'))
        self.search.fetch = self.fetch
        self.result = {'result': [], 'next': 0}
        self.assertIn('没有找到', self.search.handle('a', 'search', '不存在'))
        self.result = {'wrong': []}
        self.assertIn('失败', self.search.handle('a', 'search', '新词'))

    def test_api_next_offset_and_end(self):
        self.result = {'result': [card('卡甲'), card('卡乙', 2)], 'next': 17}
        self.search.handle('a', 'search', '卡')
        self.result = {'result': [card('卡丙', 3)], 'next': 0}
        self.assertIn('卡丙', self.search.handle('a', 'next'))
        self.assertIn('start=17', self.calls[-1])
        self.assertIn('最后一页', self.search.handle('a', 'next'))

    def test_commands_keep_multiword_queries_and_do_not_hijack_player_names(self):
        self.assertEqual(parse_command('ck Blue Eyes')['query'], 'Blue Eyes')
        self.assertEqual(parse_command('查卡下一页')['op'], 'card_next')
        self.assertEqual(parse_command('选卡 1')['op'], 'card_select')
        self.assertEqual(parse_command('1')['op'], 'unknown')
        self.assertEqual(parse_command('选卡 -1')['op'], 'error')

    def test_service_authorization_before_network_and_command_integration(self):
        with temporary_service() as service:
            service.card_search = self.search
            self.assertIn('没有使用权限', service.handle_text('999', '查卡 白龙'))
            self.assertEqual(self.calls, [])
            self.assertIn('候选', service.handle_text('10001', '查卡 闪刀'))
            self.assertIsInstance(service.handle_text('10001', '选卡 1'), CardReply)


class CardTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_uses_structured_segments_and_failure_falls_back(self):
        client = OneBotClient('ws://local', 'token', None)
        client.connected = True
        client.call = AsyncMock(side_effect=[{'retcode': 1}, {'retcode': 0}])
        reply = CardReply('效果[CQ:at,qq=all]', 'https://example.com/card.jpg')
        self.assertTrue(await client.send_private('10001', reply))
        image_payload = client.call.call_args_list[0].args[1]
        self.assertEqual(image_payload['message'][0]['data']['text'], str(reply))
        self.assertEqual(image_payload['message'][1]['type'], 'image')
        fallback = client.call.call_args_list[1].args[1]
        self.assertTrue(fallback['auto_escape'])
        self.assertIn('卡图：https://example.com/card.jpg', fallback['message'])


if __name__ == '__main__':
    unittest.main()
