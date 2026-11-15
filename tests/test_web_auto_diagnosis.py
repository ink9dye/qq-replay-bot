import json
from pathlib import Path
import tempfile
import unittest

from tools.web_latency_report import build_report


class AutoDiagnosisTests(unittest.TestCase):
    def report(self, workers=(), observations=(), until=200):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            access=root/'web-access.jsonl'
            access.write_text('',encoding='utf8')
            def write(name, values):
                p=root/name
                p.write_text(''.join(json.dumps(r)+'\n' for r in values),encoding='utf8')
                return p
            write('statistics-query-timings.jsonl',workers)
            obs=write('obs.jsonl',observations)
            return build_report([access],[obs],since=1,until=until)

    def test_pairs_exclude_missing_fields_and_do_not_sum_nested_phases(self):
        report=self.report([
            dict(at=100,kind='recordings',status=200,recordings_total_ms=10000,
                 recordings_eligible_ms=6000,identity_read_ms=5000,
                 recordings_filter_ms=2,params='PRIVATE'),
            dict(at=101,kind='recordings',status=200,recordings_total_ms=90000)])
        self.assertIn('配对1/2条，累计6.00秒，占配对执行60.0%',report)
        self.assertIn('身份读取（嵌套）',report)
        self.assertIn('少量样本',report)
        self.assertIn('阶段覆盖不完整',report)
        self.assertIn('不足1%',report)
        self.assertNotIn('PRIVATE',report)
        self.assertNotIn('110.0%',report)

    def test_queue_is_independent_and_failed_tasks_do_not_rank(self):
        report=self.report([
            dict(at=100,kind='group-list',status=200,elapsed_ms=1000,queue_wait_ms=95000),
            dict(at=101,kind='recordings',status=500,recordings_total_ms=10000,recordings_eligible_ms=9000)])
        self.assertIn('最大95.00秒（独立于执行）',report)
        self.assertNotIn('9500.0%',report)
        self.assertIn('没有成功录像计算分阶段样本',report)

    def test_stale_samples_and_resource_pressure_are_explicit(self):
        report=self.report([dict(at=100,kind='recordings',status=200,recordings_total_ms=10000,
                                recordings_eligible_ms=8000)],
                           [dict(at=100,psi_io_full=80,mem_available=120)],until=30000)
        self.assertIn('超过6小时',report)
        self.assertIn('IO full峰值80.0%',report)
        self.assertIn('未证明慢任务因果',report)

    def test_detail_does_not_substitute_for_sample_or_download(self):
        report=self.report([dict(at=100,kind='deck-detail',status=200,elapsed_ms=50000,
                                identity_read_ms=25000)])
        self.assertIn('deck-detail：身份读取',report)
        self.assertIn('录像详情sample访问0次',report)
        self.assertIn('不能用deck-detail构筑任务代替',report)

    def test_no_data_or_nonfinite_metrics_do_not_invent_a_bottleneck(self):
        for workers in ([],[dict(at=100,kind='recordings',status=200,
                               recordings_total_ms=0,recordings_eligible_ms=float('nan'))]):
            report=self.report(workers)
            self.assertIn('未达到诊断阈值',report)
            self.assertIn('不能换算用户最终失败率',report)


if __name__=='__main__':
    unittest.main()
