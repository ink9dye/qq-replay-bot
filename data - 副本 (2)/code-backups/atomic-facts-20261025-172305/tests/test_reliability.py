import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.reliability import OutboundGate, RequestGuard, restriction_hint
from bot.qq import OneBotClient
from bot.launcher import launch_services, startup_lock
from test_bot import temporary_service, FakeQQ
from bot.watch_core import ReplayWriter, _rank_cache


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_global_spacing_per_receiver_and_success_dedup(self):
        now, sent = [0], []
        async def pause(seconds):
            now[0] += seconds
        async def send(name):
            sent.append((name, now[0]))
            return True
        gate = OutboundGate(1, 2, 15, clock=lambda: now[0], pause=pause)
        await asyncio.gather(gate.submit('a', 'one', lambda: send('a')),
                             gate.submit('b', 'two', lambda: send('b')),
                             gate.submit('a', 'three', lambda: send('a')))
        self.assertEqual(sent, [('a', 0), ('b', 1), ('a', 2)])
        self.assertTrue(await gate.submit('a', 'three', lambda: send('duplicate')))
        self.assertEqual(len(sent), 3)
        now[0] = 18
        await gate.submit('a', 'three', lambda: send('fresh'))
        self.assertEqual(sent[-1], ('fresh', 18))

    async def test_queue_limit_is_bounded_and_cancellation_releases_capacity(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def wait():
            started.set()
            await release.wait()
            return True
        gate = OutboundGate(limit=1)
        task = asyncio.create_task(gate.submit('a', 'first', wait))
        await started.wait()
        self.assertFalse(await gate.submit('b', 'second', wait))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(gate.waiting, 0)
        release.set()
        self.assertTrue(await gate.submit('b', 'second', wait))

    async def test_explicit_account_restriction_pauses_without_fallback_or_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []
            async def send(raw):
                request = json.loads(raw)
                sent.append(request)
                client._pending[request['echo']].set_result({'retcode': 1, 'message': '发送消息受限 secret-response'})
            transport = SimpleNamespace(send_text=send, close=AsyncMock())
            client = OneBotClient('ws://local', 'secret-token', None, transport=transport, state_dir=tmp)
            client.connected = True
            self.assertFalse(await client.send_private(10001, 'hello'))
            self.assertTrue(client.suspended)
            self.assertEqual(len(sent), 1)
            self.assertFalse(await client.send_private(10001, 'another'))
            self.assertEqual(len(sent), 1)
            self.assertTrue((Path(tmp) / 'qq-paused.json').exists())
            self.assertNotIn('secret', (Path(tmp) / 'qq-paused.json').read_text(encoding='utf8'))
            (Path(tmp) / 'qq-resume.request').touch()
            self.assertTrue(client.consume_resume_request())
            self.assertFalse(client.suspended)
            self.assertFalse(client.connected)

    async def test_pause_survives_restart_and_does_not_attempt_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'qq-paused.json').write_text('{}')
            transport = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
            async def pause(_):
                client.stop()
            client = OneBotClient('ws://local', 'token', None, transport=transport, state_dir=tmp, pause=pause)
            await client.run()
            transport.connect.assert_not_called()

    async def test_network_retries_back_off_without_restarting_qq(self):
        transport = SimpleNamespace(connect=AsyncMock(side_effect=ConnectionRefusedError()), close=AsyncMock())
        delays = []
        async def pause(delay):
            delays.append(delay)
            if len(delays) == 3:
                client.stop()
        client = OneBotClient('ws://local', 'token', None, transport=transport, pause=pause)
        with patch('bot.launcher.restart_napcat') as restart:
            await client.run()
        self.assertEqual(delays, [1, 2, 4])
        restart.assert_not_called()

    async def test_wrong_login_in_startup_never_triggers_automatic_qq_restart(self):
        spawned = []
        with tempfile.TemporaryDirectory() as tmp, patch('bot.launcher.restart_napcat') as restart, patch('builtins.print'):
            ok = await launch_services(Path(tmp), {}, spawn=spawned.append,
                probe=AsyncMock(return_value=(False, '登录账号错误')),
                state=lambda _: 'stopped', port_open=lambda _: True)
        self.assertFalse(ok)
        self.assertEqual(spawned, [])
        restart.assert_not_called()

    async def test_paused_startup_runs_recording_without_starting_or_probing_qq(self):
        spawned = []
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.print'):
            root = Path(tmp)
            (root / 'data').mkdir()
            (root / 'data/qq-paused.json').write_text('{}')
            probe = AsyncMock()
            states = iter(['stopped', 'ready'])
            ok = await launch_services(root, {}, spawn=spawned.append, probe=probe,
                state=lambda _: next(states), port_open=lambda _: False)
        self.assertTrue(ok)
        self.assertEqual(spawned, [root / 'start.bat'])
        probe.assert_not_called()


class LocalGuardTests(unittest.TestCase):
    def test_request_cooldown_and_duplicate_are_separate_per_user_and_group(self):
        now = [0]
        guard = RequestGuard(.8, 8, clock=lambda: now[0])
        self.assertTrue(guard.accept('group:1', 'a', '查卡 白龙'))
        now[0] = .1
        self.assertFalse(guard.accept('group:1', 'a', '选卡1'))
        self.assertTrue(guard.accept('group:1', 'b', '查卡 白龙'))
        self.assertTrue(guard.accept('group:2', 'a', '查卡 白龙'))
        now[0] = 1
        self.assertFalse(guard.accept('group:1', 'a', '查卡 白龙'))
        now[0] = 9
        self.assertTrue(guard.accept('group:1', 'a', '查卡 白龙'))

    def test_generic_network_or_api_errors_are_not_claimed_as_account_risk(self):
        self.assertEqual(restriction_hint({'retcode': 1, 'message': 'network timeout'}), '')
        self.assertEqual(restriction_hint({'retcode': 1, 'message': '用户不存在'}), '')
        self.assertTrue(restriction_hint({'retcode': 1, 'message': 'captcha required'}))

    def test_main_runtime_lock_prevents_second_instance(self):
        import bot.main as main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'ROOT', Path(tmp)), patch.object(main, 'run_main') as run:
            with startup_lock(Path(tmp), 'bot-runtime.lock'), patch('builtins.print'):
                main.main()
            run.assert_not_called()

    def test_existing_project_napcat_is_reused_without_launching_another(self):
        from bot import napcat_start
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / 'NapCat.Test' / 'NapCat.Test.Shell' / 'NapCatWinBootMain.exe'
            exe.parent.mkdir(parents=True)
            exe.touch()
            with patch.object(napcat_start, 'ROOT', root), patch.object(napcat_start, 'existing_napcat', return_value=[{'ProcessId': 1}]), \
                 patch('sys.argv', ['start']), patch('subprocess.Popen') as launch, patch('builtins.print'):
                self.assertEqual(napcat_start.main(), 0)
            launch.assert_not_called()

    def test_replay_notifications_coalesce_per_user_and_player(self):
        _rank_cache['甲'], _rank_cache['对手'] = 1, 2
        with temporary_service() as service:
            for user in ['10001', '10002']:
                service.handle_text(user, '订阅 甲 全部 推送')
            writer = ReplayWriter(service.replay_root, ['甲'])
            writer.on_saved = service.on_replay_saved
            writer.save(b'replay-one', ['甲', '对手'])
            writer.save(b'replay-two', ['甲', '对手'])
            service.qq = FakeQQ()
            self.assertEqual(asyncio.run(service.deliver()), 4)
            self.assertEqual(len(service.qq.sent), 2)
            self.assertEqual({r[0] for r in service.qq.sent}, {'10001', '10002'})
            self.assertEqual(service.store.pending_notifications(), [])
