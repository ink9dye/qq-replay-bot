import json
import tempfile
import unittest
from pathlib import Path

from bot.statistics_rank_scope import _stored_ranks
from bot.statistics_side_recent import RANK_SNAPSHOT_SECONDS


class RankSnapshotFreshnessTests(unittest.TestCase):
    """换备按 14 天取快照；其它入口默认仍是 7 天。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.now = 1794200000.0

    def write_index(self, age_seconds):
        payload = dict(saved_at=self.now - age_seconds, players={'某人': 150})
        (self.dir / 'rank-scope-index.json').write_text(json.dumps(payload), encoding='utf-8')

    def test_default_window_is_seven_days(self):
        self.write_index(10 * 86400)
        self.assertEqual(_stored_ranks(self.dir, self.now), {})

    def test_fourteen_days_accepted(self):
        self.write_index(10 * 86400)
        got = _stored_ranks(self.dir, self.now, max_age=14 * 86400)
        self.assertEqual(got, {'某人': (150, self.now - 10 * 86400)})

    def test_older_than_fourteen_still_dropped(self):
        self.write_index(15 * 86400)
        self.assertEqual(_stored_ranks(self.dir, self.now, max_age=14 * 86400), {})

    def test_side_recent_uses_fourteen_days(self):
        self.assertEqual(RANK_SNAPSHOT_SECONDS, 14 * 86400)


if __name__ == '__main__':
    unittest.main()
