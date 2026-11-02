# -*- coding: utf-8 -*-
"""本机录像目录。页面、单个下载和打包下载都检查链接是否还在有效期内。"""

from __future__ import annotations

import html
import io
import json
import re
import secrets
import socket
import sys
import threading
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlparse

from .store import Store
from .replay_window import replay_in_window
from .deck_links import observed_deck, ourygo_url, observed_title, arena_points, relative_age

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
ID_RE = re.compile(r"^[0-9]+$")


def attachment_header(filename: str, fallback: str) -> str:
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', filename)
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def format_time(stamp: float) -> str:
    return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M:%S")


def safe_replay_file(root: Path, rel_path: str, root_resolved: Path = None, dir_cache: dict = None) -> Optional[Path]:
    """只允许打开录像根目录里面的普通文件，拒绝绝对路径和越出根目录的路径。

    root_resolved/dir_cache 可由同一请求复用，避免每个视角重复解析根目录；
    父目录仍 resolve 并做包含校验，文件符号链接仍单独 resolve。
    """
    if not rel_path or Path(rel_path).is_absolute():
        return None
    if rel_path.startswith(("/", "\\")):
        return None
    root_resolved = root_resolved or root.resolve()
    if ".." not in Path(rel_path).parts:
        candidate = root_resolved / rel_path
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except OSError:
            return None
    candidate = root_resolved / rel_path
    if dir_cache is None:
        candidate = candidate.resolve()
    else:
        parent_key = str(candidate.parent)
        parent = dir_cache.get(parent_key)
        if parent is None:
            parent = candidate.parent.resolve()
            try:
                parent.relative_to(root_resolved)
            except ValueError:
                return None
            dir_cache[parent_key] = parent
        candidate = parent / candidate.name
        if candidate.is_symlink():
            candidate = candidate.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


class ShareLinkUnavailable(RuntimeError):
    """链接生成失败（数据库错误/重试上限）；上层应明确报错而不是发死链。"""


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
        self.public_url_provider = None
        self.statistics_portal = None
        # 同一分享范围优先复用未过期链接；失败有限重试，避免把死链发出去。
        self.link_reuse_margin_seconds = 600.0
        self.link_attempts = 3
        self._link_lock = threading.Lock()
        # /s/<token>/analysis-refresh 是 GET 触发写（重解析 + sidecar），独立 share 进程必须关掉它。
        self.allow_analysis_refresh = True

    def local_base(self) -> str:
        host = self.bind_host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.bind_port}"

    def public_base(self) -> str:
        base = self.public_url_provider() if self.public_url_provider else self.public_base_url
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

    def _link_payload(self, link: dict) -> dict:
        url, public = self.url_for(link['token'])
        return {
            "token": link['token'], "url": url, "public": public,
            "created_at": link['created_at'], "expires_at": link['expires_at'],
            "range_start": float(link['range_start']), "replay_ids": list(link['replay_ids']),
            "reused": bool(link.get('reused')),
        }

    def create_link(self, user_id: str, player: str, replay_ids: list[int], range_start: float,
                    *, deck_replay_ids: Optional[list[int]] = None) -> dict:
        ids = list(replay_ids)
        deck_ids = list(ids if deck_replay_ids is None else deck_replay_ids)
        now = float(self.clock())
        with self._link_lock:
            reusable = self.store.find_reusable_link(
                user_id, player, ids, float(range_start), deck_ids,
                now + self.link_reuse_margin_seconds)
            if reusable:
                reusable['reused'] = True
                return self._link_payload(reusable)
            last_error = None
            for attempt in range(self.link_attempts):
                token = secrets.token_urlsafe(32)
                expires = now + self.link_hours * 3600
                try:
                    self.store.insert_link(token, user_id, player, now, expires, ids, range_start, deck_ids)
                    return self._link_payload({
                        "token": token, "created_at": now, "expires_at": expires,
                        "range_start": range_start, "replay_ids": ids, "reused": False})
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < self.link_attempts:
                        time.sleep(0.05 * (attempt + 1))
            raise ShareLinkUnavailable('分享链接生成失败，请稍后再试。') from last_error

    def create_stats_link(self, user_id: str, hours: float = 2) -> dict:
        """只读统计页的临时入口；与录像链接分开表和有效期。"""
        now = float(self.clock())
        with self._link_lock:
            reusable = self.store.find_reusable_stats_link(user_id, now + self.link_reuse_margin_seconds)
            if reusable:
                public = self.public_base()
                base = public or self.local_base()
                return {"token": reusable['token'], "url": base + "/stats/" + reusable['token'],
                        "public": bool(public), "created_at": reusable['created_at'],
                        "expires_at": reusable['expires_at'], "reused": True}
            last_error = None
            for attempt in range(self.link_attempts):
                token = secrets.token_urlsafe(32)
                expires = now + max(0.1, float(hours)) * 3600
                try:
                    self.store.insert_stats_link(token, user_id, now, expires)
                    self.store.purge_stats_links(now)
                    public = self.public_base()
                    base = public or self.local_base()
                    return {"token": token, "url": base + "/stats/" + token,
                            "public": bool(public), "created_at": now, "expires_at": expires,
                            "reused": False}
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < self.link_attempts:
                        time.sleep(0.05 * (attempt + 1))
            raise ShareLinkUnavailable('统计链接生成失败，请稍后再试。') from last_error

    def page(self, token: str, kind: str, replay_id: str = "") -> tuple[int, str, bytes]:
        if not TOKEN_RE.fullmatch(token or ""):
            return self._text(404, "找不到这个分享链接。")
        link = self.store.get_link(token)
        if link is None:
            return self._text(404, "找不到这个分享链接。")
        if link.get("revoked_at"):
            return self._html(410, "链接已撤销", "<h1>分享链接已被撤销</h1>"
                                     "<p>这条链接不能再访问；请向分享者重新要一条。</p>")
        if float(self.clock()) >= float(link["expires_at"]):
            return self._html(
                410,
                "链接已失效",
                "<h1>分享链接已失效</h1>"
                "<p>这条链接从生成时起已经超过有效期。本机录像还在，请向订阅者重新要一条链接。</p>"
                "<p>重新生成的链接是新的，不会把这一条的到期时间往后延。</p>",
            )
        allowed = {int(item) for item in link["replay_ids"]}
        if kind == 'profile':
            from .arena_profile import arena_points
            score = arena_points(link['player'], self.replay_root.parent / 'cache')
            payload = {'score': float(score) if score is not None else None,
                       'rank': getattr(score, 'rank', None),
                       'updated_at': getattr(score, 'updated_at', None),
                       'stale': getattr(score, 'stale', False)}
            return 200, 'application/json; charset=utf-8', json.dumps(payload).encode('utf8')
        if kind in {"file", "analysis", "analysis-json", "analysis-refresh"}:
            if kind == 'analysis-refresh' and not getattr(self, 'allow_analysis_refresh', True):
                # 重新解析会读 .yrp、写 analysis/evidence sidecar（GET 触发写），必须留在 watch 进程；
                # 独立 share 进程只提供读缓存的 analysis/analysis-json。
                return self._text(409, "重新解析需要观战（watch）进程在线；独立分享服务不提供这个操作。")
            if not ID_RE.fullmatch(replay_id or "") or int(replay_id) not in allowed:
                return self._text(404, "这个链接不能下载这份录像。")
            replay = self.store.get_replay(int(replay_id))
            if replay is not None and replay['focus_player'] != link['player']:
                return self._text(404, "这个链接不能下载其他玩家的录像。")
            if replay is not None and not replay_in_window(replay, float(self.clock())):
                return self._text(410, "这份录像已保存满14天，不再提供下载，关联文件按保留策略清理。")
            path = safe_replay_file(self.replay_root, replay["rel_path"]) if replay else None
            if path is None:
                return self._text(404, "录像文件不在分享目录里。")
            if kind != 'file':
                from .replay_analysis_ui import request_report, render_report, match_context
                from .watch_core import YGOPRO_DIR
                report = request_report(path, YGOPRO_DIR, match_context(self, link, replay), force=kind == 'analysis-refresh')
                if kind == 'analysis-refresh':
                    return self._html(200, '重新解析', f'<h1>正在重新解析</h1><p>正在更新本小局及公开对战记录。</p><script>setTimeout(()=>location.replace("/s/{token}/analysis/{int(replay_id)}"),2000)</script>')
                if kind == 'analysis-json':
                    payload = report if report is not None else {'status': 'pending'}
                    return (200 if report is not None else 202), 'application/json; charset=utf-8', json.dumps(payload, ensure_ascii=False).encode('utf-8')
                return self._html(200, '单小局解析', render_report(report, token, int(replay_id)))
            return 200, "application/octet-stream", path.read_bytes()
        if kind == "zip":
            from .statistics_recording_names import catalog
            buffer = io.BytesIO()
            used: set[str] = set()
            catalogs: dict[str, dict] = {}

            def folder_for(replay: dict) -> str:
                player = link['player']
                if player not in catalogs:
                    try:
                        catalogs[player] = catalog(self.store._db, player)
                    except Exception:
                        catalogs[player] = {}
                digest = None
                try:
                    row = self.store._db.execute(
                        'SELECT digest FROM stat_files WHERE replay_id=? LIMIT 1', (replay['id'],)).fetchone()
                    digest = row[0] if row else None
                except Exception:
                    digest = None
                info = catalogs[player].get(digest) if digest else None
                if info and info.get('observed_date'):
                    return f"{info['observed_date']} 第{info.get('observed_match_no') or '?'}场"
                stamp = replay.get('recorded_at') or replay.get('saved_at')
                try:
                    return datetime.fromtimestamp(float(stamp)).strftime('%Y-%m-%d')
                except (TypeError, ValueError, OSError):
                    return ''

            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for item in link["replay_ids"]:
                    replay = self.store.get_replay(int(item))
                    if replay is not None and (replay['focus_player'] != link['player']
                                               or not replay_in_window(replay, float(self.clock()))):
                        continue
                    path = safe_replay_file(self.replay_root, replay["rel_path"]) if replay else None
                    if path is None:
                        continue
                    name = self.download_name(replay)
                    folder = folder_for(replay)
                    arcname = f"{folder}/{name}" if folder else name
                    if arcname in used:
                        fallback = f"{path.stem}-{item}{path.suffix}"
                        arcname = f"{folder}/{fallback}" if folder else fallback
                        suffix = 2
                        while arcname in used:
                            fallback = f"{path.stem}-{item}-{suffix}{path.suffix}"
                            arcname = f"{folder}/{fallback}" if folder else fallback
                            suffix += 1
                    used.add(arcname)
                    archive.write(path, arcname=arcname)
            return 200, "application/zip", buffer.getvalue()
        return self._html(200, "录像目录", self._directory_body(link))

    def download_name(self, replay: dict) -> str:
        path = safe_replay_file(self.replay_root, replay['rel_path'])
        if path is None:
            return Path(replay['rel_path']).name
        from .deck_links import matchup_labels
        return matchup_labels(replay, path, self.replay_root.parent / 'cache')[1]

    def _directory_body(self, link: dict) -> str:
        from .page_ui import directory_body
        return directory_body(self, link, observed_title, arena_points)

    def deck_observations(self, link: dict) -> list:
        available = []
        for identifier in link.get('deck_replay_ids', link['replay_ids']):
            replay = self.store.get_replay(int(identifier))
            if (not replay or replay['focus_player'] != link['player']
                    or not replay_in_window(replay, float(self.clock()))):
                continue
            path = safe_replay_file(self.replay_root, replay['rel_path'])
            if path:
                available.append((replay, path))
        return available

    def _html(self, status: int, title: str, body: str) -> tuple[int, str, bytes]:
        from .page_ui import PAGE_CSS
        page = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{PAGE_CSS}</style></head><body><main class="page">{body}</main></body></html>' 
        )
        return status, "text/html; charset=utf-8", page.encode("utf-8")

    def _text(self, status: int, message: str) -> tuple[int, str, bytes]:
        return self._html(status, "无法访问", f"<h1>{html.escape(message)}</h1>")


def make_handler(app: ShareApp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if self.path.split('?',1)[0].split('/',2)[1:2] == ['statistics'] and app.statistics_portal is not None:
                status, content_type, body, headers = app.statistics_portal.route(self.path,self.headers,self.client_address[0])
                self.send_response(status)
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(body)))
                for key,value in headers.items():self.send_header(key,value)
                self.end_headers();self.wfile.write(body)
                return
            path = unquote(self.path.split("?", 1)[0])
            parts = [part for part in path.split("/") if part]
            result=self._route(parts)
            status,content_type,body=result[:3]
            extra=result[3] if len(result)==4 else {}
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if 'Location' in extra:
                self.send_header('Location', extra['Location'])
            if 'Content-Disposition' in extra:
                self.send_header('Content-Disposition',extra['Content-Disposition'])
            elif status == 200 and content_type == "application/octet-stream":
                replay = app.store.get_replay(int(parts[3]))
                filename = app.download_name(replay)
                self.send_header("Content-Disposition", attachment_header(filename, f"replay-{int(parts[3])}.yrp"))
            elif status == 200 and content_type == "application/zip":
                link = app.store.get_link(parts[1])
                self.send_header("Content-Disposition", attachment_header(link["player"] + " 观战录像.zip", "replays.zip"))
            self.send_header("Cache-Control", "no-store")
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 8192:
                self.send_response(413 if length > 8192 else 400)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body = self.rfile.read(length)
            path = unquote(self.path.split('?', 1)[0])
            parts = [part for part in path.split('/') if part]
            if len(parts) >= 2 and parts[0] == 'stats':
                from .statistics_proxy import post as stats_post
                status, content_type, payload = stats_post(app, self.path, parts, body)
            else:
                status, content_type, payload = 403, 'text/plain; charset=utf-8', '不支持的操作。'.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(payload)

        def _route(self, parts: list[str]) -> tuple[int, str, bytes]:
            if len(parts) >= 2 and parts[0] == 'stats':
                from .statistics_proxy import route as stats_route
                return stats_route(app, self.path, parts)
            if len(parts)>=2 and parts[0]=='s':
                from .share_build import route
                result=route(app,self.path,parts)
                if result is not None:return result
            if parts == ["health"]:
                from retention_policy import RETENTION_DAYS
                payload = {'service':'qq-replay-bot','status':'running',
                           'retention_days': RETENTION_DAYS}
                payload.update(getattr(app, 'watch_runtime', {}))
                if app.statistics_portal is not None:payload['statistics_portal']=True
                return 200, "application/json; charset=utf-8", json.dumps(payload).encode('utf-8')
            if len(parts) == 2 and parts[0] == "s":
                return app.page(parts[1], "dir")
            if len(parts) == 3 and parts[0] == "s" and parts[2] == "zip":
                return app.page(parts[1], "zip")
            if len(parts) == 3 and parts[0] == "s" and parts[2] == "profile":
                return app.page(parts[1], "profile")
            if len(parts) == 4 and parts[0] == "s" and parts[2] == "file":
                return app.page(parts[1], "file", parts[3])
            if len(parts) == 4 and parts[0] == "s" and parts[2] in {'analysis', 'analysis-json', 'analysis-refresh'}:
                return app.page(parts[1], parts[2], parts[3])
            if not parts:
                return app._html(
                    200,
                    "分享服务",
                    "<h1>这是分享服务入口</h1>"
                    "<p>根路径本身没有分享数据。请打开机器人消息里的完整链接，通常形如：</p>"
                    "<p><code>https://你的域名/s/&lt;token&gt;</code></p>"
                    "<p>或统计页：<code>https://你的域名/stats/&lt;token&gt;</code></p>",
                )
            if parts == ['s'] or parts == ['stats']:
                return app._html(
                    200,
                    "分享服务",
                    "<h1>缺少分享 token</h1><p>请从机器人消息里复制完整链接，不要只打开 /s/ 或 /stats/。</p>",
                )
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

    def handle_error(self, request, client_address) -> None:
        """客户端主动断开（隧道重连/健康检查/浏览器提前关）只静音，真正的问题照常打印。"""
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


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
