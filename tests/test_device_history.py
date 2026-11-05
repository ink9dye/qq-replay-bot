import json
from test_device_dashboard import DeviceDashboardTests


class DeviceHistoryTests(DeviceDashboardTests):
    def test_history_cursor_returns_older_disjoint_pages(self):
        import sqlite3
        with sqlite3.connect(self.database) as db:
            db.executemany('INSERT INTO replays VALUES (?,?,?,?,?,?)',
                [(i,str(i),'A',json.dumps(['A','B']),900,950) for i in range(3,263)])
        first=self.dashboard.recording_history(0)
        older=self.dashboard.recording_history(first['next_before'])
        self.assertEqual(len(first['recordings']),60)
        self.assertEqual(len(older['recordings']),60)
        self.assertLess(max(r['id'] for r in older['recordings']),first['next_before'])
        self.assertFalse({r['digest'] for r in first['recordings']} & {r['digest'] for r in older['recordings']})

    def test_negative_cursor_is_rejected(self):
        with self.assertRaises(ValueError):self.dashboard.recording_history(-1)
