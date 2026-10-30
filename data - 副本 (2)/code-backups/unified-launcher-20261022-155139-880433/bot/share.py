# -*- coding: utf-8 -*-
"""本机录像目录。页面、单个下载和打包下载都检查链接是否还在有效期内。"""

from __future__ import annotations

import html
import io
import re
import secrets
import socket
import threading
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from .store import Store

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
ID_RE = re.compile(r"^[0-9]+$")


def format_time(stamp: float) -> str:
    return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M:%S")


def safe_replay_file(root: Path, rel_path: str) -> Optional[Path]:
    """只允许打开录像根目录里面的普通文件，拒绝绝对路径和越出根目录的路径。"""
    if not rel_path or Path(rel_path).is_absolute():
        return None
    if rel_path.startswith(("/", "\\")):
        return None
    root_resolved = root.resolve()
    candidate = (root_resolved / rel_path).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


class ShareApp:
    def __init__(
        self,
        store: Store,
        replay_root: Path,
        clock,
        public_base_url: str = "",
        bind_host: str = "127.0.0.1",
        bind_port: int = 8787,
        link_hours: float = 5,
    ):
        self.store = store
        self.replay_root = Path(replay_root)
        self.replay_root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.public_base_url = (public_base_url or "").strip().rstrip("/")
        self.bind_host = bind_host or "127.0.0.1"
        self.bind_port = int(bind_port)
        self.link_hours = float(link_hours)

    def local_base(self) -> str:
        host = self.bind_host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.bind_port}"

    def public_base(self) -> str:
        base = self.public_base_url
        if not base:
            return ""
        host = (urlparse(base).hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
            return ""
        return base

    def url_for(self, token: str) -> tuple[str, bool]:
        public = self.public_base()
        if public:
            return public + "/s/" + token, True
        return self.local_base() + "/s/" + token, False

    def create_link(self, user_id: str, player: str, replay_ids: list[int], range_start: float) -> dict:
        now = float(self.clock())
        token = secrets.token_urlsafe(32)
        expires = now + self.link_hours * 3600
        self.store.insert_link(token, user_id, player, now, expires, list(replay_ids), range_start)
        url, public = self.url_for(token)
        return {
            "token": token,
            "url": url,
            "public": public,
            "created_at": now,
            "expires_at": expires,
            "range_start": float(range_start),
            "replay_ids": list(replay_ids),
        }

    def page(self, token: str, kind: str, replay_id: str = "") -> tuple[int, str, bytes]:
        if not TOKEN_RE.fullmatch(token or ""):
            return self._text(404, "找不到这个分享链接。")
        link = self.store.get_link(token)
        if link is None:
            return self._text(404, "找不到这个分享链接。")
        if float(self.clock()) >= float(link["expires_at"]):
            return self._html(
                410,
                "链接已失效",
                "<h1>分享链接已失效</h1>"
                "<p>这条链接从生成时起已经超过有效期。本机录像还在，请向订阅者重新要一条链接。</p>"
                "<p>重新生成的链接是新的，不会把这一条的到期时间往后延。</p>",
            )
        allowed = {int(item) for item in link["replay_ids"]}
        if kind == "file":
            if not ID_RE.fullmatch(replay_id or "") or int(replay_id) not in allowed:
                return self._text(404, "这个链接不能下载这份录像。")
            replay = self.store.get_replay(int(replay_id))
            path = safe_replay_file(self.replay_root, replay["rel_path"]) if replay else None
            if path is None:
                return self._text(404, "录像文件不在分享目录里。")
            return 200, "application/octet-stream", path.read_bytes()
        if kind == "zip":
            buffer = io.BytesIO()
            used: set[str] = set()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for item in link["replay_ids"]:
                    replay = self.store.get_replay(int(item))
                    path = safe_replay_file(self.replay_root, replay["rel_path"]) if replay else None
                    if path is None:
                        continue
                    name = path.name
                    if name in used:
                        name = f"{path.stem}-{item}{path.suffix}"
                    used.add(name)
                    archive.write(path, arcname=name)
            return 200, "application/zip", buffer.getvalue()
        return self._html(200, "录像目录", self._directory_body(link))

    def _directory_body(self, link: dict) -> str:
        rows = []
        for item in link["replay_ids"]:
            replay = self.store.get_replay(int(item))
            if not replay:
                continue
            path = safe_replay_file(self.replay_root, replay["rel_path"])
            label = html.escape(Path(replay["rel_path"]).name)
            when = html.escape(format_time(replay["recorded_at"]))
            if path is None:
                rows.append(f"<li>{label}（{when}）文件已不在目录中</li>")
                continue
            size = path.stat().st_size
            token = html.escape(link["token"])
            rows.append(
                f'<li><a href="/s/{token}/file/{int(item)}">{label}</a>'
                f"（{when}，{size} 字节）</li>"
            )
        listing = "\n".join(rows) or "<li>生成链接时还没有符合条件的录像。</li>"
        player = html.escape(link["player"])
        token = html.escape(link["token"])
        return (
            f"<h1>{player} 的观战录像</h1>"
            f"<p>收录范围：{html.escape(format_time(link['range_start']))}"
            f" 至 {html.escape(format_time(link['created_at']))} 已经保存、并且符合该订阅条件的录像。</p>"
            f"<p>链接到期：{html.escape(format_time(link['expires_at']))}。"
            "到期后不能再打开或下载，本机文件不会因此删除。之后再查询会得到一条新链接。</p>"
            f'<p><a href="/s/{token}/zip">打包下载</a></p><ul>{listing}</ul>'
        )

    def _html(self, status: int, title: str, body: str) -> tuple[int, str, bytes]:
        page = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title></head><body>{body}</body></html>"
        )
        return status, "text/html; charset=utf-8", page.encode("utf-8")

    def _text(self, status: int, message: str) -> tuple[int, str, bytes]:
        return self._html(status, "无法访问", f"<h1>{html.escape(message)}</h1>")


def make_handler(app: ShareApp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = unquote(self.path.split("?", 1)[0])
            parts = [part for part in path.split("/") if part]
            status, content_type, body = self._route(parts)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if not content_type.startswith("text/html"):
                self.send_header("Content-Disposition", "attachment")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _route(self, parts: list[str]) -> tuple[int, str, bytes]:
            if len(parts) == 2 and parts[0] == "s":
                return app.page(parts[1], "dir")
            if len(parts) == 3 and parts[0] == "s" and parts[2] == "zip":
                return app.page(parts[1], "zip")
            if len(parts) == 4 and parts[0] == "s" and parts[2] == "file":
                return app.page(parts[1], "file", parts[3])
            return app._text(404, "找不到这个分享链接。")

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


class ReplayHTTPServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 会允许两个进程抢同一端口，导致链接随机落到另一个实例。
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ShareServer:
    def __init__(self, app: ShareApp):
        self.app = app
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        handler = make_handler(self.app)
        self.httpd = ReplayHTTPServer((self.app.bind_host, self.app.bind_port), handler)
        self.app.bind_port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="replay-share", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
