import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from bot.account_guard import AccountMismatch, verify_transport_account
from bot.qq import OneBotClient, connection_hint
from bot.setup import probe_napcat


class Transport:
    def __init__(self, actual):
        self.actual, self.sent, self.closed = actual, [], False
    async def connect(self):
        pass
    async def send_text(self, text):
        self.sent.append(json.loads(text))
    async def recv_text(self):
        request = self.sent[-1]
        return json.dumps({'echo': request['echo'], 'retcode': 0, 'data':
                           {'user_id': self.actual} if request['action'] == 'get_login_info' else {'online': True}})
    async def close(self):
        self.closed = True


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_check_accepts_only_bound_account(self):
        await verify_transport_account(Transport(3528698561), '3528698561')
        with self.assertRaises(AccountMismatch):
            await verify_transport_account(Transport(1617562300), 3528698561)

    async def test_probe_rejects_online_wrong_account_without_sending_qq(self):
        t = Transport(1617562300)
        ok, message = await probe_napcat({'qq': {'access_token': 'secret', 'expected_self_id': 3528698561}}, transport=t)
        self.assertFalse(ok)
        self.assertIn('1617562300', message)
        self.assertIn('3528698561', message)
        self.assertNotIn('secret', message)
        self.assertTrue(t.closed)
        self.assertEqual([m['action'] for m in t.sent], ['get_login_info'])

    async def test_probe_correct_account_checks_online_too(self):
        ok, _ = await probe_napcat({'qq': {'access_token': 'secret', 'expected_self_id': 3528698561}}, transport=Transport(3528698561))
        self.assertTrue(ok)

    async def test_runtime_wrong_account_never_becomes_connected_or_dispatches(self):
        t = Transport(1617562300)
        statuses, events = [], []
        async def pause(_):
            client.stop()
        client = OneBotClient('ws://local', 'secret', events.append, transport=t, pause=pause,
                              expected_self_id=3528698561, on_status=statuses.append)
        await client.run()
        self.assertFalse(client.connected)
        self.assertEqual(events, [])
        self.assertTrue(t.closed)
        self.assertIn('登录账号错误', statuses[0])

    async def test_account_probe_detects_wrong_account_when_port_closed(self):
        t = Transport(1617562300)
        t.connect = AsyncMock(side_effect=ConnectionRefusedError())
        async def pause(_):
            client.stop()
        client = OneBotClient('ws://local', 'secret', None, transport=t, pause=pause,
                              expected_self_id=3528698561, account_probe=lambda: '1617562300')
        await client.run()
        self.assertEqual(t.sent, [])
        self.assertFalse(client.connected)

    def test_hint_preserves_specific_identity_message(self):
        self.assertIn('应为工具号', connection_hint(AccountMismatch('3528698561', '1617562300')))
