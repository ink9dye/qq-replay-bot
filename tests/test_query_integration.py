"""Integration checks for query migration against the retained watch and portal code."""
import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.commands import parse_command
from bot.query_auth import QueryAuth
from bot.query_core import PortalClient, QueryCore
from bot.query_setup import build_query
from bot.service import BotService
from bot.watch_api import WatchApiServer
from bot.watch_jobs import RemoteJobs
from test_query_auth import _post, _get, TOKEN, ADMIN
from test_query_core import FakePortal


class AuthConcurrencyTests(unittest.TestCase):
    def test_code_and_refresh_are_each_consumed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = QueryAuth(Path(tmp) / 'auth.sqlite3')
            code = qa.provision('12345')['code']
            def race(fn):
                barrier = threading.Barrier(2)
                def run(_):
                    barrier.wait()
                    return fn()
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    return list(pool.map(run, range(2)))
            logins = race(lambda: qa.login('12345', code, '192.0.2.1'))
            self.assertEqual(sum(bool(r['ok']) for r in logins), 1)
            first = next(r for r in logins if r['ok'])
            refreshed = race(lambda: qa.refresh(first['refresh_token']))
            self.assertEqual(sum(bool(r['ok']) for r in refreshed), 1)
            self.assertIsNone(qa.authenticate(first['token']))

    def test_lock_does_not_lock_known_account_at_other_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = QueryAuth(Path(tmp) / 'auth.sqlite3', lock_after_failures=2)
            code = qa.provision('12345')['code']
            for _ in range(2):
                qa.login('12345', 'wrong', '192.0.2.1')
            self.assertTrue(qa.login('12345', code, '192.0.2.2')['ok'])

    def test_ip_rate_limit_does_not_share_one_global_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa = QueryAuth(Path(tmp) / 'auth.sqlite3')
            for _ in range(10):
                qa._ip_window_allow(100, '192.0.2.1')
            self.assertFalse(qa._ip_window_allow(100, '192.0.2.1'))
            self.assertTrue(qa._ip_window_allow(100, '192.0.2.2'))


class QueryWatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.qa = QueryAuth(Path(self.tmp.name) / 'auth.sqlite3')
        self.core = QueryCore(FakePortal({'/statistics/api/roster': {}}))
        self.api = WatchApiServer(RemoteJobs(), Path(self.tmp.name) / 'replays', TOKEN,
                                  bind=('127.0.0.1', 0), log=lambda *_: None,
                                  query_auth=self.qa, admin_token=ADMIN, query_core=self.core)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.port = self.api._httpd.server_address[1]
        code = self.qa.provision('12345')['code']
        self.session = self.qa.login('12345', code)
        self.headers = {'Authorization': 'Bearer ' + self.session['token']}

    def test_query_and_watch_tokens_cannot_cross(self):
        status, _ = _post(self.port, '/watch-api/claim', {'worker': 'other'}, self.headers)
        self.assertEqual(status, 401)
        status, _ = _get(self.port, '/query-api/top100', {'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(status, 401)
        status, _ = _get(self.port, '/watch-api/status', {'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(status, 200)

    def test_disabled_queries_do_not_disable_watch(self):
        self.api._httpd.query_auth = None
        self.assertEqual(_get(self.port, '/query-api/status', self.headers)[0], 503)
        self.assertEqual(_get(self.port, '/watch-api/status', {'Authorization': 'Bearer ' + TOKEN})[0], 200)

    def test_busy_queries_fail_without_blocking_watch(self):
        gate = self.api._httpd.query_light_gate
        permits = max(1, int(self.api.query_light_gate))
        for _ in range(permits):
            gate.acquire()
        try:
            self.assertEqual(_get(self.port, '/query-api/top100', self.headers)[0], 429)
            self.assertEqual(_get(self.port, '/watch-api/status', {'Authorization': 'Bearer ' + TOKEN})[0], 200)
        finally:
            for _ in range(permits):
                gate.release()

    def test_forwarded_admin_rejected_even_with_correct_secret(self):
        for name in ('X-Real-IP', 'X-Forwarded-For', 'Forwarded'):
            headers = {'X-Admin-Token': ADMIN, name: '127.0.0.1'}
            self.assertEqual(_get(self.port, '/query-api/admin/list', headers)[0], 401)
        self.assertEqual(_get(self.port, '/query-api/admin/list', {'X-Admin-Token': ADMIN})[0], 200)

    def test_malformed_body_and_subscription_types_rejected(self):
        from test_query_core import FakeSubStore
        self.core.store = FakeSubStore()
        self.api._httpd.writes_enabled = True
        self.qa.set_writes('12345', True)
        for value in ([], {'player': '甲', 'rank_top': True},
                      {'player': '甲', 'notify': 'false'}, {'op': 'typo', 'player': '甲'}):
            self.assertEqual(_post(self.port, '/query-api/subscriptions', value, self.headers)[0], 400)
        self.assertEqual(_post(self.port, '/query-api/admin/issue', {'identity': '<script>'},
                               {'X-Admin-Token': ADMIN})[0], 400)


class EnrollFlowTests(unittest.TestCase):
    """QQ「开通查询」：首次开通建号，已开通账号再次发送应重发码而不是拒绝。"""

    def _service(self):
        service = object.__new__(BotService)
        service.config = {"query": {"enabled": True, "admin_token": "x" * 24, "code_hours": 24},
                          "remote": {"bind_port": 8791}}
        return service

    def test_first_enroll_provisions(self):
        service = self._service()
        with patch('bot.query_core.admin_issue',
                   return_value={"ok": True, "identity": "12345", "code": "first-code"}), \
             patch('bot.query_core.admin_reissue') as reissue:
            text = service._query_enroll("12345")
        self.assertIn("first-code", text)
        self.assertNotIn("重新发放", text)
        reissue.assert_not_called()

    def test_second_enroll_reissues_instead_of_refusing(self):
        service = self._service()
        with patch('bot.query_core.admin_issue',
                   return_value={"ok": False, "error": "already_provisioned", "status": 409}), \
             patch('bot.query_core.admin_reissue',
                   return_value={"ok": True, "identity": "12345", "code": "fresh-code"}) as reissue:
            text = service._query_enroll("12345")
        reissue.assert_called_once()
        self.assertIn("fresh-code", text)
        self.assertIn("重新发放", text)
        self.assertNotIn("不重复发放", text)
        self.assertIn("再私聊发送“开通查询”", text)

    def test_disabled_account_cannot_self_reissue(self):
        service = self._service()
        with patch('bot.query_core.admin_issue',
                   return_value={"ok": False, "error": "already_provisioned", "status": 409}), \
             patch('bot.query_core.admin_reissue',
                   return_value={"ok": False, "error": "not_found", "status": 404}):
            text = service._query_enroll("12345")
        self.assertIn("停用", text)
        self.assertIn("联系管理员", text)

    def test_help_lists_enroll_only_for_confidential_list(self):
        service = object.__new__(BotService)
        service.config = {'statistics': {'allowed_groups': ['1'], 'private_users': ['12345']}}
        service.admins = set()
        service.authorized = lambda *_: True
        with patch('bot.private_commands.dispatch', return_value=(False, None)):
            self.assertIn('开通查询：', service.handle_text('12345', '帮助', '12345'))
            self.assertNotIn('开通查询：', service.handle_text('99999', '帮助', '99999'))
            self.assertIn('开通查询：', service.handle_text('12345', '帮助', 'group:1'))
            self.assertNotIn('开通查询：', service.handle_text('12345', '帮助', 'group:2'))
            # 连普通使用权限都没有的人，即使私聊也不显示
            service.authorized = lambda *_: False
            denied = service.handle_text('12345', '帮助', '12345')
            self.assertIn('没有使用权限', denied)
            self.assertNotIn('开通查询：', denied)


class MergeBehaviorTests(unittest.TestCase):
    def test_existing_commands_and_group_code_protection(self):
        self.assertEqual(parse_command('令牌分发')['op'], 'watch_token')
        self.assertEqual(parse_command('OCG饼图')['op'], 'ocg_pie')
        self.assertEqual(parse_command('统计对局 前100')['op'], 'game_counts')
        self.assertEqual(parse_command('选卡 1')['op'], 'card_select')
        service = object.__new__(BotService)
        service.config = {'statistics': {'allowed_groups': ['1'], 'private_users': ['12345']}}
        service.admins = set()
        service.authorized = lambda *_: True
        service._query_enroll = Mock(return_value='secret-code')
        with patch('bot.private_commands.dispatch', return_value=(False, None)):
            reply = service.handle_text('12345', '开通查询', 'group:1')
            self.assertNotIn('secret-code', reply)
            service._query_enroll.assert_not_called()
            self.assertEqual(service.handle_text('12345', '开通查询', '12345'), 'secret-code')

    def test_query_enroll_is_limited_to_confidential_list(self):
        service = object.__new__(BotService)
        service.config = {'statistics': {'allowed_groups': ['1'], 'private_users': ['12345']}}
        service.admins = set()
        service.authorized = lambda *_: True      # allow_all_users=true 时普通授权对所有人成立
        service._query_enroll = Mock(return_value='secret-code')
        with patch('bot.private_commands.dispatch', return_value=(False, None)):
            # 名单外私聊：明确拒绝且不发码
            self.assertIn('保密名单', service.handle_text('99999', '开通查询', '99999'))
            service._query_enroll.assert_not_called()
            # 名单外群：静默（同「观战设备」）
            self.assertIsNone(service.handle_text('99999', '开通查询', 'group:2'))
            # 名单内群：只提示私聊
            self.assertIn('私聊', service.handle_text('12345', '开通查询', 'group:1'))
            # 名单内私聊：发码
            self.assertEqual(service.handle_text('12345', '开通查询', '12345'), 'secret-code')

    def test_query_enroll_honors_explicit_remote_token_list(self):
        service = object.__new__(BotService)
        # 显式空 token_users 表示关闭该范围，不再回退 statistics.private_users
        service.config = {'remote': {'token_users': []},
                          'statistics': {'private_users': ['12345']}}
        service.admins = set()
        service.authorized = lambda *_: True
        service._query_enroll = Mock(return_value='secret-code')
        with patch('bot.private_commands.dispatch', return_value=(False, None)):
            self.assertIn('保密名单', service.handle_text('12345', '开通查询', '12345'))
            service._query_enroll.assert_not_called()

    def test_replay_cache_is_scoped_to_user(self):
        core = QueryCore(FakePortal(), store=object(), share=object(), replay_root=Path('.'))
        with patch.object(core, '_replay_links', side_effect=lambda identity, name: {'owner': identity}):
            self.assertEqual(core.player_replays('甲', '12345')['owner'], '12345')
            self.assertEqual(core.player_replays('甲', '54321')['owner'], '54321')

    def test_pie_and_deck_search_reuse_portal_roster(self):
        portal = FakePortal({'/statistics/api/roster': {
            'deck_distribution': [{'name': '青眼', 'players': 3}], 'deck_pie': {'total': 3},
            'covered_players': 3, 'current_players': 100}})
        core = QueryCore(portal, database=Path('unused'), replay_root=Path('unused'))
        self.assertEqual(core.pie()['items'][0]['count'], 3)
        self.assertEqual(core.search_decks('青眼')['items'][0]['name'], '青眼')
        self.assertEqual(len(portal.calls), 1)

    def test_enabled_setup_uses_separate_auth_and_closes_owned_store(self):
        from bot.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = Store(root / 'bot.sqlite3')
            existing.close()
            cfg = {'query': {'enabled': True, 'admin_token': 'local-test-admin-' + 'x' * 24},
                   'share': {'public_base_url': 'https://example.test'}}
            options = build_query(cfg, root, root / 'replays', log=lambda *_: None)
            self.assertIsInstance(options['query_auth'], QueryAuth)
            self.assertTrue((root / 'query-auth.sqlite3').is_file())
            self.assertFalse(options['writes_enabled'])
            core = options['query_core']
            try:
                core.subscribe('12345', '甲')
                self.assertEqual(core.subscription_list('54321')['items'], [])
                self.assertEqual(len(core.subscription_list('12345')['items']), 1)
            finally:
                core.close()
            self.assertFalse(core._owned_store)

    def test_disabled_setup_does_not_open_database(self):
        with patch('bot.store.Store', side_effect=AssertionError('must not open store')):
            self.assertEqual(build_query({'query': {'enabled': False}}, Path('.'), Path('.')), {})


class RealPortalBridgeTests(unittest.TestCase):
    def test_real_owner_cookie_and_query_paths(self):
        import test_card_statistics as C
        from bot.share import ReplayHTTPServer
        from bot.statistics_portal import StatisticsPortal, owner_secret
        from bot.statistics_server import make_handler
        C.CardStatisticsTests.setUp(self)
        secret_path = self.root / 'statistics-owner.key'
        portal = StatisticsPortal(self.store.path, self.replays, lambda: C.NOW, owner_secret(secret_path))
        server = ReplayHTTPServer(('127.0.0.1', 0), make_handler(portal))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = PortalClient('http://127.0.0.1:' + str(server.server_address[1]), secret_path)
        core = QueryCore(client, database=self.store.path, replay_root=self.replays)
        roster = core.top100()
        self.assertEqual(roster['covered_players'], 1)
        data = core.player_decks('甲')
        self.assertTrue(data['cards'])
        self.assertTrue(all('name' in card for card in data['cards']))
        self.assertEqual(core.pie()['covered'], roster['covered_players'])
        self.assertIsInstance(core.search_players('甲'), dict)
        self.assertIsInstance(core.search_cards('1'), dict)
        self.assertIsInstance(core.card_details(1), dict)


if __name__ == '__main__':
    unittest.main()
