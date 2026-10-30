import asyncio
import json
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from bot.launcher import bot_state, launch_services, spawn_batch, startup_lock
from test_bot import temporary_service


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, states, *, running=False, probe_results=None, login_timeout=10, bot_timeout=10):
        now, spawned = [0], []
        state_values = iter(states)
        last_state = [states[-1]]
        responses = iter(probe_results or [(True, 'online')])
        last_response = [(True, 'online')]

        def state(_config):
            last_state[0] = next(state_values, last_state[0])
            return last_state[0]

        async def probe(_config, timeout):
            last_response[0] = next(responses, last_response[0])
            return last_response[0]

        async def pause(seconds):
            now[0] += seconds

        with patch('builtins.print'):
            ok = await launch_services(Path('D:/test'), {}, spawn=lambda path: spawned.append(path.name),
                probe=probe, state=state, port_open=lambda _: running,
                pause=pause, clock=lambda: now[0], login_timeout=login_timeout, bot_timeout=bot_timeout)
        return ok, spawned

    async def test_waits_for_login_then_starts_bot(self):
        ok, spawned = await self.scenario(['stopped', 'stopped', 'stopped', 'ready'],
                                         probe_results=[(False, 'waiting'), (True, 'online')])
        self.assertTrue(ok)
        self.assertEqual(spawned, ['start-napcat.bat', 'start.bat'])

    async def test_reuses_both_running_services(self):
        ok, spawned = await self.scenario(['ready', 'ready'], running=True)
        self.assertTrue(ok)
        self.assertEqual(spawned, [])

    async def test_only_starts_missing_bot(self):
        ok, spawned = await self.scenario(['stopped', 'stopped', 'ready'], running=True)
        self.assertTrue(ok)
        self.assertEqual(spawned, ['start.bat'])

    async def test_busy_port_and_bad_token_do_not_spawn_duplicate_services(self):
        self.assertEqual(await self.scenario(['busy'], running=True), (False, []))
        self.assertEqual(await self.scenario(['stopped'], running=True,
            probe_results=[(False, 'Token 验证失败')]), (False, []))

    async def test_login_timeout_never_starts_bot(self):
        ok, spawned = await self.scenario(['stopped'], login_timeout=3,
            probe_results=[(False, 'not logged in')])
        self.assertFalse(ok)
        self.assertEqual(spawned, ['start-napcat.bat'])

    async def test_bot_timeout_reports_failure(self):
        ok, spawned = await self.scenario(['stopped'], running=True, bot_timeout=3)
        self.assertFalse(ok)
        self.assertEqual(spawned, ['start.bat'])


class LauncherIntegrationTests(unittest.TestCase):
    def test_health_recognizes_bot_without_exposing_configuration(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 保密玩家 全部 静默')
            service.share_server.start()
            config = {'share': {'bind_host': '127.0.0.1', 'bind_port': service.share.bind_port}}
            self.assertEqual(bot_state(config), 'ready')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{service.share.bind_port}/health', timeout=2) as response:
                self.assertEqual(json.loads(response.read()), {'service': 'qq-replay-bot', 'status': 'running'})

    def test_repeated_click_lock_is_released_when_startup_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with startup_lock(root) as first:
                self.assertTrue(first)
                with startup_lock(root) as second:
                    self.assertFalse(second)
            with startup_lock(root) as third:
                self.assertTrue(third)

    def test_spawn_handles_spaces_chinese_and_other_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / '启动 folder & test'
            directory.mkdir()
            batch = directory / '启动.bat'
            batch.write_bytes(b'@echo off\r\necho ready>"%~dp0marker.txt"\r\n')
            real_popen = subprocess.Popen

            def hidden_test_process(command, cwd, creationflags):
                process = real_popen(command, cwd=cwd, creationflags=subprocess.CREATE_NO_WINDOW,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, repr((stdout, stderr)))
                return process

            with patch('bot.launcher.subprocess.Popen', side_effect=hidden_test_process):
                spawn_batch(batch)
            self.assertEqual((directory / 'marker.txt').read_text().strip(), 'ready')


if __name__ == '__main__':
    unittest.main()
