"""用离线会话刷新替身验证入房身份，不访问游戏服或真实账号。"""
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import watch


class WatchAuthIdentityTests(unittest.IsolatedAsyncioTestCase):
    def spectator(self, **overrides):
        config = dict(host='test.invalid', port=8911, nickname='Spectator',
                      join_mode='mycard', mycard_secret=None)
        config.update(overrides)
        return watch.Spectator(config,
            watch.RoomInfo(key='test', title='test', join_id='ROOM123', players=[]),
            watch.ReplayWriter(Path('unused-test-output'), []))

    async def run_fake(self, spectator, refresh, outcomes=None):
        connect = AsyncMock(return_value=True)
        observe = AsyncMock(side_effect=outcomes or [('leave', False)])
        with patch.object(watch, 'refresh_secret', side_effect=refresh), \
             patch.object(watch, 'room_label_with_ranks', return_value='test room'), \
             patch.object(watch, 'log'), \
             patch.object(watch.asyncio, 'sleep', new=AsyncMock()), \
             patch.object(spectator, '_connect', connect), \
             patch.object(spectator, '_watch', observe), \
             patch.object(spectator, 'write_observed_decks'):
            result = await spectator.run()
        return result, connect

    async def test_first_join_sends_authenticated_identity_and_its_secret(self):
        spectator = self.spectator()
        real_refresh = watch.refresh_secret
        session = {'token': 'unit-test-token', 'username': 'authenticated-user'}
        with patch.object(watch, 'load_session', return_value=session), \
             patch.object(watch, 'save_session'), \
             patch.object(watch, 'fetch_u16_secret', return_value=123456):
            result, connect = await self.run_fake(spectator, real_refresh)
        self.assertEqual(result, 'leave')
        host, port, nickname, password = connect.await_args.args
        self.assertEqual(nickname, 'authenticated-user')
        self.assertEqual(password, watch.encode_mycard_password(123456, 3, 'ROOM123'))

    async def test_reconnect_uses_the_identity_from_each_refresh(self):
        spectator = self.spectator()
        identities = iter([('first-identity', 123), ('renewed-identity', 456)])
        def refresh(config):
            username, secret = next(identities)
            config.update(nickname=username, mycard_secret=secret)
        result, connect = await self.run_fake(spectator, refresh,
                                            [('retry', True), ('leave', False)])
        calls = [call.args for call in connect.await_args_list]
        self.assertEqual([call[2] for call in calls], ['first-identity', 'renewed-identity'])
        self.assertEqual([call[3] for call in calls], [
            watch.encode_mycard_password(123, 3, 'ROOM123'),
            watch.encode_mycard_password(456, 3, 'ROOM123')])

    async def test_plain_room_keeps_configured_nickname_without_account_requests(self):
        spectator = self.spectator(join_mode='plain', nickname='local-spectator')
        real_refresh = watch.refresh_secret
        with patch.object(watch, 'http_json', side_effect=AssertionError('unexpected HTTP')):
            result, connect = await self.run_fake(spectator, real_refresh)
        self.assertEqual(connect.await_args.args[2:], ('local-spectator', 'ROOM123'))

    async def test_failed_refresh_does_not_connect_with_an_unverified_identity(self):
        spectator = self.spectator()
        def refresh(config):
            raise RuntimeError('refresh unavailable')
        result, connect = await self.run_fake(spectator, refresh)
        self.assertEqual(result, 'config')
        connect.assert_not_awaited()
