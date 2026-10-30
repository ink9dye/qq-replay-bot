import json
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bot.napcat_observer import Journal, Observer, ProcessHandle, markers, relay_process


class ObserverTests(unittest.TestCase):
    def test_launch_relay_preserves_console_but_only_stores_categories_and_exit(self):
        from types import SimpleNamespace
        raw='[error] 快速登录错误：用户身份已失效 secret-token 123456789\n二维码 secret-qr\n'.encode()
        child=SimpleNamespace(pid=123,stdout=io.BytesIO(raw),wait=lambda:7)
        with tempfile.TemporaryDirectory() as tmp:
            log=Journal(tmp,filename='napcat-launcher.jsonl');out=io.BytesIO()
            self.assertEqual(relay_process(child,log,out),7)
            self.assertEqual(out.getvalue(),raw)
            saved=log.path.read_text()
            self.assertNotIn('secret',saved);self.assertNotIn('123456789',saved)
            self.assertIn('identity_expired_text',saved);self.assertIn('launcher_child_exit',saved)

    def test_logging_failure_does_not_block_console_or_wait(self):
        from types import SimpleNamespace
        child=SimpleNamespace(pid=123,stdout=io.BytesIO(b'[error] [KickedOffLine]\n'),wait=lambda:0)
        with patch.object(Journal,'emit',side_effect=OSError):
            self.assertEqual(relay_process(child,Journal('.'),io.BytesIO()),0)

    def test_sensitive_console_text_never_leaves_classifier(self):
        text='[error] [KickedOffLine] 其他设备 123456789 secret-token https://secret/qr'
        result=markers(text)
        self.assertEqual(result[0][1:],('kicked_offline_marker','other_device_text'))
        self.assertNotIn('123456789',str(result));self.assertNotIn('secret',str(result))

    def test_message_with_fake_marker_is_ignored(self):
        self.assertEqual(markers('[info] 接收 <- 群 [KickedOffLine]'),[])

    def test_quick_login_and_expired_identity_are_separate_from_exit(self):
        self.assertEqual(markers('[error] 快速登录错误：用户身份已失效')[0][1:],
                         ('quick_login_failed_marker','identity_expired_text'))
        self.assertEqual(markers('unstructured 用户身份已失效'),[])

    def test_journal_whitelist_and_bounded_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=Journal(tmp,limit=250)
            for _ in range(10):log.emit('heartbeat',target_pid=12,reason='safe',body='secret',token='secret')
            paths=list(Path(tmp).glob('*.jsonl*'))
            self.assertLessEqual(len(paths),3)
            text=''.join(p.read_text() for p in paths)
            self.assertNotIn('secret',text)
            self.assertIn('target_pid',text)

    def test_observation_captures_exit_without_assuming_login_loss(self):
        with tempfile.TemporaryDirectory() as tmp,patch('bot.napcat_observer.ProcessHandle') as handles,patch('bot.napcat_observer.console_text',return_value=None):
            handles.return_value.exit_code.return_value=7
            log=Journal(tmp);watch=Observer(log)
            watch.tick({'processes':[{'ProcessId':10,'ParentProcessId':1,'Name':'QQ.exe'}],'port_open':True},{'state':'online'})
            watch.tick({'processes':[],'port_open':False},{'state':'cooldown'})
            rows=[json.loads(line) for line in log.path.read_text().splitlines()]
            exit_row=next(r for r in rows if r['event']=='process_exit_observed')
            self.assertEqual(exit_row['exit_code'],7)
            self.assertFalse(any(r.get('kind')=='kicked_offline_marker' for r in rows))

    def test_process_exists_port_down_not_reported_as_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp,patch('bot.napcat_observer.ProcessHandle'),patch('bot.napcat_observer.console_text',return_value=None):
            log=Journal(tmp);watch=Observer(log)
            state={'processes':[{'ProcessId':10,'ParentProcessId':1,'Name':'QQ.exe'}],'port_open':True}
            watch.tick(state,{});state['port_open']=False;watch.tick(state,{})
            self.assertNotIn('process_exit_observed',log.path.read_text())

    @unittest.skipUnless(sys.platform=='win32','Windows process handles')
    def test_real_exit_status_retained_after_process_finishes(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.2);raise SystemExit(7)'])
        handle=ProcessHandle(child.pid)
        try:
            child.wait(timeout=5)
            self.assertEqual(handle.exit_code(),7)
        finally:handle.close()
