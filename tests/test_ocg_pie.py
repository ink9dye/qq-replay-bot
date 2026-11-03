import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bot.commands import parse_command
from bot.ocg_pie import HOME, TTL, OcgPie, latest_report, report_details, _FETCH_LOCK
from bot.qq import group_command_text
from test_bot import temporary_service
from test_groups import GroupQQ, group_event

URL = HOME + "ocg-test-metagame-report/"
PIE = HOME + "wp-content/uploads/2026/10/OCG202610-2-Pie.jpg"
INDEX = f'<a href="/md-report/">Master Duel Metagame Report</a><h2><a href="{URL}">OCG Test Metagame Report #2, #3</a></h2>'
ARTICLE = '''<meta property="article:published_time" content="2026-10-30T00:00:00+00:00">
<p>This report tabulates 185 performing decks from 33 tournaments that were held in Japan during 10 – 26 October 2026.</p>
<img src="/wp-content/uploads/cover.jpg"><img src="/wp-content/uploads/2026/10/OCG202610-2-Pie-520x245.jpg">
<img src="/wp-content/uploads/deck.jpg">'''
IMAGE = b"\xff\xd8\xff" + b"fixture"


class PieTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 100_000
        self.fetch = Mock(side_effect=lambda url, limit: {
            HOME: INDEX.encode(), URL: ARTICLE.encode(), PIE: IMAGE}[url])
        self.pie = OcgPie(self.temp.name, fetch=self.fetch, clock=lambda: self.now)

    def test_discovery_selects_ocg_report_and_chart_not_cover_or_deck(self):
        self.assertEqual(latest_report(INDEX)[0], URL)
        details = report_details(ARTICLE)
        self.assertEqual(details['image_url'], PIE)
        self.assertEqual(details['published'], '2026-10-30')
        with self.assertRaises(ValueError):
            latest_report('<a href="https://evil.test/">OCG Metagame Report</a>')
        with self.assertRaises(ValueError):
            report_details('<img src="/wp-content/uploads/deck.jpg">')

    def test_first_fetch_cache_and_six_hour_same_article_check(self):
        reply = self.pie.reply()
        self.assertIn('33场赛事、185份上位构筑', reply)
        self.assertIn('10 – 26 October 2026', reply)
        self.assertIn(URL, reply)
        self.assertTrue(reply.image_url.startswith('base64://'))
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(self.pie.reply(), reply)
        self.assertEqual(self.fetch.call_count, 3)
        self.now += TTL + 1
        self.pie.reply()
        self.assertEqual(self.fetch.call_count, 4)  # Same report: no duplicate image download.

    def test_failed_refresh_preserves_old_cache_and_backs_off(self):
        self.pie.reply()
        original = self.pie.path.read_bytes()
        self.now += TTL + 1
        self.fetch.side_effect = TimeoutError('offline')
        reply = self.pie.reply()
        self.assertIn('上次成功保存', reply)
        self.assertTrue(reply.image_url)
        self.assertEqual(self.pie.path.read_bytes(), original)
        self.pie.reply()
        self.assertEqual(self.fetch.call_count, 4)

    def test_invalid_new_image_cannot_replace_good_cache(self):
        self.pie.reply()
        original = self.pie.path.read_bytes()
        self.now += TTL + 1
        new_url = HOME + 'new-report/'
        def fetch(url, limit):
            if url == HOME:
                return f'<a href="{new_url}">OCG New Metagame Report</a>'.encode()
            if url == new_url:
                return ARTICLE.encode()
            return b'<html>blocked</html>'
        self.pie.fetch = fetch
        self.assertIn('上次成功保存', self.pie.reply())
        self.assertEqual(self.pie.path.read_bytes(), original)

    def test_cold_failure_and_concurrent_request_do_not_download_again(self):
        with _FETCH_LOCK:
            self.assertIn('正在获取', self.pie.reply())
            self.fetch.assert_not_called()
        self.fetch.side_effect = TimeoutError()
        self.assertIn('获取失败', self.pie.reply())
        self.assertIn('暂时不可用', self.pie.reply())
        self.assertEqual(self.fetch.call_count, 1)

    def test_corrupt_cache_is_replaced_by_successful_fetch(self):
        self.pie.path.parent.mkdir(parents=True)
        self.pie.path.write_text('{bad json', encoding='utf8')
        self.assertTrue(self.pie.reply().image_url)
        self.assertEqual(json.loads(self.pie.path.read_text(encoding='utf8'))['url'], URL)

    def test_case_insensitive_command_group_routing_and_authorization(self):
        for prefix in ('OCG', 'ocg', 'Ocg', 'oCg'):
            text = prefix + '饼图'
            self.assertEqual(parse_command(text), {'op': 'ocg_pie'})
            event = group_event(text)
            event['message'] = [{'type': 'text', 'data': {'text': text}}]
            self.assertEqual(group_command_text(event, True), text)
        self.assertEqual(parse_command('OCG饼图 更新')['op'], 'error')
        with temporary_service() as service:
            service.ocg_pie = self.pie
            self.assertIn('没有使用权限', service.handle_text('999', 'ocg饼图'))
            self.fetch.assert_not_called()
            service.group_commands = service.group_without_at = True
            service.qq = GroupQQ()
            async def query():
                await service.on_qq_event(event)
                await asyncio.gather(*service._slow_tasks)
            asyncio.run(query())
            self.assertEqual(len(service.qq.group_sent), 2)
            self.assertTrue(service.qq.group_sent[-1][1].image_url)
            self.assertEqual(service.qq.sent, [])


if __name__ == '__main__':
    unittest.main()
