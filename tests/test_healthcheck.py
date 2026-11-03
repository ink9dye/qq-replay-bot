import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('healthcheck', Path(__file__).resolve().parents[1]/'tools/healthcheck.py')
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def low(mb): return f'可用内存低于 150MB（{mb} MB）'


class AlertStateTests(unittest.TestCase):
    def test_duration_and_changing_measurements_do_not_fake_recovery(self):
        state = {}
        for now, mb in [(1000,84),(1030,83),(1060,82),(1090,81)]:
            self.assertEqual(hc.update_alerts(state,[low(mb)],now,hold=120,max_gap=90),[])
        self.assertEqual(hc.update_alerts(state,[low(80)],1120,hold=120,max_gap=90),[('alert',low(80))])
        self.assertEqual(hc.update_alerts(state,[low(79)],1150,hold=120,max_gap=90),[])
        self.assertEqual(hc.update_alerts(state,[],1180,hold=120,max_gap=90),[('recovered',low(79))])

    def test_short_spike_resets_pending_without_recovery_mail(self):
        state={}
        hc.update_alerts(state,[low(84)],1000,hold=120,max_gap=90)
        self.assertEqual(hc.update_alerts(state,[],1030,hold=120,max_gap=90),[])
        self.assertEqual(hc.update_alerts(state,[low(80)],1120,hold=120,max_gap=90),[])
        self.assertFalse(state['memory_low']['active'])

    def test_missing_samples_cannot_prove_continuous_failure(self):
        state={}
        hc.update_alerts(state,[low(84)],1000,hold=120,max_gap=90)
        self.assertEqual(hc.update_alerts(state,[low(80)],1200,hold=120,max_gap=90),[])
        self.assertEqual(state['memory_low']['first_seen'],1200)

    def test_legacy_state_migrates_and_recurrence_is_not_suppressed(self):
        state={low(84):dict(active=True,ts=1000)}
        self.assertEqual(hc.update_alerts(state,[low(83)],1030),[])
        self.assertEqual(list(state),['memory_low'])
        self.assertEqual(hc.update_alerts(state,[],1060),[('recovered',low(83))])
        self.assertEqual(hc.update_alerts(state,[low(84)],1090),[('alert',low(84))])
        self.assertTrue(state['memory_low']['active'])

    def test_heartbeat_transitions_immediately_and_repeats_after_cooldown(self):
        state={}; issue='8787 /health 非 200（nginx 上游异常）'
        self.assertEqual(hc.update_alerts(state,[issue],1000),[('alert',issue)])
        self.assertEqual(hc.update_alerts(state,[issue],1060),[])
        self.assertEqual(hc.update_alerts(state,[issue],2800),[('alert',issue)])
        self.assertEqual(hc.update_alerts(state,[],2860),[('recovered',issue)])

    def test_dynamic_disk_and_metrics_values_have_stable_identity(self):
        state={}
        hc.update_alerts(state,['磁盘使用 81%','watch-metrics 已 16 分钟未更新'],1000)
        self.assertEqual(hc.update_alerts(state,['磁盘使用 82%','watch-metrics 已 17 分钟未更新'],1060),[])
