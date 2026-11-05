# -*- coding: utf-8 -*-
"""查询数据层测试：门户透传、TTL 缓存、/query-api 路由、开通查询链路。"""
import json
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from bot.query_auth import QueryAuth
from bot.query_core import PortalClient, PortalUnavailable, QueryCore, admin_issue
from bot.service import _enroll_reply
from bot.watch_api import WatchApiServer
from bot.watch_jobs import RemoteJobs

TOKEN = "test-token"
ADMIN = "admin-token-123"


class FakePortal(PortalClient):
    """替换 get_json 的门户桩：可编程响应与失败。"""

    def __init__(self, responses=None, fail=False):
        super().__init__(base_url="http://127.0.0.1:8788", secret_path=None)
        self.responses = responses or {}
        self.fail = fail
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append((path, params))
        if self.fail:
            raise PortalUnavailable("portal_unreachable")
        if path not in self.responses:
            raise AssertionError(f"未编程的门户路径 {path}")
        return self.responses[path]


class QueryCoreTests(unittest.TestCase):
    def test_passthrough_and_cache(self):
        now = [100.0]
        portal = FakePortal(responses={"/statistics/api/roster": {"members": [1, 2]}})
        core = QueryCore(portal, clock=lambda: now[0])
        self.assertEqual(core.top100(), {"members": [1, 2]})
        self.assertEqual(core.top100(), {"members": [1, 2]})
        self.assertEqual(len(portal.calls), 1)  # 命中缓存
        now[0] += 301.0
        core.top100()
        self.assertEqual(len(portal.calls), 2)  # TTL 过期重取
        self.assertEqual(portal.calls[1][0], "/statistics/api/roster")

    def test_player_endpoints_pass_player_param(self):
        portal = FakePortal(responses={
            "/statistics/api/pt-history": {"status": "ok", "games": []},
            "/statistics/api/player-history": {"text": "历史"},
            "/statistics/api/build": {"cards": []},
            "/statistics/api/recordings": {"items": []},
        })
        core = QueryCore(portal)
        self.assertEqual(core.player_pt("花无十日红"), {"status": "ok", "games": []})
        self.assertEqual(core.player_history("花无十日红"), {"text": "历史"})
        self.assertEqual(core.player_decks("花无十日红"), {"cards": []})
        self.assertEqual(core.player_replays("花无十日红"), {"items": []})
        self.assertTrue(all(params == {"player": "花无十日红"} for _, params in portal.calls))

    def test_portal_down_raises(self):
        core = QueryCore(FakePortal(fail=True))
        with self.assertRaises(PortalUnavailable):
            core.top100()


class QueryApiHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.portal = FakePortal(responses={
            "/statistics/api/roster": {"members": ["甲", "乙"]},
            "/statistics/api/pt-history": {"status": "ok", "games": [1]},
        })
        self.core = QueryCore(self.portal)
        self.qa = QueryAuth(Path(self.tmp.name) / "qa.sqlite3", log=lambda *_: None)
        self.api = WatchApiServer(RemoteJobs(), Path(self.tmp.name) / "replays", TOKEN,
                                  log=lambda *_: None, bind=("127.0.0.1", 0),
                                  query_auth=self.qa, admin_token=ADMIN, query_core=self.core,
                                  writes_enabled=False)
        self.assertTrue(self.api.start())
        self.port = self.api._httpd.server_address[1]
        self.headers = self._login("40001")

    def tearDown(self):
        self.api.stop()
        self.tmp.cleanup()

    def _login(self, identity):
        code = self.qa.provision(identity)["code"]
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/query-api/login",
            data=json.dumps({"account": identity, "secret": code}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as resp:
            body = json.loads(resp.read())
        return {"Authorization": "Bearer " + body["token"]}

    def _get(self, path, headers=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_me_and_top100(self):
        import urllib.error
        status, body = self._get("/query-api/me", self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["identity"], "40001")
        status, body = self._get("/query-api/top100", self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"members": ["甲", "乙"]})
        status, _ = self._get("/query-api/top100")
        self.assertEqual(status, 401)

    def test_player_routes(self):
        status, body = self._get("/query-api/player/%E8%8A%B1%E6%97%A0%E5%8D%81%E6%97%A5%E7%BA%A2/pt", self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"status": "ok", "games": [1]})
        self.assertEqual(self.portal.calls[-1][1], {"player": "花无十日红"})
        # %2F 解码后含路径分隔符 → 400；（多段路径形态在切段时已是 6 段 → 404）
        status, _ = self._get("/query-api/player/%2F/pt", self.headers)
        self.assertEqual(status, 400)
        status, _ = self._get("/query-api/player/%2Fetc/passwd/pt", self.headers)
        self.assertEqual(status, 404)
        status, _ = self._get("/query-api/player/abc/unknown", self.headers)
        self.assertEqual(status, 404)

    def test_search_and_card_endpoints(self):
        self.portal.responses.update({
            "/statistics/api/player-search": {"people": [{"player": "甲", "rank": 1, "points": 1200}], "limited": False},
            "/statistics/api/card-search": {"cards": [{"code": 59400890, "name": "毁灭之黑魔术师"}], "limit": 20},
            "/statistics/api/card-info": {"code": 59400890, "name": "毁灭之黑魔术师", "description": "效果文本"},
            "/statistics/api/card-metrics": {"metrics": {"carrying": {"numerator": 1, "denominator": 2, "percentage": 50.0}},
                                             "by_deck": [{"deck_type": "烙印", "metrics": {}}]},
        })
        status, body = self._get("/query-api/search?type=player&q=%E7%94%B2", self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["people"][0]["player"], "甲")
        status, body = self._get("/query-api/search?type=card&q=%E9%BB%91", self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["cards"][0]["code"], 59400890)
        # 卡组搜索与单卡共用卡名解析；非法类型 400
        status, body = self._get("/query-api/search?type=deck&q=%E9%BB%91", self.headers)
        self.assertEqual(status, 200)
        status, _ = self._get("/query-api/search?type=bogus&q=x", self.headers)
        self.assertEqual(status, 400)
        status, _ = self._get("/query-api/search?type=player&q=", self.headers)
        self.assertEqual(status, 400)
        # 单卡详情 = 效果 + 携带率/胜率
        status, body = self._get("/query-api/card/59400890", self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["details"]["name"], "毁灭之黑魔术师")
        self.assertEqual(body["data"]["metrics"]["metrics"]["carrying"]["percentage"], 50.0)
        status, _ = self._get("/query-api/card/0", self.headers)
        self.assertEqual(status, 400)
        status, _ = self._get("/query-api/card/abc", self.headers)
        self.assertEqual(status, 400)

    def test_pie_unavailable_is_503(self):
        # QueryApiHttpTests 的 core 未注入 store/database → pie 不可用 → 503
        status, body = self._get("/query-api/charts/pie", self.headers)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "query_unavailable")

    def test_subscription_double_gate(self):
        import urllib.error

        def post(payload):
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/query-api/subscriptions",
                data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", **self.headers})
            try:
                with urllib.request.urlopen(request, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read() or b"{}")

        self.core.store = FakeSubStore()
        # 闸 1：全局 writes_enabled=false → 403（账号开关都不看）
        status, body = post({"player": "甲"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "writes_disabled")
        # 闸 2：总闸开、账号只读 → 403
        self.api._httpd.writes_enabled = True
        status, body = post({"player": "甲"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        # 双闸全开：订阅成功，列表可见，取消生效
        self.qa.set_writes("40001", True)
        status, body = post({"player": "甲", "notify": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self._get("/query-api/subscriptions", self.headers)
        self.assertEqual([s["player"] for s in body["data"]["items"]], ["甲"])
        status, body = post({"op": "unsubscribe", "player": "甲"})
        self.assertEqual(status, 200)
        status, body = self._get("/query-api/subscriptions", self.headers)
        self.assertEqual(body["data"]["items"], [])

    def test_portal_down_is_503(self):
        self.portal.fail = True
        status, body = self._get("/query-api/top100", self.headers)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "query_unavailable")
        self.portal.fail = False

    def test_query_core_disabled_is_503(self):
        self.api._httpd.query_core = None
        status, body = self._get("/query-api/top100", self.headers)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "query_disabled")


class FakeSubStore:
    """最小订阅表：只实现 QueryCore 用到的三个方法。"""

    def __init__(self):
        self.rows = {}

    def list_subscriptions(self, user_id=None):
        return [dict(player=p, rank_top=None, notify=False)
                for p in sorted(self.rows.get(user_id, []))]

    def upsert_subscription(self, user_id, player, rank_top, notify, created_at):
        self.rows.setdefault(user_id, set()).add(player)
        return dict(player=player, rank_top=rank_top, notify=notify)

    def delete_subscription(self, user_id, player):
        s = self.rows.get(user_id, set())
        if player in s:
            s.discard(player)
            return True
        return False


class ReplayLinksTests(unittest.TestCase):
    """P3：/s/ 链接组合逻辑——store 行过滤 + create_link 身份/窗口。"""

    class FakeStore:
        def __init__(self, rows):
            self.rows = rows

        def replays_for_player(self, player):
            return self.rows

    class FakeShare:
        def __init__(self):
            self.calls = []

        def create_link(self, user_id, player, ids, range_start, **kw):
            self.calls.append((user_id, player, list(ids), range_start))
            return dict(url="http://x/s/tok", expires_at=999, public=True, token="tok")

    def test_replay_links_filter_and_identity(self):
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            player_dir = Path(tmp) / "甲"
            player_dir.mkdir()
            (player_dir / "a.yrp").write_bytes(b"x")
            store = self.FakeStore([
                dict(id=1, rel_path="甲/a.yrp", recorded_at=time.time()),
                dict(id=2, rel_path="甲/ghost.yrp", recorded_at=time.time()-1),  # 磁盘上不存在
            ])
            share = self.FakeShare()
            core = QueryCore(FakePortal(), store=store, share=share,
                             replay_root=Path(tmp), database=Path(tmp) / "b.sqlite3")
            out = core.player_replays("甲", identity="60001")
            self.assertEqual([i["name"] for i in out["items"]], ["a.yrp"])
            self.assertEqual(out["link"]["url"], "http://x/s/tok")
            self.assertTrue(out["link"]["public"])
            self.assertEqual(share.calls[0][0], "60001")   # 链接按调用者身份建
            self.assertEqual(share.calls[0][2], [1])       # 幽灵行被过滤
            # range_start = now-14天 的真实时间戳
            self.assertLess(share.calls[0][3], time.time())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pie_requires_database(self):
        core = QueryCore(FakePortal())
        with self.assertRaises(PortalUnavailable):
            core.pie()


class EnrollReplyTests(unittest.TestCase):
    def test_success_message_contains_code_and_warning(self):
        text = _enroll_reply({"ok": True, "identity": "123", "code": "abc-XYZ"}, 24)
        self.assertIn("123", text)
        self.assertIn("abc-XYZ", text)
        self.assertIn("24 小时", text)
        self.assertIn("只能使用一次", text)

    def test_already_provisioned_and_down(self):
        self.assertIn("联系管理员", _enroll_reply({"ok": False, "error": "already_provisioned"}, 24))
        self.assertIn("暂不可用", _enroll_reply({"ok": False, "error": "query_service_unavailable"}, 24))
        self.assertIn("账号不存在", _enroll_reply({"ok": False, "error": "not_found"}, 24))
        self.assertIn("暂不可用", _enroll_reply({"ok": False, "error": "unknown_error"}, 24))


class AdminIssueIntegrationTests(unittest.TestCase):
    def test_admin_issue_end_to_end(self):
        """QQ 进程开通查询的真实链路：query_core.admin_issue → 管理接口 → 登录。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            qa = QueryAuth(Path(tmp.name) / "qa.sqlite3", log=lambda *_: None)
            api = WatchApiServer(RemoteJobs(), Path(tmp.name) / "replays", TOKEN,
                                 log=lambda *_: None, bind=("127.0.0.1", 0),
                                 query_auth=qa, admin_token=ADMIN)
            self.assertTrue(api.start())
            port = api._httpd.server_address[1]
            result = admin_issue(f"http://127.0.0.1:{port}", ADMIN, "50001")
            self.assertTrue(result["ok"])
            self.assertTrue(qa.login("50001", result["code"], "1.1.1.1")["ok"])
            # 无 token 被拒
            denied = admin_issue(f"http://127.0.0.1:{port}", "wrong-token", "50002")
            self.assertFalse(denied["ok"])
            # 服务不可达
            down = admin_issue("http://127.0.0.1:1", ADMIN, "50003", timeout=1)
            self.assertEqual(down["error"], "query_service_unavailable")
        finally:
            api.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
