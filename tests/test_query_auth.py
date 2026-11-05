# -*- coding: utf-8 -*-
"""查询平台认证体系测试：一次性码、双令牌轮换、锁定、管理操作（交接文档 v5）。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from bot.query_auth import QueryAuth
from bot.watch_api import WatchApiServer
from bot.watch_jobs import RemoteJobs

TOKEN = "test-token"
ADMIN = "admin-token-123"


def _post(port, path, payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {}


def _get(port, path, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {}


class QueryAuthUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [1_700_000_000.0]
        self.qa = QueryAuth(Path(self.tmp.name) / "qa.sqlite3",
                            token_hours=24, refresh_days=180, code_hours=24,
                            lock_after_failures=3, lock_minutes=15,
                            clock=lambda: self.now[0], log=lambda *_: None)

    def tearDown(self):
        self.tmp.cleanup()

    def _provision_and_login(self, identity="10001", ip="1.1.1.1"):
        r = self.qa.provision(identity)
        self.assertTrue(r["ok"])
        r2 = self.qa.login(identity, r["code"], ip)
        self.assertTrue(r2["ok"], r2)
        return r2

    def test_provision_login_authenticate(self):
        r = self._provision_and_login()
        self.assertFalse(r["writes"])
        self.assertEqual(r["identity"], "10001")
        info = self.qa.authenticate(r["token"])
        self.assertEqual(info["identity"], "10001")
        self.assertEqual(info["role"], "user")

    def test_activation_code_is_one_time(self):
        r = self.qa.provision("10002")
        self.assertTrue(self.qa.login("10002", r["code"], "1.1.1.1")["ok"])
        again = self.qa.login("10002", r["code"], "1.1.1.1")
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"], "bad_credentials")

    def test_duplicate_provision_refused(self):
        self.assertTrue(self.qa.provision("10003")["ok"])
        dup = self.qa.provision("10003")
        self.assertFalse(dup["ok"])
        self.assertEqual(dup["error"], "already_provisioned")

    def test_reissue_invalidates_old_pending_code(self):
        self.qa.provision("10004")
        first = self.qa.reissue_code("10004")
        second = self.qa.reissue_code("10004")
        self.assertEqual(self.qa.login("10004", first["code"], "1.1.1.1")["error"], "bad_credentials")
        self.assertTrue(self.qa.login("10004", second["code"], "1.1.1.1")["ok"])

    def test_code_expiry(self):
        r = self.qa.provision("10005")
        self.now[0] += 24 * 3600 + 1
        self.assertEqual(self.qa.login("10005", r["code"], "1.1.1.1")["error"], "bad_credentials")

    def test_lockout_per_account_and_ip_then_unlock(self):
        self.qa.provision("10006")
        for _ in range(3):
            self.qa.login("10006", "wrong", "9.9.9.9")
        # 正确的码也被锁（账号与 IP 两个维度都已累计满）
        code = self.qa.reissue_code("10006")["code"]
        self.assertEqual(self.qa.login("10006", code, "9.9.9.9")["error"], "locked")
        # 其他账号同 IP 也被锁（IP 维度）
        self.qa.provision("10007")
        self.assertEqual(self.qa.login("10007", self.qa.reissue_code("10007")["code"],
                                       "9.9.9.9")["error"], "locked")
        # 解锁账号后，换一个未锁的 IP 用正确的码应能成功（失败登录不耗用激活码）
        self.assertTrue(self.qa.unlock("user:10006")["ok"])
        self.assertTrue(self.qa.login("10006", code, "8.8.8.8")["ok"])
        # 成功已耗用该码，重放失败
        self.assertEqual(self.qa.login("10006", code, "8.8.8.8")["error"], "bad_credentials")

    def test_refresh_rotation_and_expiry(self):
        r = self._provision_and_login("20001")
        r2 = self.qa.refresh(r["refresh_token"])
        self.assertTrue(r2["ok"])
        # 旧 refresh 已轮换作废
        self.assertEqual(self.qa.refresh(r["refresh_token"])["error"], "bad_credentials")
        # 新 access 可用
        self.assertEqual(self.qa.authenticate(r2["token"])["identity"], "20001")
        # refresh 令牌过期后必须重新登录
        self.now[0] += 181 * 86400
        self.assertEqual(self.qa.refresh(r2["refresh_token"])["error"], "bad_credentials")

    def test_access_token_expiry(self):
        r = self._provision_and_login("20002")
        self.now[0] += 25 * 3600
        self.assertIsNone(self.qa.authenticate(r["token"]))

    def test_logout_revokes_only_own_pair(self):
        a = self._provision_and_login("20003")
        self.qa.logout(a["token"])
        self.assertIsNone(self.qa.authenticate(a["token"]))
        self.assertIsNone(self.qa.refresh(a["refresh_token"]).get("token"))

    def test_reset_revokes_tokens_and_issues_new_code(self):
        r = self._provision_and_login("20004")
        reset = self.qa.reset_user("20004")
        self.assertTrue(reset["ok"])
        self.assertIsNone(self.qa.authenticate(r["token"]))
        self.assertTrue(self.qa.login("20004", reset["code"], "1.1.1.1")["ok"])

    def test_disable_user_blocks_everything(self):
        self._provision_and_login("20005")
        self.assertTrue(self.qa.disable_user("20005")["ok"])
        r = self.qa.provision("20005")
        # 已注销账号重新开通也应拒绝（注销是管理员操作，恢复走管理员）
        self.assertFalse(r["ok"])
        self.assertTrue(self.qa.unlock("user:20005")["ok"])

    def test_writes_toggle_takes_effect_immediately(self):
        r = self._provision_and_login("20006")
        self.assertFalse(self.qa.authenticate(r["token"])["writes"])
        self.assertTrue(self.qa.set_writes("20006", True)["ok"])
        self.assertTrue(self.qa.authenticate(r["token"])["writes"])

    def test_revoke_device_by_prefix(self):
        a = self._provision_and_login("20007")
        token_id = self.qa.list_users()[0]["devices"][0]["token_id"]
        self.assertTrue(self.qa.revoke_device(token_id)["ok"])
        self.assertIsNone(self.qa.authenticate(a["token"]))
        self.assertEqual(self.qa.revoke_device(token_id)["error"], "not_found")

    def test_list_users_leak_check(self):
        r = self._provision_and_login("20008")
        users = self.qa.list_users()
        blob = json.dumps(users)
        self.assertNotIn(r["token"], blob)
        self.assertNotIn(r["refresh_token"], blob)
        self.assertTrue(all("code" not in u for u in users))


class QueryAuthHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [1_700_000_000.0]
        self.qa = QueryAuth(Path(self.tmp.name) / "qa.sqlite3", clock=lambda: self.now[0],
                            lock_after_failures=50, log=lambda *_: None)
        self.api = WatchApiServer(RemoteJobs(), Path(self.tmp.name) / "replays", TOKEN,
                                  log=lambda *_: None, bind=("127.0.0.1", 0),
                                  query_auth=self.qa, admin_token=ADMIN)
        self.assertTrue(self.api.start())
        self.port = self.api._httpd.server_address[1]

    def tearDown(self):
        self.api.stop()
        self.tmp.cleanup()

    def test_watch_api_still_requires_remote_token(self):
        status, _ = _get(self.port, "/watch-api/status")
        self.assertEqual(status, 401)
        status, body = _get(self.port, "/watch-api/status", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_login_flow_over_http(self):
        r = self.qa.provision("30001")["code"]
        status, body = _post(self.port, "/query-api/login", {"account": "30001", "secret": "wrong"})
        self.assertEqual(status, 401)
        status, body = _post(self.port, "/query-api/login", {"account": "30001", "secret": r})
        self.assertEqual(status, 200)
        self.assertTrue(body["token"])
        # 码已耗用
        status, _ = _post(self.port, "/query-api/login", {"account": "30001", "secret": r})
        self.assertEqual(status, 401)

    def test_command_endpoint_requires_authentication(self):
        status, _ = _post(self.port, "/query-api/command", {"cmd": "x"},
                          {"Authorization": "Bearer whatever"})
        self.assertEqual(status, 401)

    def test_status_requires_user_token(self):
        status, _ = _get(self.port, "/query-api/status")
        self.assertEqual(status, 401)
        code = self.qa.provision("30002")["code"]
        _, body = _post(self.port, "/query-api/login", {"account": "30002", "secret": code})
        status, body = _get(self.port, "/query-api/status", {"Authorization": "Bearer " + body["token"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["identity"], "30002")

    def test_logout_over_http(self):
        code = self.qa.provision("30003")["code"]
        _, body = _post(self.port, "/query-api/login", {"account": "30003", "secret": code})
        status, _ = _post(self.port, "/query-api/logout", None, {"Authorization": "Bearer " + body["token"]})
        self.assertEqual(status, 200)
        self.assertIsNone(self.qa.authenticate(body["token"]))

    def test_admin_endpoints_require_admin_token(self):
        status, _ = _post(self.port, "/query-api/admin/issue", {"identity": "30004"})
        self.assertEqual(status, 401)
        status, body = _post(self.port, "/query-api/admin/issue", {"identity": "30004"},
                             {"X-Admin-Token": ADMIN})
        self.assertEqual(status, 200)
        self.assertTrue(body["code"])
        # 发放后走完整登录链路
        status, body = _post(self.port, "/query-api/login", {"account": "30004", "secret": body["code"]})
        self.assertEqual(status, 200)
        status, body = _get(self.port, "/query-api/admin/list", {"X-Admin-Token": ADMIN})
        self.assertEqual(status, 200)
        self.assertEqual(body["users"][0]["identity"], "30004")

    def test_admin_writes_and_reset_over_http(self):
        code = self.qa.provision("30005")["code"]
        status, _ = _post(self.port, "/query-api/admin/writes", {"identity": "30005", "enabled": True},
                          {"X-Admin-Token": ADMIN})
        self.assertEqual(status, 200)
        _, body = _post(self.port, "/query-api/login", {"account": "30005", "secret": code})
        self.assertTrue(body["writes"])
        old_token = body["token"]
        # 重置：旧令牌全部作废 + 发新码
        status, reset_body = _post(self.port, "/query-api/admin/reset", {"identity": "30005"},
                                   {"X-Admin-Token": ADMIN})
        self.assertEqual(status, 200)
        self.assertTrue(reset_body["code"])
        self.assertIsNone(self.qa.authenticate(old_token))
        self.assertTrue(self.qa.login("30005", reset_body["code"], "1.1.1.1")["ok"])


if __name__ == "__main__":
    unittest.main()
