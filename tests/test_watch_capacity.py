import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('watch_capacity',Path(__file__).resolve().parents[1]/'tools/watch_capacity.py')
C=importlib.util.module_from_spec(SPEC);sys.modules[SPEC.name]=C;SPEC.loader.exec_module(C)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.token='bench_test-only-capacity-secret'
        self.state=C.ReceiverState(self.token,max_qps=200,max_bytes=8*C.MIB,claim_bytes=128)
        self.server=C.BenchServer(('127.0.0.1',0),self.state,16)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.finished.set();self.server.shutdown();self.server.server_close();self.thread.join(2)

    def client(self,token=None):
        stop=threading.Event()
        return C.BenchClient(self.url,token or self.token,C.ByteBudget(8*C.MIB,0,stop),C.Results(),stop)

    def test_real_http_flow_discards_uploads_and_reports_one_real_ip(self):
        c=self.client();self.addCleanup(c.close)
        hello=c.call('hello');self.assertEqual(hello['protocol'],C.PROTOCOL)
        job=c.call('claim',{'worker':'synthetic','capacity':1})['job']['id']
        self.assertTrue(c.call('heartbeat',{'worker':'synthetic','active':[job]})['ok'])
        self.assertTrue(c.call('upload',body=b'x'*1024)['ok'])
        self.assertTrue(c.call('report',{'worker':'synthetic','job':job})['ok'])
        snapshot=c.call('status')
        self.assertEqual(snapshot['distinct_source_ips'],1)
        self.assertFalse(snapshot['uploads_saved']);self.assertFalse(snapshot['real_watch_api_exercised'])

    def test_wrong_key_fails_without_exposing_key(self):
        c=self.client('wrong-test-key');self.addCleanup(c.close)
        with self.assertRaisesRegex(RuntimeError,'benchmark_http_401'):c.call('hello')
        self.assertNotIn(self.token,json.dumps(self.state.snapshot()))

    def test_production_paths_urls_and_credentials_rejected(self):
        for url in ('http://127.0.0.1/watch-api','http://127.0.0.1/?token=x','http://user:pass@127.0.0.1',
                    'http://192.0.2.1:8896','file:///tmp/test'):
            with self.assertRaises(ValueError):C.checked_url(url)
        self.assertEqual(C.checked_url('http://192.0.2.1:8896',True).port,8896)

    def test_rate_limiter_cannot_hang_beyond_stage_deadline(self):
        stop=threading.Event();budget=C.ByteBudget(1024,1e-6,stop,deadline=time.monotonic()+.1)
        budget.reserve(500)
        with self.assertRaisesRegex(RuntimeError,'rate_limited_stage_deadline'):budget.reserve(500)
        self.assertTrue(stop.is_set())

    def test_byte_budget_and_receiver_qps_stop_growth(self):
        stop=threading.Event();budget=C.ByteBudget(100,0,stop)
        budget.reserve(80)
        with self.assertRaises(RuntimeError):budget.reserve(21)
        state=C.ReceiverState(self.token,max_bytes=100,max_qps=1)
        self.assertTrue(state.admit());self.assertFalse(state.admit())
        self.assertFalse(state.account(incoming=101));self.assertEqual(state.stopped,'receiver_byte_budget')

    def test_resource_guard_uses_low_memory_disk_and_sustained_cpu(self):
        base=dict(available_mib=500,process_rss_mib=20,free_disk_mib=3000,system_cpu_ticks=100,system_idle_ticks=0)
        self.state.sample=dict(base)
        for n in range(1,4):
            with patch.object(C,'resource_sample',return_value=dict(base,system_cpu_ticks=100+n*100)):
                self.state.monitor()
        self.assertEqual(self.state.stopped,'system_cpu_pressure')
        state=C.ReceiverState(self.token)
        with patch.object(C,'resource_sample',return_value=dict(base,available_mib=100)):
            state.monitor()
        self.assertEqual(state.stopped,'system_memory_pressure')

    def test_estimator_separates_directions_and_includes_burst(self):
        report=C.estimate(C.Profile(rooms_per_ip=1),10,3)
        self.assertEqual(report['bandwidth_only_model_limit_ips'],min(report['limits_ips'].values()))
        self.assertFalse(report['actual_public_ip_limit_verified'])
        self.assertEqual(C.estimate(C.Profile(),10,3,baseline_in=10)['bandwidth_only_model_limit_ips'],0)

    def test_multihost_merge_deduplicates_nat_and_rejects_sequential_stages(self):
        def row(source,start=0,end=10):
            return dict(server_id='receiver',profile={'rooms':6},started_at=start,ended_at=end,
                        observed_source_id=source,observed_source_public=True,modeled_ip_groups=4,stage_passed=True,client_rate_limiter_engaged=False)
        result=C.merge_reports([row('a'),row('a'),row('b')])
        self.assertEqual(result['observed_distinct_public_ips'],2)
        self.assertEqual(result['offered_modeled_ip_groups'],12)
        with self.assertRaises(ValueError):C.merge_reports([row('a'),row('b',11,20)])
        with self.assertRaises(ValueError):C.merge_reports([row('a'),dict(row('b'),server_id='other')])

    def test_two_logical_ip_groups_remain_one_observed_source(self):
        profile=C.Profile(rooms_per_ip=1,heartbeat_seconds=.15,upload_seconds=.2,
                          replay_bytes=128,evidence_bytes=128,claim_reply_bytes=128)
        result=C.run_stage(self.url,self.token,2,profile,.65,4*C.MIB,0)
        self.assertTrue(result['stage_passed'],result)
        self.assertEqual(result['modeled_ip_groups'],2)
        self.assertEqual(result['server_after']['distinct_source_ips'],1)
        self.assertFalse(result['actual_public_ip_limit_verified'])
        self.assertFalse(result['observed_source_public'])

    def test_nonfinite_and_negative_inputs_rejected(self):
        with self.assertRaises(ValueError):C.Profile(upload_seconds=float('nan')).validate()
        with self.assertRaises(ValueError):C.estimate(C.Profile(),float('inf'),3)
        with self.assertRaises(ValueError):C.estimate(C.Profile(),10,3,baseline_out=-1)

    def test_partial_ramp_report_is_saved_when_receiver_handshake_fails(self):
        with tempfile.TemporaryDirectory()as directory:
            target=Path(directory)/'report.json'
            with patch.dict('os.environ',{'WATCH_CAPACITY_TOKEN':self.token}),patch.object(C,'run_stage',side_effect=RuntimeError('benchmark_http_503')):
                code=C.main(['run','--url',self.url,'--stages','1','--output',str(target)])
            self.assertEqual(code,2)
            self.assertEqual(json.loads(target.read_text())['stopped_error_type'],'RuntimeError')

    def test_oversized_declared_body_rejected_before_read_or_account(self):
        connection=C.http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        self.addCleanup(connection.close)
        connection.request('POST',C.PREFIX+'upload',body=b'x',headers={
            'Authorization':'Bearer '+self.token,'Content-Length':str(C.MAX_BODY+1)})
        response=connection.getresponse();response.read()
        self.assertEqual(response.status,413)
        self.assertEqual(self.state.received,0)

    def test_receiver_payload_mismatch_rejected_before_claim_load(self):
        profile=C.Profile(claim_reply_bytes=129)
        with self.assertRaisesRegex(ValueError,'payload sizes differ'):
            C.run_stage(self.url,self.token,1,profile,.5,C.MIB,0)
        self.assertNotIn(C.PREFIX+'claim',self.state.requests)

    def test_receiver_duration_expiry(self):
        state=C.ReceiverState(self.token,duration=.01)
        state.started-=1
        self.assertFalse(state.admit());self.assertEqual(state.stopped,'receiver_duration_limit')

    def test_production_token_shape_and_nonascii_key_refused(self):
        for key in ('wt_'+'x'*43,'x'*43,'bench_'+'密'*24,'bench_'+'x '*24):
            with self.assertRaises(ValueError):C.checked_bench_key(key)
        self.assertEqual(C.checked_bench_key(self.token),self.token)

    def test_stage_without_uploads_and_heartbeats_cannot_pass(self):
        profile=C.Profile(rooms_per_ip=1,claim_reply_bytes=128)
        result=C.run_stage(self.url,self.token,1,profile,.000001,C.MIB,0)
        self.assertFalse(result['stage_passed'])
        self.assertFalse(result['minimum_workload_covered'])


if __name__=='__main__':unittest.main()
