import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.tunnel import QuickTunnel
from bot.ssh_tunnel import LocalTunnelWorker, hostname_from_event, ssh_command
from bot.watch_core import ReplayWriter, _rank_cache
from test_bot import FakeQQ, link_token, temporary_service


class TunnelTests(unittest.TestCase):
    def test_only_connected_matching_tunnel_provides_a_public_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            tunnel = QuickTunnel(root, data, 'http://127.0.0.1:8787')
            with patch.object(tunnel, '_get_status', return_value={}) as getter:
                self.assertEqual(tunnel.current_url(), '')
                getter.assert_not_called()
            tunnel.state_path.write_text(json.dumps({'origin': tunnel.origin, 'metrics_port': 47879,
                                                    'provider': 'localhost.run', 'instance': 'abc'}))
            response = {'service': 'qq-replay-tunnel', 'origin': tunnel.origin, 'instance': 'abc',
                        'hostname': 'example-bot.lhr.life', 'ready': True}
            with patch.object(tunnel, '_get_status', return_value=response):
                self.assertEqual(tunnel.current_url(), 'https://example-bot.lhr.life')
                response['instance'] = 'different-worker'
                self.assertEqual(tunnel.current_url(), '')
                response['instance'] = 'abc'
                response['hostname'] = 'attacker.example'
                self.assertEqual(tunnel.current_url(), '')
            with patch.object(tunnel, '_get_status', side_effect=OSError):
                self.assertEqual(tunnel.current_url(), '')
            with self.assertRaises(ValueError):
                QuickTunnel(root, data, 'http://external.example:3001')

    def test_starts_hidden_helper_for_replay_port_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel = QuickTunnel(root, root / 'data', 'http://127.0.0.1:8787')
            tunnel.executable = root / 'tools/ssh.exe'
            tunnel.executable.parent.mkdir()
            tunnel.executable.write_bytes(b'fake-exe-never-executed')
            with patch.object(tunnel, '_metrics_open', return_value=False), \
                    patch('bot.tunnel.subprocess.Popen', return_value=Mock(pid=123)) as spawn:
                self.assertTrue(tunnel.ensure_started())
                arguments = spawn.call_args.args[0]
                self.assertEqual(arguments[arguments.index('--origin') + 1], 'http://127.0.0.1:8787')
                self.assertIn('47879', arguments)
                self.assertIn('bot.ssh_tunnel', arguments)
                self.assertNotIn('3001', arguments)
                self.assertNotIn('6099', arguments)
                with patch.object(tunnel, '_metrics_open', return_value=True), \
                        patch.object(tunnel, '_status', return_value={'ready': False}):
                    self.assertTrue(tunnel.ensure_started())
                self.assertEqual(spawn.call_count, 1)

    def test_other_service_on_metrics_port_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel = QuickTunnel(root, root / 'data', 'http://127.0.0.1:8787')
            with patch.object(tunnel, '_metrics_open', return_value=True), \
                    patch('bot.tunnel.subprocess.Popen') as spawn:
                self.assertFalse(tunnel.ensure_started())
                self.assertIn('占用', tunnel.last_error)
                spawn.assert_not_called()

    def test_tunnel_outage_keeps_notifications_and_new_domain_does_not_extend_old_link(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 推送')
            service.tunnel = object()
            current = ['']
            service.share.public_url_provider = lambda: current[0]
            _rank_cache['甲'], _rank_cache['乙'] = 12, 88
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            writer.save(b'public-replay', ['甲', '乙'])
            service.qq = FakeQQ()
            self.assertEqual(asyncio.run(service.deliver()), 0)
            self.assertEqual(service.store.pending_notifications()[0]['attempts'], 0)
            self.assertNotIn('127.0.0.1', service.handle_text('10001', '录像 甲'))
            current[0] = 'https://old.lhr.life'
            first = service.handle_text('10001', '录像 甲')
            old = service.store.get_link(link_token(first))
            self.assertIn(current[0], first)
            current[0] = 'https://new.lhr.life'
            service.clock.now += 10
            second = service.handle_text('10001', '录像 甲')
            self.assertIn(current[0], second)
            self.assertEqual(service.store.get_link(old['token'])['expires_at'], old['expires_at'])
            self.assertEqual(asyncio.run(service.deliver()), 1)

    def test_ssh_event_parsing_only_accepts_registered_tunnel_hostname(self):
        event = {'type': 'v1.tcpip_forward.register.accepted', 'id': 'localhost/31fc689e0d0ccd.lhr.life'}
        self.assertEqual(hostname_from_event(json.dumps(event)), '31fc689e0d0ccd.lhr.life')
        event['type'] = 'error'
        self.assertEqual(hostname_from_event(json.dumps(event)), '')
        event['type'] = 'v1.tcpip_forward.register.accepted'
        event['id'] = 'localhost/evil.example'
        self.assertEqual(hostname_from_event(json.dumps(event)), '')
        for line in ['[]', '{}', 'welcome https://admin.localhost.run/', 'null']:
            self.assertEqual(hostname_from_event(line), '')

    def test_ssh_uses_only_replay_forward_and_no_personal_identity_or_configuration(self):
        command = ssh_command(Path('ssh.exe'), 'http://127.0.0.1:8787', Path('data'))
        self.assertEqual(command[command.index('-R') + 1], '80:127.0.0.1:8787')
        self.assertIn('nokey@localhost.run', command)
        self.assertIn('IdentityFile=none', command)
        self.assertIn('IdentityAgent=none', command)
        self.assertIn('-F', command)
        self.assertNotIn('3001', ' '.join(command))
        self.assertNotIn('6099', ' '.join(command))
        for origin in ['https://127.0.0.1:8787', 'http://external.example:8787',
                       'http://user:password@127.0.0.1:8787', 'http://127.0.0.1:8787/secret']:
            with self.assertRaises(ValueError):
                ssh_command(Path('ssh.exe'), origin, Path('data'))

    def test_dead_ssh_process_never_reports_ready_even_if_old_hostname_remains(self):
        worker = LocalTunnelWorker('http://127.0.0.1:8787', Path('data'), 'abc', Path('ssh.exe'))
        worker.hostname = 'example.lhr.life'
        worker.process = Mock()
        worker.process.poll.return_value = None
        self.assertTrue(worker.status()['ready'])
        worker.process.poll.return_value = 255
        self.assertFalse(worker.status()['ready'])
        self.assertEqual(worker.status()['hostname'], '')

    def test_disconnect_during_link_creation_does_not_send_local_link_or_consume_notification(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 甲 全部 推送')
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            writer.save(b'replay-before-disconnect', ['甲', '乙'])
            service.tunnel = object()
            service.qq = FakeQQ()
            service.share.public_url_provider = Mock(side_effect=['https://test.lhr.life',
                                                                 'https://test.lhr.life', ''])
            self.assertEqual(asyncio.run(service.deliver()), 0)
            self.assertEqual(service.store.pending_notifications()[0]['attempts'], 0)


if __name__ == '__main__':
    unittest.main()
