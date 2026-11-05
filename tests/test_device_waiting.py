import json
from test_device_dashboard import DeviceDashboardTests


class WaitingRoomTests(DeviceDashboardTests):
    def test_waiting_matches_capture_not_empty_task_queue(self):
        self.fetch.return_value['pending']=0
        (self.root/'capture-status.json').write_text(json.dumps(dict(updated_at=self.now,waiting_rooms=7)))
        sample=self.dashboard.snapshot()
        self.assertEqual(sample['summary']['pending'],7)
        self.assertEqual(sample['task_board_pending'],0)

    def test_old_missing_or_invalid_capture_is_unknown(self):
        for capture in [dict(updated_at=self.now-91,waiting_rooms=7),
                        dict(updated_at=self.now+1,waiting_rooms=7),
                        dict(updated_at=self.now,waiting_rooms=True)]:
            (self.root/'capture-status.json').write_text(json.dumps(capture))
            self.assertIsNone(self.dashboard._waiting_rooms(self.now))

    def test_fresh_empty_waiting_is_real_zero(self):
        (self.root/'capture-status.json').write_text(json.dumps(dict(updated_at=self.now,waiting_rooms=0)))
        self.assertEqual(self.dashboard.snapshot()['summary']['pending'],0)
