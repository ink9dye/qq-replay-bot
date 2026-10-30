import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.qq import NapCatTransport, OneBotClient, connection_hint
from bot.setup import (config_issues, initial_config, napcat_servers, parse_users,
                       probe_napcat, read_config, save_config, valid_public_url, wizard)
from bot.watch_core import ReplayWriter, WebSocketClient, _rank_cache
from test_bot import FakeQQ, temporary_service


class SetupTests(unittest.TestCase):
    def test_discovery_only_uses_enabled_local_ws(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'NapCat.OneKey/NapCat.123.Shell/versions/1/resources/app/napcat/config/onebot11_12345.json'
            path.parent.mkdir(parents=True)
            servers = [
                {'enable': True, 'host': '127.0.0.1', 'port': 3001, 'token': 'correct-secret'},
                {'enable': False, 'host': '127.0.0.1', 'port': 3002, 'token': 'disabled'},
                {'enable': True, 'host': 'other.example', 'port': 3003, 'token': 'remote'},
            ]
            path.write_text(json.dumps({'network': {'websocketServers': servers}}), encoding='utf-8')
            self.assertEqual(napcat_servers(root), [{'url': 'ws://127.0.0.1:3001', 'token': 'correct-secret'}])

    def test_config_backup_and_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{"qq": {"access_token": "old-secret"}}', encoding='utf-8-sig')
            self.assertEqual(read_config(path)['qq']['access_token'], 'old-secret')
            save_config(path, {'qq': {'access_token': 'new-secret'}})
            self.assertEqual(read_config(path)['qq']['access_token'], 'new-secret')
            backups = list((path.parent / 'data/config-backups').glob('*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(read_config(backups[0])['qq']['access_token'], 'old-secret')
            self.assertEqual(list(path.parent.glob('.config-*.tmp')), [])

    def test_users_urls_and_sanitized_errors(self):
        self.assertEqual(parse_users('12345，67890 12345'), [12345, 67890])
        for value in ('', 'abc', '12345,bot', '012345'):
            with self.assertRaises(ValueError):
                parse_users(value)
        self.assertTrue(valid_public_url('https://example.trycloudflare.com'))
        for value in ('http://example.org', 'https://127.0.0.1', 'https://host/s/token', 'https://user:secret@host'):
            self.assertFalse(valid_public_url(value))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('SECRET INVALID JSON', encoding='utf-8')
            with self.assertRaises(ValueError) as caught:
                read_config(path)
            self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('private-secret', connection_hint(RuntimeError('HTTP 401 private-secret')))

    def test_wizard_import_preserves_other_settings_and_hides_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = {'qq': {'access_token': 'webui-wrong', 'users': []},
                        'mycard': {'account': 'existing', 'password': 'existing-secret'},
                        'share': {}, 'watch': {'max_concurrent': 7}, 'data_dir': 'existing-data'}
            (root / 'config.json').write_text(json.dumps(original), encoding='utf-8')
            server = [{'url': 'ws://127.0.0.1:3001', 'token': 'interface-secret'}]
            with patch('bot.setup.napcat_servers', return_value=server), \
                    patch('builtins.input', side_effect=['', '12345', '', '']), \
                    patch('bot.setup.getpass.getpass', return_value=''), \
                    patch('builtins.print') as printer:
                wizard(root)
            saved = read_config(root / 'config.json')
            self.assertEqual(saved['qq']['access_token'], 'interface-secret')
            self.assertEqual(saved['qq']['users'], [12345])
            self.assertEqual(saved['mycard'], original['mycard'])
            self.assertEqual(saved['watch'], original['watch'])
            output = str(printer.call_args_list)
            self.assertNotIn('interface-secret', output)
            self.assertNotIn('existing-secret', output)

    def test_wizard_cancel_never_changes_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'config.json'
            path.write_text('{"qq": {}, "mycard": {}}', encoding='utf-8')
            before = path.read_bytes()
            with patch('bot.setup.napcat_servers', return_value=[]), \
                    patch('builtins.input', side_effect=KeyboardInterrupt), patch('builtins.print'):
                with self.assertRaises(KeyboardInterrupt):
                    wizard(root)
            self.assertEqual(path.read_bytes(), before)


class NotificationChangesTests(unittest.TestCase):
    def test_public_mode_isolates_subscriptions_and_links_per_user(self):
        with temporary_service() as service:
            self.assertFalse(service.authorized('55555'))
            service.allow_all_users = True
            self.assertTrue(service.authorized('55555'))
            self.assertFalse(service.authorized(None))
            self.assertIn('已订阅', service.handle_text('55555', '订阅 甲 全部 静默'))
            unsubscribed = service.handle_text('66666', '录像 甲')
            self.assertIn('未订阅', unsubscribed)
            self.assertIn('默认仅查看', unsubscribed)
            self.assertIn('没有这个订阅', service.handle_text('66666', '取消订阅 甲'))
            self.assertIsNotNone(service.store.get_subscription('55555', '甲'))
            self.assertIn('/s/', service.handle_text('55555', '录像 甲'))
            service.config['qq']['allow_all_users'] = True
            service.config['qq']['users'] = []
            service.config['qq']['admins'] = []
            self.assertFalse(any('授权：' in issue for issue in config_issues(service.config)))

    def test_pending_notices_recheck_silent_rank_and_authorization(self):
        for change in ('silent', 'rank', 'revoke'):
            with self.subTest(change=change), temporary_service() as service:
                service.handle_text('10001', '订阅 甲 全部 推送')
                _rank_cache['乙'] = 50
                writer = ReplayWriter(service.replay_root, ['甲'])
                writer.on_saved = service.on_replay_saved
                writer.save(b'notice-change', ['甲', '乙'])
                replay_id = service.store.replays_for_player('甲')[0]['id']
                self.assertEqual(len(service.store.pending_notifications()), 1)
                if change == 'silent':
                    service.handle_text('10001', '静默 甲')
                elif change == 'rank':
                    service.handle_text('10001', '订阅 甲 前10 推送')
                else:
                    service.users.discard('10001')
                    self.assertEqual(service.current_rules(), ([], []))
                fake = FakeQQ()
                service.qq = fake
                self.assertEqual(asyncio.run(service.deliver()), 0)
                self.assertEqual(fake.sent, [])
                self.assertEqual(service.store.notification_status('10001', replay_id), 'skipped')

    def test_resubscribe_delivers_shared_pending_replay(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 推送')
            _rank_cache['乙'] = 50
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            writer.save(b'notice-change', ['甲', '乙'])
            replay_id = service.store.replays_for_player('甲')[0]['id']
            self.assertEqual(len(service.store.pending_notifications()), 1)
            service.handle_text('10001', '取消订阅 甲')
            service.clock.now += 100
            service.handle_text('10001', '订阅 甲 全部 推送')
            fake = FakeQQ()
            service.qq = fake
            self.assertEqual(asyncio.run(service.deliver()), 1)
            self.assertEqual(len(fake.sent), 1)
            self.assertEqual(service.store.notification_status('10001', replay_id), 'sent')


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_handshake_read_only_probe_and_masking(self):
        keys, masks, actions = [], [], []
        errors = []

        async def server(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(b': ', 1) for line in request.split(b'\r\n')[1:] if b': ' in line)
                self.assertEqual(headers[b'Authorization'], b'Bearer interface-secret')
                key = headers[b'Sec-WebSocket-Key']
                self.assertEqual(len(base64.b64decode(key)), 16)
                keys.append(key)
                accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: keep-alive,Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                await writer.drain()
                header = await reader.readexactly(2)
                self.assertTrue(header[1] & 128)
                length = header[1] & 127
                if length == 126:
                    length = int.from_bytes(await reader.readexactly(2), 'big')
                mask = await reader.readexactly(4)
                masks.append(mask)
                body = await reader.readexactly(length)
                action = json.loads(bytes(byte ^ mask[i % 4] for i, byte in enumerate(body)))
                actions.append(action['action'])
                response = json.dumps({'echo': action['echo'], 'retcode': 0, 'data': {'online': True}}).encode()
                writer.write(bytes([129, len(response)]) + response)
                await writer.drain()
            except BaseException as exc:
                errors.append(exc)
            finally:
                writer.close()
                await writer.wait_closed()

        listener = await asyncio.start_server(server, '127.0.0.1', 0)
        port = listener.sockets[0].getsockname()[1]
        config = {'qq': {'onebot_ws': f'ws://127.0.0.1:{port}', 'access_token': 'interface-secret'}}
        try:
            for _ in range(2):
                ok, text = await probe_napcat(config)
                self.assertTrue(ok, text)
            self.assertEqual(actions, ['get_status', 'get_status'])
            self.assertEqual(len(set(keys)), 2)
            self.assertEqual(len(set(masks)), 2)
            self.assertEqual(errors, [])
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_wrong_token_closes_transport_and_probe_hides_response(self):
        async def server(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 401 private-secret\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        listener = await asyncio.start_server(server, '127.0.0.1', 0)
        port = listener.sockets[0].getsockname()[1]
        transport = NapCatTransport(f'ws://127.0.0.1:{port}', 'wrong-secret')
        try:
            ok, message = await probe_napcat({'qq': {'access_token': 'wrong-secret'}}, transport=transport)
            self.assertFalse(ok)
            self.assertIn('Token', message)
            self.assertNotIn('private-secret', message)
            self.assertNotIn('wrong-secret', message)
            self.assertIsNone(transport.client)
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_invalid_handshake_is_rejected(self):
        async def server(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: wrong\r\n\r\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        listener = await asyncio.start_server(server, '127.0.0.1', 0)
        transport = NapCatTransport(f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}', 'token')
        try:
            with self.assertRaises(RuntimeError):
                await transport.connect()
            self.assertIsNone(transport.client)
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_cancellation_clears_connection_and_closes_transport(self):
        class WaitingTransport:
            def __init__(self):
                self.started = asyncio.Event()
                self.closed = 0
            async def connect(self):
                self.started.set()
            async def recv_text(self):
                await asyncio.Future()
            async def close(self):
                self.closed += 1
        transport = WaitingTransport()
        client = OneBotClient('ws://local', 'token', None, transport=transport)
        task = asyncio.create_task(client.run())
        await transport.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(client.connected)
        self.assertGreaterEqual(transport.closed, 1)


if __name__ == '__main__':
    unittest.main()
