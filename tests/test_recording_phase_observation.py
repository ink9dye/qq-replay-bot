import gzip,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from recording_filter_fixture import set_up
import test_card_statistics as C
from bot.statistics_recording_filters import query
from bot.web_perf_metrics import request_metrics,timed
from tools.web_latency_report import build_report,log_family,rows


class PhaseTests(unittest.TestCase):
    setUp=set_up

    def test_results_identical_with_complete_phase_fields_and_no_inputs(self):
        for args in ({},{'card':['1'],'mode':['opening']},{'not_card':['1']},
                     {'opponent_card':['3'],'opponent_mode':['not_drawn']}):
            before=query(self.store._db,self.replays,C.NOW,args)
            with request_metrics('refresh-worker') as metrics:
                after=query(self.store._db,self.replays,C.NOW,args)
            self.assertEqual(before,after)
            for field in ('recordings_total_ms','recordings_eligible_ms','identity_read_ms',
                          'recordings_opponent_ms','recordings_naming_ms','recordings_filter_ms','recordings_sort_ms'):
                self.assertGreaterEqual(metrics[field],0,field)
            if args:self.assertIn('recordings_facts_ms',metrics)
            if args.get('mode')==['opening']:self.assertIn('recordings_opening_ms',metrics)
            self.assertNotIn(self.rows[0]['digest'],json.dumps(metrics))

    def test_failure_is_still_failure_and_phase_is_closed(self):
        @timed('test_phase_ms')
        def fail():raise ValueError('expected')
        with request_metrics() as metrics:
            with self.assertRaisesRegex(ValueError,'expected'):fail()
        self.assertIn('test_phase_ms',metrics)


class RotationTests(unittest.TestCase):
    def write(self,path,rows_,compressed=False):
        text=''.join(json.dumps(r)+'\n' for r in rows_)
        if compressed:
            with gzip.open(path,'wt',encoding='utf8') as f:f.write(text)
        else:path.write_text(text,encoding='utf8')

    def test_family_dedup_rotation_and_worker_stage_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);access=root/'web-access.jsonl'
            row=dict(at=100,path='/statistics/api/recordings',status=200,elapsed_ms=1)
            self.write(access,[row]);self.write(Path(str(access)+'.1'),[row])
            self.write(Path(str(access)+'.1.gz'),[row],True)
            self.write(Path(str(access)+'.2.gz'),[row],True)
            self.write(root/'statistics-query-timings.jsonl.1.gz',[
                dict(at=100,kind='recordings',status=200,elapsed_ms=10,recordings_filter_ms=0),
                dict(at=101,kind='recordings',status=200,elapsed_ms=20)],True)
            expanded=log_family([access,Path(str(access)+'.1'),access.parent/'.'/access.name])
            self.assertEqual(len(expanded),3)
            report=build_report([access,access],since=90,until=110)
            self.assertIn('3/3',report)
            self.assertIn('recordings_filter_ms | 1 | 0.000',report)
            self.assertIn('recordings_facts_ms | 0 | 未知',report)
            self.assertIn('statistics-query-timings.jsonl.1.gz',report)
            self.assertIn('不能重复相加',report)

    def test_gzip_bound_truncation_and_bad_gzip_are_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'web-access.jsonl.1.gz';coverage={}
            self.write(p,[dict(at=100),dict(private='x'*1000)],True)
            with patch('tools.web_latency_report.MAX_BYTES',64):
                self.assertEqual(len(list(rows(p,coverage))),1)
            self.assertTrue(coverage[p]['truncated'])
            p.write_bytes(b'not-gzip');self.assertEqual(list(rows(p,coverage)),[])
            self.assertEqual(coverage[p]['status'],'read_error')
