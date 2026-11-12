import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.commands import parse_command
from bot.service import BotService
from bot import watch_token_command as tokens


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = object.__new__(BotService)
        s = self.service
        s.data_dir = Path(self.tmp.name)
        s.config = {'query': {'enabled': True, 'admin_token': 'test-admin'},
                    'statistics': {'allowed_groups': ['123']}}
        s.admins = {'99999'}
        s.authorized = lambda *_: True
        s._bind_pending_active = lambda *_: False
        s._take_confidential_retry = lambda *_: False
        s._query_enroll = Mock(return_value='test-code')
        s.store = SimpleNamespace(path=s.data_dir / 'bot.sqlite3')
        self.dispatch = patch('bot.private_commands.dispatch', return_value=(False, None))
        self.dispatch.start()
        self.addCleanup(self.dispatch.stop)

    def call(self, user, text, conversation=''):
        return self.service.handle_text(user, text, conversation)

    def test_both_designated_users_can_enroll_target(self):
        for user in tokens.DELEGATE_USERS:
            self.assertEqual(self.call(user, '开通查询 1347226440'), 'test-code')
            self.service._query_enroll.assert_called_with('1347226440', delegated=True)

    def test_other_admin_cannot_delegate(self):
        self.assertIn('仅指定管理员', self.call('99999', '开通查询 1347226440'))
        self.service._query_enroll.assert_not_called()
        self.assertIn('仅指定管理员', tokens.handle(self.service, '99999', ['1347226440']))
        self.assertFalse(tokens.granted(self.service, '1347226440', 'watch_token'))

    def test_watch_owner_and_persistent_scoped_permission(self):
        with patch.object(tokens, '_issue', return_value='test-token') as issue:
            self.assertIn('test-token', self.call('1278389701', '令牌分发 1347226440'))
            issue.assert_called_once_with(self.service, '1347226440', 1, '')
        other = SimpleNamespace(data_dir=self.service.data_dir)
        self.assertTrue(tokens.granted(other, '1347226440', 'watch_token'))
        self.assertFalse(tokens.granted(other, '1347226440', 'query_enroll'))
        self.assertNotIn('test-token', (other.data_dir / 'confidential-grants.json').read_text())

    def test_real_token_is_bound_to_target_and_shares_target_cooldown(self):
        s = self.service
        try:
            reply = self.call('1278389701', '令牌分发 1347226440')
            self.assertIn('客户端名称：android-1347226440', reply)
            token = reply.split('接口令牌：', 1)[1].splitlines()[0]
            row = tokens._store(s).verify(token)
            self.assertEqual(row['owner'], '1347226440')
            self.assertEqual(row['worker'], 'android-1347226440')
            self.assertIn('30 秒', self.call('3572605808', '令牌分发 1347226440'))
            self.assertIn('30 秒', self.call('1347226440', '令牌分发'))
        finally:
            if getattr(s, '_watch_token_store', None):
                s._watch_token_store.close()

    def test_granted_user_can_get_own_token_with_no_general_access(self):
        tokens.grant(self.service, '1347226440', 'watch_token')
        self.service.authorized = lambda *_: False
        with patch.object(tokens, '_issue', return_value='own-token') as issue:
            self.assertEqual(self.call('1347226440', '令牌分发'), 'own-token')
            issue.assert_called_once_with(self.service, '1347226440', 1, '')
        self.assertNotEqual(self.call('1347226440', '开通查询'), 'test-code')

    def test_query_success_grants_and_existing_account_reissues(self):
        del self.service._query_enroll
        result = {'ok': True, 'identity': '1347226440', 'code': 'test-only'}
        with patch('bot.query_core.admin_issue', return_value={'ok': False, 'error': 'already_provisioned'}), \
             patch('bot.query_core.admin_reissue', return_value=result) as reissue:
            self.assertIn('test-only', self.call('3572605808', '开通查询 1347226440'))
            self.assertEqual(reissue.call_args.args[-1], '1347226440')
        self.assertTrue(tokens.granted(self.service, '1347226440', 'query_enroll'))
        self.assertFalse(tokens.granted(self.service, '1347226440', 'watch_token'))
        self.service.authorized = lambda *_: False
        self.service._query_enroll = Mock(return_value='own-code')
        self.assertEqual(self.call('1347226440', '开通查询'), 'own-code')
        self.service._query_enroll.assert_called_once_with('1347226440')

    def test_failed_enroll_does_not_grant(self):
        del self.service._query_enroll
        with patch('bot.query_core.admin_issue', return_value={'ok': False, 'error': 'unavailable'}):
            self.call('1278389701', '开通查询 1347226440')
        self.assertFalse(tokens.granted(self.service, '1347226440', 'query_enroll'))

    def test_group_sends_only_to_requesting_admin(self):
        s = self.service
        s.group_commands = True
        s.group_without_at = False
        s.qq = SimpleNamespace(send_private=Mock(), send_group=Mock(),
                               send_group_forward=Mock(), send_private_forward=Mock())
        async def delivered(*args):
            return True
        from unittest.mock import AsyncMock
        s.qq.send_private = AsyncMock(side_effect=delivered)
        s.qq.send_group = AsyncMock()
        event = {'message_type': 'group', 'group_id': 123, 'user_id': 1278389701,
                 'self_id': 55555, 'message': '[CQ:at,qq=55555]开通查询 1347226440'}
        asyncio.run(s.on_qq_event(event))
        s.qq.send_private.assert_awaited_once_with(1278389701, 'test-code')
        s.qq.send_group.assert_not_awaited()

    def test_invalid_targets_and_existing_slots(self):
        for text in ['开通查询 abc', '开通查询 1234', '开通查询 12345 67890']:
            self.assertEqual(parse_command(text)['op'], 'error')
        self.assertEqual(parse_command('令牌分发 2')['args'], ['2'])
        with patch.object(tokens, '_issue', return_value='slot-two') as issue:
            self.assertEqual(tokens.handle(self.service, '1278389701', ['2']), 'slot-two')
            issue.assert_called_once_with(self.service, '1278389701', 2, '')

    def test_non_confidential_group_cannot_delegate(self):
        self.assertIsNone(self.call('1278389701', '开通查询 1347226440', 'group:456'))
        self.service._query_enroll.assert_not_called()
        self.assertIsNone(self.call('1278389701', '令牌分发 1347226440', 'group:456'))

    def temp_context(self, result, *, event=None, op='watch_token'):
        from unittest.mock import AsyncMock
        self.service.qq = SimpleNamespace(call=AsyncMock(return_value=result))
        event = event or {'message_type': 'private', 'sub_type': 'group', 'user_id': 1174338198}
        return asyncio.run(self.service._confidential_private_context(event, op))

    def test_temp_member_can_issue_with_private_context(self):
        context = self.temp_context({'retcode': 0, 'data': {'user_id': 1174338198, 'group_id': 123}})
        self.assertEqual(context, 'confidential-private:123')
        self.service.authorized = lambda *_: False
        with patch.object(tokens, '_issue', return_value='private-token') as issue:
            self.assertEqual(self.call('1174338198', '令牌分发', context), 'private-token')
            issue.assert_called_once_with(self.service, '1174338198', 1, context)
        self.assertFalse(tokens.granted(self.service, '1174338198', 'watch_token'))
        self.assertFalse(tokens.confidential_allowed(self.service, '1174338198', context))

    def test_temp_context_supports_query_without_broad_permission(self):
        context = self.temp_context({'retcode': 0, 'data': {'user_id': 1174338198, 'group_id': 123}}, op='query_enroll')
        self.service.authorized = lambda *_: False
        self.assertEqual(self.call('1174338198', '开通查询', context), 'test-code')
        self.assertIn('没有使用权限', self.call('1174338198', '订阅 test', context))

    def test_temp_bad_membership_and_missing_data_fail_closed(self):
        for result in [{'retcode': 1}, {'retcode': 0, 'data': {}},
                       {'retcode': 0, 'data': {'user_id': 99999, 'group_id': 123}},
                       {'retcode': 0, 'data': {'user_id': 1174338198, 'group_id': 456}}]:
            self.assertEqual(self.temp_context(result), '')

    def test_explicit_non_confidential_source_never_probes_other_groups(self):
        event = {'message_type': 'private', 'sub_type': 'group', 'group_id': 456, 'user_id': 1174338198}
        self.assertEqual(self.temp_context({}, event=event), '')
        self.service.qq.call.assert_not_awaited()

    def test_ordinary_private_and_unrelated_commands_never_probe(self):
        self.assertEqual(self.temp_context({}, event={'message_type': 'private', 'sub_type': 'friend', 'user_id': 1174338198}), '')
        self.service.qq.call.assert_not_awaited()
        self.assertEqual(self.temp_context({}, op='subscribe'), '')
        self.service.qq.call.assert_not_awaited()

    def test_existing_grant_works_when_temp_source_is_missing(self):
        tokens.grant(self.service, '1174338198', 'watch_token')
        self.assertEqual(self.temp_context({}), '')
        self.service.qq.call.assert_not_awaited()
        with patch.object(tokens, '_issue', return_value='granted-token'):
            self.assertEqual(self.call('1174338198', '令牌分发'), 'granted-token')

    def test_temp_api_failure_does_not_grant(self):
        from unittest.mock import AsyncMock
        self.service.qq = SimpleNamespace(call=AsyncMock(side_effect=TimeoutError))
        event = {'message_type': 'private', 'sub_type': 'group', 'user_id': 1174338198}
        self.assertEqual(asyncio.run(self.service._confidential_private_context(event, 'watch_token')), '')
        self.assertFalse(tokens.granted(self.service, '1174338198', 'watch_token'))

    def test_temp_event_keeps_reply_private(self):
        from unittest.mock import AsyncMock
        s = self.service
        s._top100_deck_candidates = {}
        s.qq = SimpleNamespace(
            call=AsyncMock(return_value={'retcode': 0, 'data': {'user_id': 1174338198, 'group_id': 123}}),
            send_private=AsyncMock(return_value=True), send_group=AsyncMock(),
            send_group_forward=AsyncMock(), send_private_forward=AsyncMock())
        event = {'message_type': 'private', 'sub_type': 'group', 'user_id': 1174338198,
                 'message': '令牌分发', 'temp_source': 0}
        with patch.object(tokens, '_issue', return_value='private-token'):
            asyncio.run(s.on_qq_event(event))
        s.qq.send_private.assert_awaited_once_with(1174338198, 'private-token')
        s.qq.send_group.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
