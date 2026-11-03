from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import watch

from app.client import Timeouts, WatchClient
from app.config import AccountConfig, AppConfig


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _send(self, status: int, payload: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/watch-api/status":
            self._send(200, {"ok": True, "workers": {"tester": {"online": True}},
                             "pending": 0, "claimed": 1, "ip_cap": 6})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        state = self.server.owner
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/watch-api/claim":
            with state["lock"]:
                job = state["jobs"].pop(0) if state["jobs"] else None
            if job:
                self._send(200, {"ok": True, "job": job})
            else:
                self._send(204)
        elif self.path == "/watch-api/heartbeat":
            self._send(200, {"ok": True})
        elif self.path == "/watch-api/report":
            with state["lock"]:
                state["reports"].append(json.loads(body.decode()))
            self._send(200, {"ok": True})
        elif self.path.startswith("/watch-api/upload"):
            with state["lock"]:
                state["uploads"].append({
                    "query": self.path,
                    "players": self.headers.get("X-Players"),
                    "body": body,
                })
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False})


class _FakeWriter:
    def __init__(self, directory, targets=None, mode="exact"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.on_saved = None
        self.last_written = []

    def for_room(self, _players):
        return self

    def save(self, _payload, players, whens=None):
        path = self.directory / "test-game.yrp"
        path.write_bytes(b"YRP1" + b"x" * 32)
        sidecar = Path(str(path) + ".evidence.json")
        sidecar.write_text("{}", encoding="utf-8")
        self.last_written = [path]
        if self.on_saved:
            self.on_saved([path], list(players), datetime.now(), "digest-1")
        return [path]


class _FakeSpectator:
    def __init__(self, config, room, writer):
        self.config = config
        self.room = room
        self.writer = writer
        self.packets_received = 3
        self.duel_started = True
        self.duel_ended = True
        self.replays_saved = 0

    async def run(self):
        self.writer.save(b"payload", self.room.players)
        self.replays_saved = 1
        return "saved"

    async def close(self):
        return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _job(job_id: str, key: str, title: str, players: tuple[str, str] = ("甲", "乙")) -> dict:
    return {
        "id": job_id,
        "room": {"key": key, "title": title, "join_id": key,
                 "players": list(players), "seats": {"0": players[0], "1": players[1]},
                 "started": True},
        "server": {"host": "127.0.0.1", "port": 1, "version": 5463},
        "focus": [players[0]],
    }


class ClientFlowTests(unittest.TestCase):
    def setUp(self):
        self.state = {"jobs": [_job("job-1", "M#1", "测试房间")],
                      "uploads": [], "reports": [], "lock": threading.Lock()}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.owner = self.state
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.events: list[tuple[str, dict]] = []
        self._patches = [
            patch.object(watch, "sign_in", side_effect=lambda account, _password: (123456, account)),
            patch.object(watch, "ReplayWriter", _FakeWriter),
            patch.object(watch, "Spectator", _FakeSpectator),
        ]
        for item in self._patches:
            item.start()
        self.client = None

    def tearDown(self):
        if self.client:
            self.client.stop()
            self.client.wait(2)
        for item in reversed(self._patches):
            item.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _emit(self, kind, data):
        self.events.append((kind, data))

    def test_claim_spectate_upload_evidence_and_report(self):
        port = self.httpd.server_address[1]
        cfg = AppConfig(server=f"http://127.0.0.1:{port}", token="token-123",
                        name="tester", accounts=[AccountConfig("acct", "pw")])
        self.client = WatchClient(cfg, self.data_dir, emit=self._emit,
                                  timeouts=Timeouts(request=5, claim_wait=0.2,
                                                    heartbeat=30, join_interval=0,
                                                    join_jitter=0))
        self.assertTrue(self.client.start())
        deadline = time.time() + 8
        while time.time() < deadline:
            with self.state["lock"]:
                reports = list(self.state["reports"])
                uploads = list(self.state["uploads"])
            if reports and len(uploads) >= 2:
                break
            time.sleep(0.05)
        self.client.stop()
        self.client.wait(3)

        with self.state["lock"]:
            uploads = list(self.state["uploads"])
            reports = list(self.state["reports"])
        self.assertEqual(len(uploads), 2, uploads)
        self.assertTrue(any("job-1" in item["query"] for item in uploads))
        self.assertTrue(any("evidence.json" in item["query"] for item in uploads))
        self.assertTrue(all(item["body"] for item in uploads))
        self.assertEqual(reports[0]["outcome"], "saved")
        self.assertEqual(reports[0]["detail"]["saved"], 1)
        self.assertEqual(reports[0]["detail"]["uploaded"], 1)
        self.assertFalse((self.data_dir / "work" / "job-1").exists())

        done = [data for kind, data in self.events if kind == "jobs" and data.get("jobs")]
        self.assertTrue(done)
        last_job = done[-1]["jobs"][0]
        self.assertEqual(last_job["status"], "已完成")
        self.assertEqual(last_job["saved"], 1)
        self.assertEqual(last_job["uploaded"], 1)

    def test_two_accounts_record_two_rooms_at_the_same_time(self):
        """两个账号各看一间：两间房都要录到，且用的是不同账号。"""
        port = self.httpd.server_address[1]
        with self.state["lock"]:
            self.state["jobs"] = [_job("job-a", "M#A", "房间A", ("甲", "乙")),
                                  _job("job-b", "M#B", "房间B", ("丙", "丁"))]
        cfg = AppConfig(server=f"http://127.0.0.1:{port}", token="token-123", name="tester",
                        accounts=[AccountConfig("acct-1", "pw"), AccountConfig("acct-2", "pw")])
        self.assertEqual(cfg.capacity, 2)
        self.client = WatchClient(cfg, self.data_dir, emit=self._emit,
                                  timeouts=Timeouts(request=5, claim_wait=0.2,
                                                    heartbeat=30, join_interval=0,
                                                    join_jitter=0))
        self.assertTrue(self.client.start())
        deadline = time.time() + 12
        while time.time() < deadline:
            with self.state["lock"]:
                reports = list(self.state["reports"])
                uploads = list(self.state["uploads"])
            if len(reports) >= 2 and len(uploads) >= 4:
                break
            time.sleep(0.05)
        self.client.stop()
        self.assertTrue(self.client.wait(5))

        with self.state["lock"]:
            reports = list(self.state["reports"])
            uploads = list(self.state["uploads"])
        self.assertEqual(len(reports), 2, reports)
        self.assertEqual(sorted(str(item["job"]) for item in reports), ["job-a", "job-b"])
        self.assertTrue(all(item["outcome"] == "saved" for item in reports), reports)
        self.assertEqual(len(uploads), 4, uploads)
        self.assertFalse((self.data_dir / "work" / "job-a").exists())
        self.assertFalse((self.data_dir / "work" / "job-b").exists())

        # 两间房必须落在两个不同账号上，不能挤同一个账号
        accounts: dict[str, str] = {}
        for kind, data in self.events:
            if kind != "jobs":
                continue
            for job in data.get("jobs") or []:
                if job.get("stage") == "done":
                    accounts[job["id"]] = str(job.get("account") or "")
        self.assertEqual(set(accounts), {"job-a", "job-b"})
        self.assertEqual(set(accounts.values()), {"acct-1", "acct-2"})

    def test_test_connection_rejects_bad_token(self):
        # 当前假服务器不校验 token；这里验证成功分支的字段解析。
        port = self.httpd.server_address[1]
        result = WatchClient.test_connection(f"127.0.0.1:{port}", "token")
        self.assertTrue(result["ok"])
        self.assertEqual(result["online_workers"], 1)


if __name__ == "__main__":
    unittest.main()
