import asyncio
import json
import subprocess
import tempfile
import unittest
import urllib.request
import io
import os
import sys
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.launcher import bot_state, launch_services, spawn_batch, startup_lock, ensure_statistics_web
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

    async def test_paused_existing_bot_returns_incomplete_without_qq_actions(self):
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.print'):
            root=Path(tmp);(root/'data').mkdir();(root/'data/qq-paused.json').write_text('{}')
            spawned=[]
            async def forbidden(*args,**kwargs):raise AssertionError('QQ must stay paused')
            ok=await launch_services(root,{},spawn=spawned.append,probe=forbidden,state=lambda _:'ready')
            self.assertFalse(ok)
            self.assertEqual(spawned,[])
            self.assertTrue((root/'data/qq-paused.json').is_file())

    async def test_explicit_attempt_opens_napcat_once_and_resumes_only_after_verified(self):
        for online in (True,False):
            with self.subTest(online=online), tempfile.TemporaryDirectory() as tmp,patch('builtins.print'),patch('bot.launcher.existing_napcat',return_value=[]),patch('bot.launcher.webui_login_id',side_effect=AssertionError('No WebUI authentication')):
                root=Path(tmp);(root/'data').mkdir();marker=root/'data/qq-paused.json';marker.write_text('{}')
                calls=[];now=[0]
                async def probe(*args,**kwargs):return online,'online' if online else 'not online'
                async def pause(seconds):now[0]+=seconds
                ok=await launch_services(root,{},attempt=True,spawn=lambda *args:calls.append(args),probe=probe,
                    state=lambda _:'ready',port_open=lambda _:False,pause=pause,clock=lambda:now[0],login_timeout=3)
                self.assertEqual(ok,online)
                self.assertEqual(calls,[(root/'start-napcat.bat','--attempt')])
                self.assertEqual((root/'data/qq-resume.request').exists(),online)
                self.assertTrue(marker.is_file())

    async def test_explicit_attempt_requests_resume_without_breaking_protection(self):
        for state_value, expect_resume in (('cooldown', True), ('online', False)):
            with self.subTest(state=state_value), tempfile.TemporaryDirectory() as tmp, patch('builtins.print'):
                root = Path(tmp); (root / 'data').mkdir()
                (root / 'data/qq-status.json').write_text(json.dumps(
                    {'state': state_value, 'expected_self_id': '10001'}), encoding='utf8')
                (root / 'data/qq-recovery.json').write_text(json.dumps(
                    {'state': 'recovery', 'failures': 6, 'next_attempt_at': 9e12,
                     'alert': 'offline', 'offline_since': 1}), encoding='utf8')
                spawned = []
                ok = await launch_services(root, {'qq': {'expected_self_id': '10001'}}, attempt=True,
                    spawn=spawned.append, probe=AsyncMock(return_value=(True, 'online')),
                    state=lambda _: 'ready', port_open=lambda _: True, pause=AsyncMock())
                self.assertTrue(ok)
                self.assertEqual(spawned, [])  # 已在运行的服务不重启、不重开 NapCat
                self.assertEqual((root / 'data/qq-resume.request').exists(), expect_resume)
                # 恢复请求只清重试预算；故障状态与提醒去重保留，重启后不能自动绕过。
                recovery = json.loads((root / 'data/qq-recovery.json').read_text(encoding='utf8'))
                self.assertEqual(recovery['alert'], 'offline')
                self.assertEqual(recovery['failures'], 6)
                self.assertEqual(recovery['next_attempt_at'], 9e12)

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
    def test_napcat_explicit_attempt_can_open_while_old_pause_is_preserved(self):
        from bot import napcat_start
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);binary=root/'NapCat.Test/NapCat.Test.Shell/NapCatWinBootMain.exe';binary.parent.mkdir(parents=True);binary.touch()
            (root/'data').mkdir();marker=root/'data/qq-paused.json';marker.write_text('{}')
            for attempt in (False,True):
                with self.subTest(attempt=attempt),patch.object(napcat_start,'ROOT',root),patch.object(napcat_start,'existing_napcat',return_value=[]),patch('bot.setup.read_config',return_value={'qq':{'expected_self_id':'10001'}}),patch.object(sys,'argv',['napcat_start']+(['--attempt'] if attempt else [])),patch('builtins.print'),patch.object(napcat_start.subprocess,'Popen',return_value=SimpleNamespace(wait=lambda:0)) as spawn:
                    self.assertEqual(napcat_start.main(),0 if attempt else 2)
                    self.assertEqual(spawn.call_count,1 if attempt else 0)
                    self.assertTrue(marker.exists())

    def test_double_click_batch_keeps_success_and_failure_results_visible(self):
        source=Path(__file__).resolve().parents[1]/'start-all.bat'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'start-all.bat').write_bytes(source.read_bytes())
            (root/'find-python.bat').write_bytes(('@echo off\r\nset "BOT_PYTHON='+sys.executable+'"\r\nexit /b 0\r\n').encode())
            (root/'bot').mkdir();(root/'bot/__init__.py').write_text('')
            (root/'bot/launcher.py').write_text("import os;print('ISOLATED_STARTUP_RESULT',flush=True);raise SystemExit(int(os.environ['STARTUP_TEST_EXIT']))")
            for code in (0,1):
                with self.subTest(code=code):
                    env=dict(os.environ,STARTUP_TEST_EXIT=str(code));env.pop('PYTHONPATH',None)
                    process=subprocess.Popen(['cmd.exe','/d','/c','start-all.bat'],cwd=root,env=env,
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        time.sleep(.4)
                        waiting=process.poll() is None
                        out,err=process.communicate(b'\r\n',timeout=5)
                    finally:
                        if process.poll() is None:process.kill();process.communicate()
                    self.assertTrue(waiting,'Batch exited before user could read the result')
                    self.assertEqual(process.returncode,code,repr(err))
                    self.assertIn(b'ISOLATED_STARTUP_RESULT',out)

    def test_statistics_web_reuses_ready_and_refuses_foreign_listener(self):
        for service,expected in [('qq-statistics-web',True),('foreign',False)]:
            opener=SimpleNamespace(open=lambda *a,**k:io.BytesIO(json.dumps(dict(service=service,status='running')).encode()))
            with patch('bot.launcher.urllib.request.build_opener',return_value=opener),patch('bot.launcher.subprocess.Popen') as spawn:
                self.assertEqual(ensure_statistics_web(Path('D:/test'),popen=spawn),expected)
                spawn.assert_not_called()

    def test_statistics_missing_starts_once_hidden_without_watch_or_qq(self):
        calls=[]
        replies=iter([None,dict(service='qq-statistics-web',status='running')])
        def read(*args,**kwargs):
            result=next(replies)
            if result is None:raise OSError('not listening')
            return io.BytesIO(json.dumps(result).encode())
        with patch('bot.launcher.urllib.request.build_opener',return_value=SimpleNamespace(open=read)),patch('bot.launcher.socket.create_connection',side_effect=OSError()):
            self.assertTrue(ensure_statistics_web(Path('D:/test'),popen=lambda *a,**k:calls.append((a,k)),pause=lambda _:None))
        self.assertEqual(len(calls),1)
        self.assertIn('bot.statistics_server',calls[0][0][0])
        self.assertEqual(calls[0][1]['creationflags'],subprocess.CREATE_NO_WINDOW)

    def test_health_recognizes_bot_without_exposing_configuration(self):
        with temporary_service() as service:
            service.handle_text('10001', '订阅 保密玩家 全部 静默')
            service.share_server.start()
            config = {'share': {'bind_host': '127.0.0.1', 'bind_port': service.share.bind_port}}
            self.assertEqual(bot_state(config), 'ready')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{service.share.bind_port}/health', timeout=2) as response:
                self.assertEqual(json.loads(response.read()), {'service': 'qq-replay-bot', 'status': 'running', 'retention_days': 14})

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
