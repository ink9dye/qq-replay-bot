"""远程观战：服务器只下发房间信息，入房口令由观战端本地账号计算。"""
import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import watch

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import watch_worker  # noqa: E402


def _writer(targets):
    return SimpleNamespace(targets=list(targets), mode="exact")


class RemotePayloadTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(host="tiramisu.moenext.com", port=8911, version="0x1362",
                           nickname="Observer", mycard_secret=123456789)
        self.room = watch.RoomInfo(key="M#1", title="测试房", join_id="12345678",
                                   players=["甲", "乙"], started=True,
                                   seats={0: "甲", 1: "乙"})

    def test_payload_does_not_carry_password(self):
        payload = watch.remote_payload(self.config, self.room, _writer(["甲"]), 1000, "前100")
        self.assertNotIn("password", payload)
        self.assertNotIn("123456789", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["room"]["join_id"], "12345678")
        self.assertEqual(payload["room"]["seats"], {"0": "甲", "1": "乙"})
        self.assertEqual(payload["server"], dict(host="tiramisu.moenext.com", port=8911,
                                                 version=0x1362))
        self.assertEqual(payload["focus"], ["甲"])
        # 连服务器账号的昵称也不下发，观战端用自己的账号名进场。
        self.assertNotIn("nickname", payload)

    def test_local_password_is_per_room(self):
        first = watch.build_join_password(self.config, self.room)
        other = watch.RoomInfo(key="M#2", title="另一间", join_id="87654321",
                               players=["丙", "丁"], seats={0: "丙", 1: "丁"})
        second = watch.build_join_password(self.config, other)
        self.assertEqual(len(first), 16)
        self.assertEqual(len(second), 16)
        self.assertNotEqual(first, second)
        prefix = watch.encode_mycard_password(123456789, 3, "")
        self.assertEqual(len(prefix), 8)
        self.assertTrue(first.startswith(prefix))
        self.assertTrue(second.startswith(prefix))


class WorkerConfigTests(unittest.TestCase):
    def tearDown(self):
        watch_worker.ACCOUNT.update(account="", password="", secret=0, nickname="", signed_at=0.0)

    def test_job_config_uses_local_credentials_and_secret(self):
        watch_worker.ACCOUNT.update(account="local-user", password="local-pass",
                                    secret=42, nickname="本地名", signed_at=0.0)
        config = watch_worker.job_config({"server": {"host": "h", "port": 1, "version": 7}},
                                         {"account": "local-user", "password": "local-pass"},
                                         Path("."))
        self.assertEqual(config["account"], "local-user")
        self.assertEqual(config["password"], "local-pass")
        self.assertEqual(config["mycard_secret"], 42)
        self.assertEqual(config["nickname"], "本地名")
        self.assertEqual(config["join_mode"], "mycard")
        self.assertEqual((config["host"], config["port"], config["version"]), ("h", 1, 7))

    def test_missing_account_refuses_to_start(self):
        with self.assertRaises(SystemExit):
            watch_worker.load_account({"server": "http://x", "token": "t", "name": "n"})


class SecretRefreshTests(unittest.TestCase):
    """远程观战端每个任务一个事件循环，刷新凭据不能串到别的 loop 上炸掉。"""

    def setUp(self):
        self.original = watch.refresh_secret
        self.calls = 0
        self.lock = threading.Lock()
        watch._SECRET_REFRESHED_AT = 0.0

    def tearDown(self):
        watch.refresh_secret = self.original
        watch._SECRET_REFRESHED_AT = 0.0

    def test_concurrent_event_loops_refresh_once(self):
        def fake(config):
            with self.lock:
                self.calls += 1
            time.sleep(0.15)
            config["mycard_secret"] = 99
            config["nickname"] = "n"

        watch.refresh_secret = fake
        configs = [dict(mycard_secret=1) for _ in range(4)]
        errors = []

        def run(cfg):
            try:
                asyncio.run(watch.refresh_secret_shared(cfg))
            except BaseException as exc:  # noqa: BLE001 - 测试要把异常带回来
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(cfg,)) for cfg in configs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(self.calls, 1)
        # 只有一个任务真正刷新；其余在 60 秒窗口内直接沿用（它们本来就有口令）。
        self.assertEqual(sorted(cfg["mycard_secret"] for cfg in configs), [1, 1, 1, 99])


class WorkerAccountRefreshTests(unittest.TestCase):
    def setUp(self):
        self.original = watch.refresh_secret
        self.calls = 0
        watch_worker.ACCOUNT.update(account="", password="", secret=0, nickname="", signed_at=0.0)

    def tearDown(self):
        watch.refresh_secret = self.original
        watch_worker.ACCOUNT.update(account="", password="", secret=0, nickname="", signed_at=0.0)

    def test_fresh_secret_skips_network(self):
        watch_worker.ACCOUNT.update(secret=7, nickname="n", signed_at=time.time())
        state = watch_worker.refresh_account({"account": "a", "password": "p"})
        self.assertEqual((state["secret"], state["nickname"]), (7, "n"))
        self.assertEqual(self.calls, 0)

    def test_stale_secret_refreshes(self):
        watch_worker.ACCOUNT.update(secret=7, nickname="old", signed_at=0.0)

        def fake(config):
            self.calls += 1
            config["mycard_secret"] = 8
            config["nickname"] = "new"

        watch.refresh_secret = fake
        state = watch_worker.refresh_account({"account": "a", "password": "p"})
        self.assertEqual(self.calls, 1)
        self.assertEqual((state["secret"], state["nickname"]), (8, "new"))


class JoinPacingTests(unittest.TestCase):
    def test_join_pacing_spaces_connections(self):
        old = (watch_worker.JOIN_MIN_INTERVAL, watch_worker.JOIN_JITTER, watch_worker.LAST_JOIN_AT)
        watch_worker.JOIN_MIN_INTERVAL = 0.05
        watch_worker.JOIN_JITTER = 0.0
        watch_worker.LAST_JOIN_AT = 0.0
        try:
            started = time.time()
            watch_worker._pace_join()
            watch_worker._pace_join()
            self.assertGreaterEqual(time.time() - started, 0.045)
        finally:
            watch_worker.JOIN_MIN_INTERVAL, watch_worker.JOIN_JITTER, watch_worker.LAST_JOIN_AT = old


class RoomDedupeTests(unittest.TestCase):
    def setUp(self):
        with watch_worker.ROOM_LOCK:
            watch_worker.ROOM_ACTIVE.clear()
            watch_worker.ROOM_SEEN.clear()

    def tearDown(self):
        with watch_worker.ROOM_LOCK:
            watch_worker.ROOM_ACTIVE.clear()
            watch_worker.ROOM_SEEN.clear()

    def test_same_room_is_recorded_once(self):
        self.assertEqual(watch_worker._room_claim("M#1"), (True, ""))
        self.assertEqual(watch_worker._room_claim("M#1"), (False, "active"))
        watch_worker._room_release("M#1")
        self.assertEqual(watch_worker._room_claim("M#1"), (False, "recent"))
        self.assertEqual(watch_worker._room_claim("M#2"), (True, ""))

    def test_expired_room_can_be_claimed_again(self):
        watch_worker.ROOM_SEEN["M#3"] = time.time() - watch_worker.ROOM_DEDUPE_SECONDS - 1
        self.assertEqual(watch_worker._room_claim("M#3"), (True, ""))


if __name__ == "__main__":
    unittest.main()
