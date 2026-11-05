import json
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from bot.watch_api import WatchApiServer
from bot.watch_jobs import RemoteJobs, Job
from bot.watch_tokens import WatchTokenStore
from bot.watch_task_state import AuthorizationExpired
from bot.watch_api import _Handler
from test_watch_device_tokens import _post


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = WatchTokenStore(self.root/'bot.sqlite3', migrate=True)
        self.qq = WatchTokenStore(self.root/'bot.sqlite3')  # independent process-style connection
        self.api = WatchApiServer(RemoteJobs(),self.root/'replays','legacy',log=lambda *a:None,bind=('127.0.0.1',0),token_store=self.store)
        self.life = self.api.lifecycle

    def tearDown(self):
        self.api.stop();self.qq.close();self.store.close();self.tmp.cleanup()

    def auth(self, worker='phone'):
        issued = self.qq.issue(worker,'owner')
        row = self.store.verify(issued['token'])
        return dict(row,generation='device:'+str(row['id'])),issued['token']

    def claim(self, auth, ip='one'):
        job=Job({'room':{'key':'room-'+str(time.time_ns())}},180)
        self.api.jobs._jobs[job.id]=job;self.api.jobs._order.append(job.id)
        return self.life.claim(auth['worker'],2,ip,0,auth)['id']

    def test_reissue_inflight_releases_without_new_client_and_other_slot_survives(self):
        old,_=self.auth();other,_=self.auth('phone-2')
        job=self.claim(old);other_job=self.claim(other)
        new,_=self.auth()
        self.life.reconcile()
        snap=self.api.jobs.snapshot()
        self.assertEqual(snap['workers']['phone']['active'],0)
        self.assertEqual(snap['workers']['phone-2']['active'],1)
        self.assertEqual(snap['claimed'],1)
        self.assertEqual(self.life.owners.get(job)['state'],'token_invalid')
        self.assertEqual(self.life.owners.get(other_job)['state'],'active')
        with self.assertRaises(AuthorizationExpired):self.life.heartbeat('phone',[job],'one',old)
        self.assertEqual(self.life.heartbeat('phone',[job],'one',new),[job])

    def test_idle_reissue_and_empty_heartbeat_are_idempotent(self):
        old,_=self.auth();self.life.heartbeat('phone',[],'one',old)
        new,_=self.auth();self.life.heartbeat('phone',[],'one',new)
        for _ in range(3):self.life.reconcile();self.life.heartbeat('phone',[],'one',new)
        self.assertEqual(self.api.jobs.snapshot()['workers']['phone']['active'],0)

    def test_lease_expiry_clears_capacity_reported_set_and_returns_one_retry(self):
        auth,_=self.auth();job=self.claim(auth)
        self.life.heartbeat('phone',[job],'one',auth)
        self.api.jobs._jobs[job].lease_until=time.time()-1
        self.life.reconcile();self.life.reconcile()
        snap=self.api.jobs.snapshot()
        self.assertEqual((snap['claimed'],snap['workers']['phone']['active']),(0,0))
        retries=[r for r in self.api.jobs.drain_reports()if r.get('retry')]
        self.assertEqual(len(retries),1)
        self.assertEqual(retries[0]['outcome'],'lease_expired')

    def test_report_repeated_does_not_double_release_or_touch_other_device(self):
        auth,_=self.auth();other,_=self.auth('other')
        job=self.claim(auth);self.claim(other)
        self.assertTrue(self.life.report('phone',job,'saved',{},auth))
        self.assertTrue(self.life.report('phone',job,'saved',{},auth))
        self.assertEqual(self.api.jobs.snapshot()['workers']['other']['active'],1)
        self.assertEqual(self.api.jobs.stats['reported'],1)

    def test_restart_recovers_generation_owned_recording_and_buffered_upload(self):
        auth,_=self.auth();job=self.claim(auth)
        restarted=WatchApiServer(RemoteJobs(),self.root/'replays','legacy',log=lambda *a:None,token_store=self.store)
        self.addCleanup(restarted.stop)
        self.assertEqual(restarted.lifecycle.heartbeat('phone',[job],'one',auth),[])
        snap=restarted.jobs.snapshot()
        self.assertEqual((snap['claimed'],snap['workers']['phone']['active']),(0,1))
        with restarted.lifecycle.upload_authorized(auth,job):
            result=restarted.save_upload(job,'record.yrp',b'YRP2data',['A','B'],time.time())
        self.assertTrue(result)
        self.assertTrue(restarted.lifecycle.report('phone',job,'saved',{},auth))
        self.assertEqual(restarted.jobs.snapshot()['workers']['phone']['active'],0)

    def test_new_generation_cannot_inherit_ledger_task_even_after_restart(self):
        old,_=self.auth();job=self.claim(old);new,_=self.auth()
        restarted=WatchApiServer(RemoteJobs(),self.root/'replays','legacy',log=lambda *a:None,token_store=self.store)
        self.addCleanup(restarted.stop)
        self.assertEqual(restarted.lifecycle.heartbeat('phone',[job],'one',new),[job])
        with self.assertRaises(PermissionError):
            with restarted.lifecycle.upload_authorized(new,job):pass

    def test_long_poll_rechecks_authority_before_actual_claim(self):
        old,_=self.auth();started=threading.Event();errors=[]
        def request():
            started.set()
            try:self.life.claim('phone',1,'one',2,old)
            except Exception as e:errors.append(type(e))
        thread=threading.Thread(target=request);thread.start();started.wait(1)
        time.sleep(.05);new,_=self.auth();thread.join(3)
        self.assertEqual(errors,[AuthorizationExpired])
        self.assertEqual(self.api.jobs.snapshot()['claimed'],0)
        self.claim(new)

    def test_reissue_serializes_with_upload_commit_without_fact_db_writer(self):
        old,_=self.auth();job=self.claim(old);issued=threading.Event();begin=threading.Event()
        def issue():
            begin.set();self.qq.issue('phone','owner');issued.set()
        with self.life.upload_authorized(old,job):
            thread=threading.Thread(target=issue);thread.start();begin.wait(1)
            self.assertFalse(issued.wait(.05))
            self.assertEqual(self.store._db.in_transaction,False)
        thread.join(2);self.assertTrue(issued.is_set())
        with self.assertRaises(AuthorizationExpired):
            with self.life.upload_authorized(old,job):pass

    def test_offline_same_ip_does_not_consume_other_device_slots(self):
        auth,_=self.auth();other,_=self.auth('other')
        self.claim(auth);self.api.jobs._workers['phone']['seen']=time.time()-60
        self.life.heartbeat('other',[],'one',other)
        self.assertEqual(self.api.jobs._active_for_ip('one'),0)
        self.assertGreater(self.api.jobs.free_slots(),0)

    def test_revocation_after_http_auth_before_body_commit_is_401(self):
        auth,token=self.auth();job=self.claim(auth)
        self.qq.revoke_worker('phone')
        self.api.start();port=self.api._httpd.server_address[1]
        status,_=_post(port,'/watch-api/heartbeat',{'worker':'phone','active':[job]},token=token)
        self.assertEqual(status,401)

    def test_legacy_upgrade_window_and_restart_upload_no_task_board_entry(self):
        auth=dict(id=None,worker='',created_at=0,generation=self.life.legacy_generation)
        self.assertEqual(self.life.heartbeat('pc',['before-upgrade'],'one',auth),[])
        self.assertEqual(self.api.jobs.snapshot()['workers']['pc']['active'],1)
        self.life.boot_at=time.time()-self.api.jobs.lease-1
        self.assertEqual(self.life.heartbeat('pc',['unproven-other'],'one',auth),['unproven-other'])

    def test_empty_heartbeat_releases_inflight_and_cannot_resurrect_cancelled(self):
        auth,_=self.auth();job=self.claim(auth)
        self.life.heartbeat('phone',[job],'one',auth)
        self.life.heartbeat('phone',[],'one',auth)
        self.assertEqual(self.api.jobs.snapshot()['workers']['phone']['active'],0)
        self.assertEqual(self.life.heartbeat('phone',[job],'one',auth),[job])

    def test_device_unknown_job_not_adopted_even_token_predates_restart(self):
        auth,_=self.auth()
        self.life.boot_at=time.time()+1
        self.assertEqual(self.life.heartbeat('phone',['unproven'],'one',auth),['unproven'])
        self.assertIsNone(self.life.owners.get('unproven'))

    def test_http_authentication_then_reissue_before_heartbeat_commit(self):
        auth,token=self.auth();job=self.claim(auth)
        self.api.start();entered=threading.Event();resume=threading.Event();result=[]
        original=_Handler._json
        def paused(handler):
            entered.set();resume.wait(2);return original(handler)
        with patch.object(_Handler,'_json',paused):
            request=threading.Thread(target=lambda:result.append(_post(self.api._httpd.server_address[1],'/watch-api/heartbeat',{'worker':'phone','active':[job]},token=token)[0]))
            request.start();self.assertTrue(entered.wait(1));self.auth();resume.set();request.join(3)
        self.assertEqual(result,[401])

    def test_http_authentication_then_reissue_before_upload_commit(self):
        auth,token=self.auth();job=self.claim(auth)
        self.api.start();entered=threading.Event();resume=threading.Event();result=[]
        original=_Handler._upload
        def paused(handler,query):
            entered.set();resume.wait(2);return original(handler,query)
        with patch.object(_Handler,'_upload',paused):
            request=threading.Thread(target=lambda:result.append(_post(self.api._httpd.server_address[1],'/watch-api/upload?job='+job+'&name=old.yrp',token=token,raw=b'YRP2buffer')[0]))
            request.start();self.assertTrue(entered.wait(1));self.auth();resume.set();request.join(3)
        self.assertEqual(result,[401]);self.assertEqual(list((self.root/'replays').glob('**/*.yrp')),[])

    def test_legacy_heartbeat_without_active_renews_persistent_lease(self):
        auth,_=self.auth();job=self.claim(auth)
        before=self.life.owners.get(job)['expires']
        self.life.heartbeat('phone',None,'one',auth)
        self.assertGreaterEqual(self.life.owners.get(job)['expires'],before)

    def test_background_reconciler_runs_without_new_client(self):
        auth,_=self.auth();self.claim(auth);self.api.start();self.qq.revoke_worker('phone')
        deadline=time.monotonic()+6
        while time.monotonic()<deadline and self.api.jobs.snapshot()['claimed']:
            time.sleep(.05)
        self.assertEqual(self.api.jobs.snapshot()['claimed'],0)

    def test_separate_qq_process_cannot_reissue_inside_upload_commit(self):
        auth,_=self.auth();job=self.claim(auth);done=self.root/'issued.txt'
        code=('from bot.watch_tokens import WatchTokenStore;from pathlib import Path;'
              's=WatchTokenStore('+repr(str(self.root/'bot.sqlite3'))+');'
              "s.issue('phone','owner');s.close();Path("+repr(str(done))+ ").write_text('done')")
        with self.life.upload_authorized(auth,job):
            child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            time.sleep(.1);self.assertFalse(done.exists())
        _,error=child.communicate(timeout=4)
        self.assertEqual(child.returncode,0,error.decode(errors='replace'));self.assertTrue(done.exists())

    def test_expired_generation_releases_without_reconnect_other_slot_keeps_capacity(self):
        old=self.qq.issue('phone','owner',hours=1)
        old_auth=dict(self.store.verify(old['token']),generation='device:'+str(self.store.verify(old['token'])['id']))
        other=self.qq.issue('phone-2','owner',hours=2)
        other_auth=dict(self.store.verify(other['token']),generation='device:'+str(self.store.verify(other['token'])['id']))
        self.claim(old_auth);self.claim(other_auth)
        self.store.clock=lambda:time.time()+3601
        self.life.reconcile()
        snapshot=self.api.jobs.snapshot()
        self.assertEqual(snapshot['workers']['phone']['active'],0)
        self.assertEqual(snapshot['workers']['phone-2']['active'],1)


if __name__=='__main__':unittest.main()
