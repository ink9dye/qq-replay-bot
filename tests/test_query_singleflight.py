# -*- coding: utf-8 -*-
"""查询缓存单飞：同 key 只算一次，不同 key 可并行。"""
import threading
import time
import unittest

from bot.query_core import PortalBusy, PortalUnavailable, QueryCore


class FakePortal:
    def get_json(self, path, params=None):
        raise AssertionError("单飞测试不应打到门户")


class SingleFlightTests(unittest.TestCase):
    def test_same_key_only_one_compute(self):
        core = QueryCore(FakePortal())
        started = threading.Event()
        release = threading.Event()
        calls = []

        def work():
            calls.append(1)
            started.set()
            release.wait(2)
            return {"value": 1}

        results = []

        def worker():
            try:
                results.append(("ok", core._cached(("same",), 10.0, work)))
            except PortalBusy as exc:
                results.append(("busy", exc.retry_after))

        first = threading.Thread(target=worker)
        first.start()
        self.assertTrue(started.wait(1))
        second = threading.Thread(target=worker)
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(2)
        second.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(kind for kind, _ in results), ["busy", "ok"])
        self.assertEqual([value for kind, value in results if kind == "busy"][0], 2)

    def test_different_keys_run_concurrently(self):
        core = QueryCore(FakePortal())
        entered = []
        release = threading.Event()

        def make(key):
            def work():
                entered.append(key)
                if len(entered) == 2:
                    release.set()
                release.wait(2)
                return {"key": key}
            return work

        threads = [threading.Thread(target=lambda k=k: core._cached((k,), 10.0, make(k)))
                   for k in ("a", "b")]
        for thread in threads:
            thread.start()
        self.assertTrue(release.wait(2), "不同 key 未能并行进入计算")
        for thread in threads:
            thread.join(2)
        self.assertEqual(sorted(entered), ["a", "b"])

    def test_portal_unavailable_clears_inflight(self):
        core = QueryCore(FakePortal())
        calls = []

        def fail():
            calls.append(1)
            raise PortalUnavailable("down")

        with self.assertRaises(PortalUnavailable):
            core._cached(("down",), 10.0, fail)
        with self.assertRaises(PortalUnavailable):
            core._cached(("down",), 10.0, fail)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
