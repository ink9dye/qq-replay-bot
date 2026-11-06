#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询 API 本地 mock（联调用）。

对齐 bot/query_api.py 的路由与响应约定，供安卓端 adb reverse / 模拟器联调：
    python tools/mock_query_server.py            # 127.0.0.1:8791
    python tools/mock_query_server.py 0.0.0.0 8791

模拟器内把「查询服务器」设为 http://127.0.0.1:8791 并执行
    adb reverse tcp:8791 tcp:8791
登录：任意 5~20 位 QQ 号 + 激活码 TESTCODE（也可用 /query-api/admin/issue 签发）。
仅供本地测试，不实现限流 / TLS / 写权限门禁的完整语义。
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

TOKEN_TTL = 24 * 3600
REFRESH_TTL = 180 * 24 * 3600
CODE_TTL = 24 * 3600

_lock = threading.Lock()
_accounts = {}      # qq -> {"code":..., "code_exp":..., "role":..., "writes":...}
_sessions = {}      # access -> {"qq":..., "refresh":..., "exp":...}
_refresh_index = {} # refresh -> access
_subs = {}          # qq -> [ {player, rank_top, notify} ]

# 429 模拟：MOCK_429_FIRST=N 时，服务启动后的前 N 条 /query-api 请求返回 429 query_busy，
# 用于验证安卓端的串行闸门 + 3 秒重试链路（设 0 关闭）。
_429_FIRST = int(os.environ.get("MOCK_429_FIRST", "0"))
_busy_left = _429_FIRST


def _busy_gate():
    """返回 True 表示本次请求应模拟繁忙。"""
    global _busy_left
    with _lock:
        if _busy_left > 0:
            _busy_left -= 1
            return True
    return False

PEOPLE = [
    {"player": "alice", "rank": 1, "points": 2018.5, "observed": True,
     "deck_type": "蛇眼", "series": "SSE"},
    {"player": "bob", "rank": 2, "points": 1903.0, "observed": True,
     "deck_type": "尤贝尔", "series": None},
    {"player": "carol", "rank": 3, "points": 1850.25, "observed": False},
]

ROSTER = {
    "generated_at": int(time.time()),
    "current_players": 100, "covered_players": 80, "unidentified_players": 20,
    "people": PEOPLE,
    "deck_distribution": [
        {"name": "蛇眼", "players": 12}, {"name": "尤贝尔", "players": 8},
        {"name": "天杯龙", "players": 6}, {"name": "纯爱", "players": 5},
    ],
}

CARD_METRICS = {
    "main": {
        "carrying": {"numerator": 40, "denominator": 80, "percentage": 50.0},
        "scope_win_rate": {"fraction": {"numerator": 55, "denominator": 100,
                                        "percentage": 55.0}},
        "draw_rate": {"numerator": 60, "denominator": 80, "percentage": 75.0},
    },
    "by_deck": [
        {"deck_type": "蛇眼", "metrics": {"carrying": {"numerator": 10, "denominator": 12,
                                                       "percentage": 83.3}}},
        {"deck_type": "尤贝尔", "metrics": {"carrying": {"numerator": 4, "denominator": 8,
                                                       "percentage": 50.0}}},
    ],
}


def ok(data):
    return {"ok": True, "data": data}


def issue_tokens(qq, writes):
    access = "mock-access-%s-%d" % (qq, time.time())
    refresh = "mock-refresh-%s-%d" % (qq, time.time())
    now = int(time.time())
    _sessions[access] = {"qq": qq, "refresh": refresh, "exp": now + TOKEN_TTL,
                         "writes": writes}
    _refresh_index[refresh] = access
    return {
        "ok": True,
        "token": access,
        "refresh_token": refresh,
        "access_expires_at": now + TOKEN_TTL,
        "refresh_expires_at": now + REFRESH_TTL,
        "identity": qq,
        "role": "user",
        "writes": writes,
    }


def check_auth(headers):
    """返回 (access_token, session) 或 None。"""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    with _lock:
        s = _sessions.get(token)
    if not s or s["exp"] < time.time():
        return None
    return token, s


class Handler(BaseHTTPRequestHandler):
    server_version = "MockQuery/1.0"

    def log_message(self, fmt, *args):
        print("[mock] %s %s" % (self.command, self.path))

    # ---------- helpers ----------

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    # ---------- dispatch ----------

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)

        if path != "/query-api/admin/ui" and _busy_gate():
            self.send_json({"ok": False, "error": "query_busy"}, 429)
            return

        if path == "/query-api/admin/ui":
            self.send_json({"ok": True, "hint": "mock admin ui"})
            return
        if path.startswith("/query-api/admin/"):
            self.send_json({"ok": False, "error": "admin_not_supported_in_mock"}, 401)
            return

        if path in ("/query-api/me", "/query-api/status"):
            checked = check_auth(self.headers)
            if not checked:
                self.send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            _, s = checked
            self.send_json({"ok": True, "identity": s["qq"], "role": "user",
                            "writes": s["writes"], "device": "mock-device"})
            return

        if path == "/query-api/top100":
            self._authed(lambda: self.send_json(ok(ROSTER)))
            return

        if path == "/query-api/charts/pie":
            self._authed(lambda: self.send_json(ok({
                "items": [ {"name": d["name"], "count": d["players"]}
                           for d in ROSTER["deck_distribution"] ] + [
                           {"name": "未识别", "count": 20} ],
                "covered": 80, "current": 100, "unidentified": 20,
            })))
            return

        if path == "/query-api/search":
            self._authed(lambda: self._search(q))
            return

        if path.startswith("/query-api/card/"):
            code = path.rsplit("/", 1)[-1]
            self._authed(lambda: self._card(code))
            return

        m = re.match(r"^/query-api/player/([^/]+)/(pt|history|decks|replays)$", path)
        if m:
            name, view = m.group(1), m.group(2)
            self._authed(lambda: self._player(name, view))
            return

        if path == "/query-api/subscriptions":
            checked = check_auth(self.headers)
            if not checked:
                self.send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            self.send_json(ok({"items": _subs.get(checked[1]["qq"], [])}))
            return

        self.send_json({"ok": False, "error": "not_found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        body = self.read_json() or {}

        if _busy_gate():
            self.send_json({"ok": False, "error": "query_busy"}, 429)
            return

        if path == "/query-api/login":
            qq = str(body.get("account", ""))
            secret = str(body.get("secret", ""))
            if not re.fullmatch(r"[0-9]{5,20}", qq):
                self.send_json({"ok": False, "error": "bad_account"}, 400)
                return
            with _lock:
                acc = _accounts.get(qq)
                if acc is None:
                    # mock：首次登录自动开户，码固定 TESTCODE
                    acc = {"code": "TESTCODE", "code_exp": time.time() + CODE_TTL,
                           "writes": True}
                    _accounts[qq] = acc
                if secret != acc["code"]:
                    self.send_json({"ok": False, "error": "bad_secret"}, 400)
                    return
            self.send_json(issue_tokens(qq, acc["writes"]))
            return

        if path == "/query-api/refresh":
            rt = str(body.get("refresh_token", ""))
            with _lock:
                access = _refresh_index.get(rt)
                old = _sessions.get(access) if access else None
            if not old:
                self.send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            self.send_json(issue_tokens(old["qq"], old["writes"]))
            return

        if path == "/query-api/logout":
            checked = check_auth(self.headers)
            if checked:
                token, s = checked
                with _lock:
                    _sessions.pop(token, None)
                    _refresh_index.pop(s["refresh"], None)
            self.send_json({"ok": True})
            return

        if path == "/query-api/subscriptions":
            checked = check_auth(self.headers)
            if not checked:
                self.send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            s = checked[1]
            op = body.get("op")
            player = str(body.get("player", ""))
            if not player:
                self.send_json({"ok": False, "error": "bad_player"}, 400)
                return
            with _lock:
                mine = _subs.setdefault(s["qq"], [])
                if op == "subscribe":
                    item = {"player": player,
                            "rank_top": body.get("rank_top"),
                            "notify": bool(body.get("notify", False))}
                    mine[:] = [x for x in mine if x["player"] != player] + [item]
                    self.send_json({"ok": True, **item})
                elif op == "unsubscribe":
                    mine[:] = [x for x in mine if x["player"] != player]
                    self.send_json({"ok": True, "removed": True, "player": player})
                else:
                    self.send_json({"ok": False, "error": "bad_op"}, 400)
            return

        if path == "/query-api/command":
            self.send_json({"ok": False, "error": "not_implemented"}, 501)
            return

        self.send_json({"ok": False, "error": "not_found"}, 404)

    # ---------- data endpoints ----------

    def _authed(self, fn):
        if not check_auth(self.headers):
            self.send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        fn()

    def _search(self, q):
        stype = (q.get("type") or [""])[0]
        kw = (q.get("q") or [""])[0]
        if stype not in ("player", "card", "deck") or not kw:
            self.send_json({"ok": False, "error": "bad_query"}, 400)
            return
        if stype == "player":
            hits = [p for p in PEOPLE if kw.lower() in p["player"].lower()]
            self.send_json(ok({"people": hits, "limited": len(hits) >= 30}))
        elif stype == "deck":
            hits = [d for d in ROSTER["deck_distribution"] if kw in d["name"]]
            self.send_json(ok({"items": hits, "limited": False}))
        else:
            self.send_json(ok({
                "result": [
                    {"id": 94820406, "cn_name": "原罪宝-蛇眼",
                     "text": {"name": "原罪宝-蛇眼", "types": "魔法/通常",
                              "desc": "mock 卡片效果文本。", "pdesc": ""}},
                ],
                "next": None,
            }))

    def _card(self, code):
        details = {
            "id": code,
            "cn_name": "mock卡-%s" % code,
            "text": {"name": "mock卡-%s" % code, "types": "魔法/通常",
                     "desc": "mock 卡片效果文本。", "pdesc": ""},
        }
        self.send_json(ok({"details": details, "metrics": CARD_METRICS}))

    def _player(self, name, view):
        if view == "pt":
            self.send_json(ok({"points": [
                {"t": 1, "pt": 1500}, {"t": 2, "pt": 1520}, {"t": 3, "pt": 1495},
            ]}))
        elif view == "history":
            self.send_json(ok({"items": [
                {"opponent": "dave", "result": "win", "dp_delta": 15,
                 "time": 1770000000},
                {"opponent": "erin", "result": "lose", "dp_delta": -18,
                 "time": 1770003600},
            ]}))
        elif view == "decks":
            self.send_json(ok({
                "person": PEOPLE[0],
                "cards": [
                    {"partition": "main", "code": 94820406, "copies": 3},
                    {"partition": "extra", "code": 1861629, "copies": 1},
                    {"partition": "side", "code": 54631665, "copies": 2},
                ],
                "completeness": {"main": True, "extra": True, "side": False},
            }))
        else:  # replays
            self.send_json(ok({
                "items": [
                    {"id": "r1", "name": "%s-vs-dave.yrp" % name,
                     "recorded_at": time.time() - 3600,
                     "url": "http://127.0.0.1:%d/file/r1" % self.server.server_address[1]},
                ],
                "link": {"url": "http://127.0.0.1:%d/dir/%s" % (
                    self.server.server_address[1], name),
                    "expires_at": int(time.time()) + 86400, "public": True},
            }))


def main():
    host = "127.0.0.1"
    port = 8791
    import sys
    if len(sys.argv) >= 2:
        host = sys.argv[1]
    if len(sys.argv) >= 3:
        port = int(sys.argv[2])
    srv = ThreadingHTTPServer((host, port), Handler)
    print("mock query api on http://%s:%d (login: any qq + TESTCODE)" % (host, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
