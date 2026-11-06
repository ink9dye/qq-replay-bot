# -*- coding: utf-8 -*-
"""门户 busy 识别、负缓存与 clamp。"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.query_core import PortalBusy, PortalClient, PortalUnavailable, QueryCore


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _send(self, status, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/statistics/api/busy"):
            self._send(503, json.dumps({"ok": False, "error": "query_busy", "code": "query_busy", "retry_after": 4}).encode())
        elif self.path.startswith("/statistics/api/clamp"):
            self._send(503, json.dumps({"ok": False, "code": "query_busy", "retry_after": 99}).encode())
        elif self.path.startswith("/statistics/api/html"):
            self._send(503, b"<html>503</html>")
        elif self.path.startswith("/statistics/api/ok"):
            self._send(200, b'{"x": 1}')
        else:
            self._send(404, b"{}")


class _Portal(PortalClient):
    def _session_cookie(self, force: bool = False) -> str:
        return "test=1"


class PortalBusyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_busy_json_raises_portal_busy(self):
        portal = _Portal(self.base, secret_path=None, timeout=2)
        with self.assertRaises(PortalBusy) as ctx:
            portal.get_json("/statistics/api/busy", {"player": "甲"})
        self.assertEqual(ctx.exception.retry_after, 4)

    def test_busy_retry_after_clamped(self):
        portal = _Portal(self.base, secret_path=None, timeout=2)
        with self.assertRaises(PortalBusy) as ctx:
            portal.get_json("/statistics/api/clamp")
        self.assertEqual(ctx.exception.retry_after, 5)

    def test_html_503_is_unavailable(self):
        portal = _Portal(self.base, secret_path=None, timeout=2)
        with self.assertRaises(PortalUnavailable) as ctx:
            portal.get_json("/statistics/api/html")
        self.assertNotIsInstance(ctx.exception, PortalBusy)

    def test_ok_passthrough(self):
        portal = _Portal(self.base, secret_path=None, timeout=2)
        self.assertEqual(portal.get_json("/statistics/api/ok"), {"x": 1})

    def test_negative_cache_prevents_hammering(self):
        core = QueryCore(_Portal(self.base, secret_path=None, timeout=2))
        calls = []

        def work():
            calls.append(1)
            raise PortalBusy(3)

        for _ in range(3):
            with self.assertRaises(PortalBusy):
                core._cached(("neg",), 10.0, work)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
