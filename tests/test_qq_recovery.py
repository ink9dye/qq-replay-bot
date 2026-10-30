import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from bot.qq_recovery import RecoveryPolicy
from bot.qq import OneBotClient


class PolicyTests(unittest.TestCase):
    def test_retry_limit_persists_and_only_stability_clears_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, notices = [1000], []
            policy = RecoveryPolicy(tmp, clock=lambda:clock[0], notify=notices.append)
            delays = []
            for _ in range(6):
                delays.append(policy.failed())
                clock[0] += delays[-1]
            self.assertEqual(delays, [1,2,4,8,16,900])
            self.assertEqual(notices, ['offline'])
            restored = RecoveryPolicy(tmp, clock=lambda:clock[0]-900, notify=notices.append)
            self.assertEqual(restored.remaining(), 900)
            restored.announce('offline')
            self.assertEqual(notices, ['offline'])
            restored.announce('attention')
            restored.announce('attention')
            restored.stable()
            restored.stable()
            self.assertEqual(notices, ['offline','attention','online'])
            self.assertEqual(restored.failures, 0)

    def test_corrupt_state_and_failed_persistence_stop_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'qq-recovery.json';path.write_text('{broken')
            self.assertTrue(RecoveryPolicy(tmp).invalid)
            path.unlink()
            policy=RecoveryPolicy(tmp,notify=lambda _:None)
            with patch('bot.qq_recovery.write_state',side_effect=OSError()):
                policy.failed()
            self.assertTrue(policy.invalid)


class Transport:
    def __init__(self, online=True):
        self.online=online;self.actions=[];self.response=None
        self.events=[];self.client=None
    async def connect(self):pass
    async def close(self):pass
    async def send_text(self,text):
        request=json.loads(text);self.actions.append(request['action'])
        self.response=json.dumps(dict(echo=request['echo'],retcode=0,data=(
            {'user_id':'10001'} if request['action']=='get_login_info' else {'online':self.online})))
    async def recv_text(self):
        if self.response:
            reply,self.response=self.response,None;return reply
        if self.events:return json.dumps(self.events.pop(0))
        self.client.stop();return None


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_cooldown_cannot_be_bypassed_by_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'qq-recovery.json').write_text('{invalid')
            (root/'qq-resume.request').write_text('{}')
            transport=Transport();transport.connect=AsyncMock()
            async def pause(_):client.stop()
            client=OneBotClient('ws://local','secret',None,transport=transport,state_dir=tmp,pause=pause)
            client.recovery.notify=lambda _:None
            await client.run()
            transport.connect.assert_not_called()
            self.assertTrue((root/'qq-resume.request').exists())

    async def test_port_online_account_offline_never_dispatches_or_logs_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport=Transport(False);events=[];notices=[]
            async def pause(_):client.stop()
            client=OneBotClient('ws://local','secret',events.append,transport=transport,
                state_dir=tmp,expected_self_id='10001',pause=pause)
            client.recovery.notify=notices.append;transport.client=client
            await client.run()
            self.assertEqual(transport.actions,['get_login_info','get_status'])
            self.assertEqual(events,[])
            self.assertEqual(notices,['attention'])
            self.assertEqual(client.recovery.failures,1)

    async def test_online_offline_online_heartbeat_restores_without_relogin(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport=Transport();events=[];notices=[]
            client=OneBotClient('ws://local','secret',events.append,transport=transport,
                state_dir=tmp,expected_self_id='10001')
            client.recovery.notify=notices.append;transport.client=client
            transport.events=[{'meta_event_type':'heartbeat','self_id':10001,'status':{'online':False}},
                {'post_type':'message','message_type':'private','self_id':10001,'user_id':10002,'message':'ignore'},
                {'meta_event_type':'heartbeat','self_id':10001,'status':{'online':True}},
                {'meta_event_type':'heartbeat','self_id':10001,'status':{'online':True}}]
            ticks=iter([0,0,1,122])
            client._connection_clock=lambda:next(ticks)
            await client.run()
            self.assertEqual(transport.actions,['get_login_info','get_status'])
            self.assertEqual(notices,['attention','online'])
            self.assertEqual(events,[])

    async def test_restart_during_cooldown_never_connects_or_probes_webui(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy=RecoveryPolicy(tmp,notify=lambda _:None)
            for _ in range(6):policy.failed()
            transport=Transport();transport.connect=AsyncMock();probe=AsyncMock()
            async def pause(_):client.stop()
            client=OneBotClient('ws://local','secret',None,transport=transport,
                state_dir=tmp,expected_self_id='10001',account_probe=probe,pause=pause)
            await client.run()
            transport.connect.assert_not_called();probe.assert_not_called()

    async def test_auth_error_suspends_instead_of_retry_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport=Transport();transport.connect=AsyncMock(side_effect=RuntimeError('401 secret'))
            async def pause(_):client.stop()
            client=OneBotClient('ws://local','secret',None,transport=transport,state_dir=tmp,pause=pause)
            client.recovery.notify=lambda _:None
            await client.run()
            self.assertTrue(client.suspended)
            self.assertEqual(transport.connect.await_count,1)
            self.assertNotIn('secret',(Path(tmp)/'qq-status.json').read_text(encoding='utf8'))
