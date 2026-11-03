# -*- coding: utf-8 -*-
"""每设备观战令牌：哈希/绑定/过期/吊销，以及 MoeCardWatch 的 Bearer 链路。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bot import watch_token_command
from bot.commands import parse_command
from bot.store import Store
from bot.watch_api import WatchApiServer
from bot.watch_jobs import RemoteJobs
from bot.watch_tokens import WatchTokenStore, hash_token

LEGACY = "legacy-token"


def _request(port: int, path: str, *, token: str = LEGACY, method: str = "GET",
             payload=None, raw: bytes | None = None, headers=None):
    data = raw
    if data is None and payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("User-Agent", "MoeCardWatch/1.0.0")
    if payload is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        exc.close()
        return exc.code, body


def _get(port: int, path: str, token: str = LEGACY):
    return _request(port, path, token=token)


def _post(port: int, path: str, payload=None, *, token: str = LEGACY,
          raw: bytes | None = None, headers=None):
    return _request(port, path, token=token, method="POST", payload=payload,
                    raw=raw, headers=headers)


class WatchTokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.sqlite3"
        self.clock = [1000.0]
        self.store = WatchTokenStore(self.path, migrate=True, clock=lambda: self.clock[0])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_issue_stores_hash_not_plaintext(self):
        row = self.store.issue("android-1001", "1001", hours=1)
        self.assertTrue(row["token"].startswith("wt_"))
        db = sqlite3.connect(self.path)
        try:
            stored = db.execute("SELECT token_hash, token_hint, worker FROM watch_tokens").fetchone()
        finally:
            db.close()
        self.assertEqual(stored[0], hash_token(row["token"]))
        self.assertNotEqual(stored[0], row["token"])
        self.assertNotIn(row["token"], stored[1])
        self.assertEqual(stored[1][:7], row["token"][:7])
        self.assertEqual(stored[2], "android-1001")

    def test_verify_expiry_and_revoke(self):
        row = self.store.issue("android-1", "1001", hours=1)
        self.assertIsNotNone(self.store.verify(row["token"]))
        self.clock[0] += 3601
        self.assertIsNone(self.store.verify(row["token"]))

        fresh = self.store.issue("android-2", "1002", hours=1)
        self.assertEqual(self.store.revoke_worker("android-2", now=self.clock[0]), 1)
        self.assertIsNone(self.store.verify(fresh["token"]))

    def test_reissue_revokes_old_token(self):
        first = self.store.issue("android-1", "1001", hours=1)
        second = self.store.issue("android-1", "1001", hours=1)
        self.assertIsNone(self.store.verify(first["token"]))
        self.assertIsNotNone(self.store.verify(second["token"]))

    def test_revoke_owner_and_list(self):
        one = self.store.issue("android-1001", "1001", hours=1)
        two = self.store.issue("android-1001-2", "1001", hours=1)
        rows = self.store.list_active()
        self.assertEqual({row["worker"] for row in rows}, {"android-1001", "android-1001-2"})
        self.assertTrue(all("token" not in row for row in rows))
        self.assertEqual(self.store.revoke_owner("1001", now=self.clock[0]), 2)
        self.assertIsNone(self.store.verify(one["token"]))
        self.assertIsNone(self.store.verify(two["token"]))
        self.assertEqual(self.store.list_active(), [])

    def test_zero_hours_is_long_term(self):
        row = self.store.issue("android-1", "1001", hours=0)
        self.assertGreater(row["expires_at"], 1000.0 + 9 * 365 * 86400)

    def test_store_schema_is_created_by_migration(self):
        store = Store(self.path)
        try:
            columns = {row[1] for row in store._db.execute("PRAGMA table_info(watch_tokens)")}
        finally:
            store._db.close()
        self.assertTrue({"token_hash", "token_hint", "worker", "owner", "expires_at",
                         "revoked_at"} <= columns)


class WatchApiTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = [1000.0]
        self.store = WatchTokenStore(self.root / "bot.sqlite3", migrate=True,
                                     clock=lambda: self.clock[0])
        self.jobs = RemoteJobs(lease=30, job_ttl=60)
        self.api = WatchApiServer(self.jobs, self.root / "replays", LEGACY,
                                  log=lambda *_: None, bind=("127.0.0.1", 0),
                                  token_store=self.store)
        self.assertTrue(self.api.start())
        self.port = self.api._httpd.server_address[1]

    def tearDown(self):
        self.api.stop()
        self.store.close()
        self.tmp.cleanup()

    def issue(self, worker="android-1", hours=1):
        return self.store.issue(worker, "1001", hours=hours)["token"]

    def test_legacy_and_device_token_status(self):
        status, _ = _get(self.port, "/watch-api/status", LEGACY)
        self.assertEqual(status, 200)
        token = self.issue()
        status, body = _get(self.port, "/watch-api/status", token)
        self.assertEqual(status, 200)
        self.assertIn("workers", json.loads(body))

    def test_worker_binding_on_claim_heartbeat_report(self):
        token = self.issue("android-1001")
        status, _ = _post(self.port, "/watch-api/claim",
                          {"worker": "android-other", "capacity": 1, "wait": 0}, token=token)
        self.assertEqual(status, 403)
        status, _ = _post(self.port, "/watch-api/claim",
                          {"worker": "android-1001", "capacity": 1, "wait": 0}, token=token)
        self.assertEqual(status, 204)

        status, _ = _post(self.port, "/watch-api/heartbeat",
                          {"worker": "android-other", "active": []}, token=token)
        self.assertEqual(status, 403)
        status, _ = _post(self.port, "/watch-api/report",
                          {"worker": "android-other", "job": "x", "outcome": "saved"},
                          token=token)
        self.assertEqual(status, 403)

    def test_upload_rejects_other_worker(self):
        from bot.watch_jobs import Job
        job = Job({"focus": ["甲"]}, 60)
        job.claimed_by = "android-1001"
        self.jobs._jobs["job-1"] = job
        token = self.issue("android-2")
        status, _ = _post(self.port, "/watch-api/upload?job=job-1&name=a.yrp", token=token,
                          raw=b"YRP2" + b"\x00" * 16,
                          headers={"X-Players": urllib.parse.quote("甲,乙"),
                                   "X-Recorded-At": "1000"})
        self.assertEqual(status, 403)

    def test_revoked_and_expired_tokens_are_401(self):
        token = self.issue()
        self.store.revoke_worker("android-1", now=self.clock[0])
        status, _ = _get(self.port, "/watch-api/status", token)
        self.assertEqual(status, 401)

        token = self.issue("android-2")
        self.clock[0] += 3601
        status, _ = _get(self.port, "/watch-api/status", token)
        self.assertEqual(status, 401)

    def test_unknown_token_is_401(self):
        status, _ = _get(self.port, "/watch-api/status", "wt_unknown")
        self.assertEqual(status, 401)

    def test_moe_card_watch_flow(self):
        """按 APK 的实际请求：status → claim → heartbeat → upload → report。"""
        token = self.issue("android-1001")
        status, _ = _get(self.port, "/watch-api/status", token)
        self.assertEqual(status, 200)

        threading.Thread(target=self.jobs.offer_sync,
                         args=({"room": {"key": "M#1", "players": ["甲", "乙"]},
                                "focus": ["甲"]}, 5), daemon=True).start()
        job = None
        deadline = time.time() + 6
        while time.time() < deadline and not job:
            status, body = _post(self.port, "/watch-api/claim",
                                 {"worker": "android-1001", "capacity": 1, "wait": 1},
                                 token=token)
            if status == 200:
                job = json.loads(body)["job"]
            elif status != 204:
                self.fail(f"claim HTTP {status}: {body!r}")
            else:
                time.sleep(0.02)
        self.assertIsNotNone(job, "没有领到测试任务")

        status, _ = _post(self.port, "/watch-api/heartbeat",
                          {"worker": "android-1001", "active": [job["id"]]}, token=token)
        self.assertEqual(status, 200)
        blob = b"YRP2" + b"\x00" * 32
        status, _ = _post(self.port, f"/watch-api/upload?job={job['id']}&name=abc.yrp",
                          token=token, raw=blob,
                          headers={"X-Players": urllib.parse.quote("甲,乙"),
                                   "X-Recorded-At": "1000"})
        self.assertEqual(status, 200)
        status, _ = _post(self.port, "/watch-api/report",
                          {"worker": "android-1001", "job": job["id"], "outcome": "saved",
                           "detail": {"saved": 1, "uploaded": 1, "bytes": len(blob)}},
                          token=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(list((self.root / "replays").rglob("*.yrp"))), 1)


class _FakeStore:
    def __init__(self, path):
        self.path = path


class _FakeService:
    def __init__(self, root):
        self.config = {"remote": {"public_url": "http://example.test:8787",
                                  "device_token_hours": 1},
                       "statistics": {"allowed_groups": ["123", "1", "90000"],
                                      "private_users": ["1001"]},
                       "share": {}}
        self.data_dir = Path(root)
        self.store = _FakeStore(Path(root) / "bot.sqlite3")
        self.admins = {"999"}
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)


class WatchTokenCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = _FakeService(self.tmp.name)
        self.reader = WatchTokenStore(self.service.store.path, migrate=True)

    def tearDown(self):
        self.reader.close()
        store = getattr(self.service, "_watch_token_store", None)
        if store is not None:
            store.close()
        self.tmp.cleanup()

    def _issue(self, user="1001", args=None, conversation=""):
        return watch_token_command.handle(self.service, user, args or [], conversation)

    def _token(self, reply):
        return reply.split("接口令牌：", 1)[1].splitlines()[0].strip()

    def test_parse_aliases(self):
        self.assertEqual(parse_command("令牌分发")["op"], "watch_token")
        self.assertEqual(parse_command("观战令牌")["op"], "watch_token")
        self.assertEqual(parse_command("接口令牌")["op"], "watch_token")
        self.assertEqual(parse_command("端口令牌")["op"], "watch_token")
        self.assertEqual(parse_command("令牌分发 2")["args"], ["2"])

    def test_denied_outside_confidential_list(self):
        self.assertIn("保密名单", self._issue(conversation="group:999"))
        self.assertIn("保密名单", self._issue(user="1002", args=[]))

    def test_service_handle_text_dispatch(self):
        from bot.service import BotService
        service = object.__new__(BotService)
        service.config = self.service.config
        service.data_dir = self.service.data_dir
        service.store = self.service.store
        service.admins = self.service.admins
        service.authorized = lambda user: True
        service._statistics_allowed = lambda *args: True
        service.write_log = lambda text: None
        reply = BotService.handle_text(service, "1001", "观战令牌", "group:1")
        self.assertIn("接口令牌：wt_", reply)
        store = getattr(service, "_watch_token_store", None)
        if store is not None:
            store.close()

    def test_group_issue_message_has_app_fields(self):
        reply = self._issue(conversation="group:123")
        self.assertIn("服务器地址：http://example.test:8787", reply)
        self.assertIn("接口令牌：wt_", reply)
        self.assertIn("客户端名称：android-1001", reply)
        self.assertIn("有效期至：", reply)
        token = self._token(reply)
        self.assertIsNotNone(self.reader.verify(token))

    def test_public_url_fallback(self):
        self.service.config["remote"].pop("public_url")
        reply = self._issue()
        self.assertIn("服务器地址：http://101.201.108.188:8787", reply)

    def test_second_slot_and_reissue(self):
        first = self._issue(args=[])
        second = self._issue(args=["2"])
        self.assertIn("客户端名称：android-1001", first)
        self.assertIn("客户端名称：android-1001-2", second)
        self.assertIsNotNone(self.reader.verify(self._token(first)))
        self.assertIsNotNone(self.reader.verify(self._token(second)))

        # 同一 slot 重发前先把冷却时间推远，验证旧令牌被吊销。
        self.service._watch_token_issued_at[("1001", 1)] = time.time() - 60
        old = self._issue(args=[])
        self.service._watch_token_issued_at[("1001", 1)] = time.time() - 60
        new = self._issue(args=[])
        self.assertIsNone(self.reader.verify(self._token(old)))
        self.assertIsNotNone(self.reader.verify(self._token(new)))

    def test_cooldown_keeps_previous_token(self):
        first = self._issue()
        second = self._issue()
        self.assertIn("令牌刚发过", second)
        self.assertIsNotNone(self.reader.verify(self._token(first)))

    def test_admin_list_and_revoke(self):
        reply = self._issue()
        token = self._token(reply)
        self.assertIn("仅管理员", self._issue(user="1001", args=["列表"]))
        self.assertIn("私聊", self._issue(user="999", args=["列表"], conversation="group:1"))
        listing = self._issue(user="999", args=["列表"])
        self.assertIn("android-1001", listing)
        self.assertIn("wt_", listing)
        self.assertNotIn(token, listing)

        revoked = self._issue(user="999", args=["吊销", "android-1001"])
        self.assertIn("1", revoked)
        self.assertIsNone(self.reader.verify(token))

    def test_admin_revoke_by_qq(self):
        first = self._token(self._issue(args=[]))
        second = self._token(self._issue(args=["2"]))
        reply = self._issue(user="999", args=["吊销", "QQ", "1001"])
        self.assertIn("2", reply)
        self.assertIsNone(self.reader.verify(first))
        self.assertIsNone(self.reader.verify(second))

    def test_help_and_bad_slot(self):
        self.assertIn("令牌分发", self._issue(args=["帮助"]))
        self.assertIn("令牌分发 2", self._issue(args=["abc"]))


class WatchTokenGroupFlowTests(unittest.TestCase):
    def test_group_message_publishes_token(self):
        from test_bot import temporary_service
        from test_groups import GroupQQ, group_event

        with temporary_service() as service:
            service.group_commands = True
            service.group_without_at = True
            service.allow_all_users = True
            service.config["remote"] = {"public_url": "http://example.test:8787",
                                        "token_groups": ["90000"],
                                        "token_users": ["10001"]}
            service.qq = GroupQQ()
            event = group_event("令牌分发")
            event["message"] = [{"type": "text", "data": {"text": "观战令牌"}}]
            try:
                asyncio.run(service.on_qq_event(event))
                reply = service.qq.group_sent[-1][1]
                self.assertIn("接口令牌：wt_", reply)
                self.assertIn("客户端名称：android-10001", reply)
                self.assertIn("服务器地址：", reply)
            finally:
                store = getattr(service, "_watch_token_store", None)
                if store is not None:
                    store.close()
