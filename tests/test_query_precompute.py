# -*- coding: utf-8 -*-
"""磁盘预计算缓存：路径、读写、过期、新录像失效、null 与清理。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from bot.query_precompute import MISS, cache_path, cleanup, read_precomputed, write_precomputed


class PrecomputeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_hash_path(self):
        payload = {"cards": [{"code": 89631139, "copies": 3}]}
        path = write_precomputed(self.data, "一之瀨帆波", payload, latest_replay_at=100.0, ttl_seconds=60)
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.data / "query-cache" / "build")
        self.assertNotIn("一之瀨帆波", path.name)
        self.assertLessEqual(path.stat().st_size, 4096)
        self.assertEqual(read_precomputed(self.data, "一之瀨帆波", 100.0), payload)

    def test_expired_is_miss(self):
        write_precomputed(self.data, "甲", {"x": 1}, latest_replay_at=0, ttl_seconds=60)
        path = cache_path(self.data, "甲")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["expires_at"] = 0
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertIs(read_precomputed(self.data, "甲", 0), MISS)

    def test_newer_replay_invalidates(self):
        write_precomputed(self.data, "甲", {"x": 1}, latest_replay_at=100.0, ttl_seconds=60)
        self.assertIs(read_precomputed(self.data, "甲", 200.0), MISS)
        self.assertEqual(read_precomputed(self.data, "甲", 100.0), {"x": 1})

    def test_null_payload_is_cached(self):
        write_precomputed(self.data, "甲", None, latest_replay_at=0, ttl_seconds=60)
        self.assertIsNone(read_precomputed(self.data, "甲", 0))

    def test_wrong_player_or_version_is_miss(self):
        write_precomputed(self.data, "甲", {"x": 1}, latest_replay_at=0, ttl_seconds=60)
        path = cache_path(self.data, "甲")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["player"] = "乙"
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertIs(read_precomputed(self.data, "甲", 0), MISS)
        doc["player"] = "甲"
        doc["version"] = 99
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertIs(read_precomputed(self.data, "甲", 0), MISS)

    def test_cleanup_removes_old_files(self):
        path = write_precomputed(self.data, "甲", {"x": 1}, latest_replay_at=0, ttl_seconds=60)
        old = time.time() - 10 * 86400
        os.utime(path, (old, old))
        self.assertEqual(cleanup(self.data, 7), 1)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
