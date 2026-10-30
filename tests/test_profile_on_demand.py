import json
import unittest
from unittest.mock import patch

from test_bot import temporary_service, link_token


class OnDemandTests(unittest.TestCase):
    def test_qq_directory_message_includes_players_latest_observed_deck(self):
        from test_deck_links import save_game
        from bot.watch_core import _rank_cache
        _rank_cache['甲'], _rank_cache['对手'] = 12, 88
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            save_game(service, '甲', [1, 2, 3], [4])
            with patch('bot.deck_links.observed_title', return_value='混沌道化'):
                message = service.handle_text('10001', '录像 甲')
            self.assertIn('最近使用卡组：混沌道化（刚刚）', message)
            self.assertIn('玩家：甲', message)

    def test_link_message_and_page_explain_maximum_not_guaranteed_availability(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            text = service.handle_text('10001', '录像 甲')
            self.assertIn('链接最晚到期', text)
            self.assertIn('不保证公网地址持续在线', text)
            _, _, body = service.share.page(link_token(text), 'dir')
            self.assertIn('临时公网地址没有固定保活时长', body.decode())
            self.assertIn('最长有效5小时', body.decode())

    def test_profile_only_queries_link_player_after_expiry_and_token_checks(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 花无十日红 全部 静默')
            token = link_token(service.handle_text('10001', '录像 花无十日红'))
            with patch('bot.arena_profile.fetch_profile', return_value={'pt': 1299.04, 'arena_rank': 196}) as fetch:
                status, _, body = service.share.page(token, 'dir')
                self.assertEqual(status, 200)
                self.assertIn(b'data-profile-url', body)
                fetch.assert_not_called()
                status, kind, body = service.share.page(token, 'profile')
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertEqual((data['score'], data['rank']), (1299.04, 196))
                fetch.assert_called_once_with('花无十日红')
                self.assertEqual(service.share.page('invalid', 'profile')[0], 404)
                service.clock.now += 5 * 3600
                self.assertEqual(service.share.page(token, 'profile')[0], 410)
                self.assertEqual(fetch.call_count, 1)

    def test_fresh_cache_and_failure_fallback_are_json_safe(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 静默')
            token = link_token(service.handle_text('10001', '录像 甲'))
            with patch('bot.arena_profile.fetch_profile', side_effect=OSError('offline')):
                status, _, body = service.share.page(token, 'profile')
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertIsNone(data['score'])
            self.assertIsNone(data['rank'])
