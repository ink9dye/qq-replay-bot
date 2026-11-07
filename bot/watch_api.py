"""远程观战 HTTP 接口（给跑在其他公网 IP 上的小程序用）。

只监听回环，由 8787 的 nginx 反代到公网；所有请求都要带
`Authorization: Bearer <token>`。token 可以是 config.json 的 remote.token
（兼容总令牌），也可以是 QQ 机器人签发、存在 SQLite watch_tokens 里的每设备令牌。

接口（详见 docs/REMOTE_WATCH.md）：
    POST /watch-api/claim      {"worker","capacity","ip"} -> {"job":{...}} 或 204
    POST /watch-api/heartbeat  {"worker","active":[job-id]} -> {"ok":true}
    POST /watch-api/report     {"worker","job","outcome","detail"} -> {"ok":true}
    POST /watch-api/upload?job=<id>&name=<file.yrp>  （body 为原始 yrp 字节）
    GET  /watch-api/status     -> 任务板/在线端快照
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from .watch_task_state import AuthorizationExpired
from .query_api import QueryHandlerMixin
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

MAX_BODY = 8 * 1024 * 1024          # 控制请求体上限
MAX_UPLOAD = 4 * 1024 * 1024        # 单个录像上限
# 只替换文件系统不接受/控制类字符；@、假名、其他文字的玩家名保持原样。
# 旧的上传命名（白名单 [0-9A-Za-z._-()汉字空格]）已停用，旧目录由查询端兼容。
_SAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_FILE_LIMIT = 60                    # 字符数；CJK 3 字节，留出 .yrp.evidence.json 的余量


def _safe_part(text: str, fallback: str = "unknown", limit: int = _FILE_LIMIT) -> str:
    cleaned = _SAFE_NAME.sub("_", str(text or "")).strip().rstrip(".")
    return cleaned[:limit] or fallback


def _folder_part(text: str) -> str:
    """录像子目录名：与本地保存的 watch.safe_filename_part 完全一致（40 字）。"""
    return _safe_part(text, limit=40)


class InsufficientStorageError(OSError):
    pass


class _Handler(QueryHandlerMixin, BaseHTTPRequestHandler):
    server_version = "ygobot-watch/1"
    protocol_version = "HTTP/1.1"

    # ---- 基础 ----------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: D401 - 走自己的日志
        return

    def _send(self, status: int, payload=None, headers=None) -> None:
        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(str(name), str(value))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _auth(self) -> bool:
        # 旧版 remote.token 是兼容总令牌；每设备令牌从 SQLite 哈希表验证。
        self.auth_worker = ""
        self.auth = None
        token = getattr(self.server, "app_token", "")
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            got = header[7:].strip()
        else:
            got = (self.headers.get("X-Watch-Token") or "").strip()
        if token and got and hmac.compare_digest(got, token):
            self.auth = dict(id=None, worker='', generation=self.server.app.lifecycle.legacy_generation, created_at=0)
            return True
        store = getattr(self.server, "token_store", None)
        if store is not None and got:
            try:
                row = store.verify(got)
            except Exception as exc:  # 令牌库坏了不能拖垮旧令牌
                app = getattr(self.server, "app", None)
                if app is not None and not getattr(app, "_token_store_error_logged", False):
                    app._token_store_error_logged = True
                    app.log(f"[远程] 每设备令牌库校验失败：{type(exc).__name__}")
                row = None
            if row and str(row.get("worker") or "").strip():
                self.auth_worker = str(row.get("worker") or "")
                self.auth = dict(row, generation='device:' + str(row['id']))
                return True
        self.close_connection = True
        self._send(401, dict(ok=False, error="unauthorized"))
        return False

    def _worker_allowed(self, worker: str) -> bool:
        """总令牌不限制 worker；每设备令牌必须和客户端名称完全一致。"""
        bound = getattr(self, "auth_worker", "")
        return not bound or hmac.compare_digest(str(worker), bound)

    def _json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---- 路由 ----------------------------------------------------------
    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path.startswith("/query-api/"):
            return self._query_dispatch(parts, post=False)
        if not self._auth():
            return
        parts = urlsplit(self.path)
        if parts.path == "/watch-api/status":
            self._send(200, dict(ok=True, **self.server.app.snapshot()))
            return
        self._send(404, dict(ok=False, error="not_found"))

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path.startswith("/query-api/"):
            return self._query_dispatch(parts, post=True)
        if not self._auth():
            return
        try:
            self._post_authorized()
        except AuthorizationExpired:
            self._send(401, dict(ok=False, error='unauthorized'))
        except PermissionError:
            self._send(403, dict(ok=False, error='job_owner_mismatch'))
        except (TimeoutError, sqlite3.Error):
            self._send(503, dict(ok=False, error='task_state_busy'))
        except ValueError:
            self._send(400, dict(ok=False, error='invalid_task_request'))

    def _post_authorized(self):
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/watch-api/claim":
            data = self._json()
            worker = str(data.get("worker") or "").strip()[:64]
            if not worker:
                self._send(400, dict(ok=False, error="worker_required"))
                return
            if not self._worker_allowed(worker):
                self._send(403, dict(ok=False, error="worker_mismatch"))
                return
            # 并发上限按“公网 IP”算：nginx 会带 X-Real-IP；云端客户端直连 8791 时是 127.0.0.1。
            # 不用请求体里的 ip，避免客户端伪造。
            client_ip = (self.headers.get("X-Real-IP") or "").strip() or (self.client_address[0] if self.client_address else "")
            job = self.server.app.lifecycle.claim(worker, int(data.get("capacity") or 1), client_ip,
                                                  float(data.get("wait") or 0), self.auth)
            if not job:
                self._send(204, None)
            else:
                self._send(200, dict(ok=True, job=job))
            return
        if path == "/watch-api/heartbeat":
            data = self._json()
            worker = str(data.get("worker") or "").strip()[:64]
            if not worker:
                self._send(400, dict(ok=False, error="worker_required"))
                return
            if not self._worker_allowed(worker):
                self._send(403, dict(ok=False, error="worker_mismatch"))
                return
            active = data.get("active")
            active = [str(item) for item in active] if isinstance(active, list) else None
            client_ip = (self.headers.get("X-Real-IP") or "").strip() or (self.client_address[0] if self.client_address else "")
            cancel = self.server.app.lifecycle.heartbeat(worker, active, client_ip, self.auth)
            self._send(200, dict(ok=True, cancel=cancel))
            return
        if path == "/watch-api/report":
            data = self._json()
            worker = str(data.get("worker") or "").strip()[:64]
            job = str(data.get("job") or "").strip()
            if not worker or not job:
                self._send(400, dict(ok=False, error="worker_and_job_required"))
                return
            if not self._worker_allowed(worker):
                self._send(403, dict(ok=False, error="worker_mismatch"))
                return
            ok = self.server.app.lifecycle.report(worker, job, str(data.get("outcome") or ""),
                                        data.get("detail") if isinstance(data.get("detail"), dict) else {}, self.auth)
            self._send(200 if ok else 409, dict(ok=ok))
            return
        if path == "/watch-api/upload":
            self._upload(parse_qs(parts.query))
            return
        self._send(404, dict(ok=False, error="not_found"))

    # ---- 上传 ----------------------------------------------------------
    def _upload(self, query: dict) -> None:
        job_id = (query.get("job") or [""])[0]
        name = unquote((query.get("name") or [""])[0])
        lower = name.lower()
        allowed = lower.endswith(".yrp") or lower.endswith(".yrp.evidence.json")
        if not job_id or not allowed or len(name) > 200 or "/" in name or "\\" in name:
            self._send(400, dict(ok=False, error="bad_upload_name"))
            return
        bound = getattr(self, "auth_worker", "")
        if bound:
            try:
                source = str(self.server.app.jobs.upload_source(job_id) or "")
            except Exception:
                source = ""
            # collector 重启后任务板可能为空；此时允许继续上传已认领的旧任务。
            if source and not hmac.compare_digest(source, bound):
                self._send(403, dict(ok=False, error="worker_mismatch"))
                return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD:
            self._send(413, dict(ok=False, error="bad_size"))
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._send(400, dict(ok=False, error="short_body"))
            return
        players = [item.strip() for item in unquote(self.headers.get("X-Players") or "").split(",")
                   if item.strip()]
        try:
            recorded = float(self.headers.get("X-Recorded-At") or time.time())
        except ValueError:
            recorded = time.time()
        try:
            with self.server.app.lifecycle.upload_authorized(self.auth, job_id):
                result = self.server.app.save_upload(job_id, name, body, players, recorded)
        except InsufficientStorageError:
            self._send(507, dict(ok=False, error="接收磁盘剩余空间不足，暂停接收新录像。"))
            return
        except PermissionError:
            raise
        except (OSError, ValueError) as exc:
            self._send(500, dict(ok=False, error=str(exc)[:120]))
            return
        self._send(200 if result else 404, dict(ok=bool(result), **(result or {})))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WatchApiServer:
    """把 RemoteJobs 暴露成 HTTP。start()/stop() 幂等，可从任意线程调用。"""

    def __init__(self, jobs, replay_root, token: str, log=print,
                 bind=("127.0.0.1", 8791), max_upload: int = MAX_UPLOAD, receipt_path=None,
                 token_store=None, query_auth=None, admin_token="", query_core=None,
                 writes_enabled=False, light_gate=6, heavy_gate=2,
                 light_wait=0.5, heavy_wait=1.5):
        self.jobs = jobs
        self.replay_root = Path(replay_root)
        self.token = str(token or "")
        self.token_store = token_store
        self.query_auth = query_auth
        self.admin_token = str(admin_token or "")
        self.query_core = query_core
        self.writes_enabled = bool(writes_enabled)
        self.query_light_gate = max(1, min(int(light_gate), 32))
        self.query_heavy_gate = max(1, min(int(heavy_gate), 16))
        self.query_light_wait = max(0.0, min(float(light_wait), 5.0))
        self.query_heavy_wait = max(0.0, min(float(heavy_wait), 5.0))
        self.log = log
        self.bind = bind
        self.max_upload = max_upload
        self.receipt_path = Path(receipt_path) if receipt_path else self.replay_root.parent / "watch-receipts.json"
        self._httpd = None
        self._thread = None
        self._upload_lock = threading.RLock()
        from .watch_task_api import TaskLifecycle
        self.lifecycle = TaskLifecycle(self)

    # ---- 供 handler 调用 -------------------------------------------------
    def claim(self, worker: str, capacity: int, ip: str, wait: float = 0.0) -> dict:
        return self.jobs.claim(worker, capacity, ip, wait)

    def heartbeat(self, worker: str, active, ip: str = "") -> list[str]:
        return self.jobs.heartbeat(worker, active, ip)

    def report(self, worker: str, job_id: str, outcome: str, detail: dict) -> bool:
        ok = self.jobs.report(worker, job_id, outcome, detail)
        if ok:
            self.log(f"[远程] {worker} 结束任务 {job_id}：{outcome or 'unknown'}")
        return ok

    def snapshot(self) -> dict:
        return self.jobs.snapshot()

    def save_upload(self, job_id: str, name: str, body: bytes,
                    players: list[str], recorded: float) -> dict:
        with self._upload_lock:
            return self._save_replay_upload(job_id, name, body, players, recorded)

    def _save_replay_upload(self, job_id, name, body, players, recorded):
        storage = self.replay_root if self.replay_root.exists() else self.replay_root.parent
        if shutil.disk_usage(storage).free < max(64*1024*1024, 2*len(body)):
            raise InsufficientStorageError("replay_storage_low")
        focus = ""
        with self.jobs._lock:                     # 只读任务载荷，锁很短
            job = self.jobs._jobs.get(job_id)
            if job is not None:
                focus = str((job.payload.get("focus") or [""])[0] or "")
                if not players:
                    players = list(job.payload.get("room", {}).get("players") or [])
        folder = _folder_part(focus or (players[0] if players else ""))
        target_dir = self.replay_root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        evidence = name.lower().endswith(".evidence.json")
        suffix = ".yrp.evidence.json" if evidence else ".yrp"
        safe_name = _safe_part(name[: -len(suffix)]) + suffix
        path = target_dir / safe_name
        from . import watch_replay_evidence as live
        if evidence:
            capture = live.decode(body)
            replay = live.locate(self, target_dir/safe_name[:-len('.evidence.json')], capture)
            path = Path(str(replay)+'.evidence.json')
            previous = live.decode(path.read_bytes()) if path.is_file() else None
            capture = live.merge(previous, capture)
            changed = live.write(path, capture)
            status = 'unchanged'
            if changed or not replay.is_file():
                try:
                    status = live.register_and_refresh(self, replay, job_id, players or [folder], recorded, capture)
                except (OSError, sqlite3.Error):
                    status = 'facts_pending'
                    self.log('[远程] 旁证已保存，单局事实待同步')
            self.log(f'[远程] 收到小局证据 {path.relative_to(self.replay_root)}（任务 {job_id}，{status}）')
            return dict(path=str(path), digest=hashlib.sha256(body).hexdigest(), evidence_status=status)
        # Repeated replay bytes reuse the canonical path; never detach its sidecar.
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(body).hexdigest():
            return dict(path=str(path), digest=hashlib.sha256(body).hexdigest(), evidence_status='unchanged')
        index = 2
        while path.exists():
            path = target_dir / f"{Path(safe_name).stem} ({index}){Path(safe_name).suffix}"
            index += 1
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(body)
        os.replace(tmp, path)
        digest = hashlib.sha256(body).hexdigest()
        if not evidence:
            try:
                from .watch_upload_sessions import record as record_session_upload
                record_session_upload(self.replay_root.parent, digest, job_id, players or [folder], recorded)
            except (OSError, ValueError, sqlite3.Error):
                self.log('[远程] 场次来源留存失败，录像接收继续；不推定首局')
            try:
                from .watch_receipts import record_receipt
                record_receipt(self.receipt_path, digest, self.jobs.upload_source(job_id), players or [folder], recorded)
            except OSError:
                self.log('[远程] 来源记录写入失败，录像接收继续')
            self.jobs.enqueue_ingest(str(path), players or [folder], recorded, digest)
            self.jobs.note_upload(job_id)
            metadata = Path(str(path)+'.evidence.json')
            if metadata.is_file():
                try:
                    capture = live.decode(metadata.read_bytes())
                    live.register_and_refresh(self, path, job_id, players or [folder], recorded, capture)
                except (OSError, ValueError, sqlite3.Error):
                    self.log('[远程] 已有旁证待核对；录像继续正常入库')
        else:
            # Evidence may arrive after ingest. Keep its small identity separately
            # so readers see it without replaying the fact database.
            try:
                capture = json.loads(body)
                replay = target_dir / safe_name[:-len('.evidence.json')]
                if (isinstance(capture, dict) and replay.is_file()
                        and replay.stat().st_size <= self.max_upload
                        and hashlib.sha256(replay.read_bytes()).hexdigest() == capture.get('digest')):
                    from .watch_upload_sessions import record as record_session_upload
                    record_session_upload(self.replay_root.parent, capture['digest'], job_id,
                                          players or [folder], recorded,
                                          {k: capture.get(k) for k in ('session_id','game_no','captured_from_start')})
            except (OSError, ValueError, sqlite3.Error):
                self.log('[远程] 首局证据留存失败，保持首局未确认')
        self.log(f"[远程] 收到{'小局证据' if evidence else '录像'} "
                 f"{path.relative_to(self.replay_root)}（{len(body)} 字节，任务 {job_id}）")
        return dict(path=str(path), digest=digest)

    # ---- 生命周期 --------------------------------------------------------
    def start(self) -> bool:
        if self._httpd is not None:
            return True
        if not self.token and self.token_store is None and self.query_auth is None:
            self.log("[远程] 未配置 token 且没有每设备令牌库，接口不启动")
            return False
        httpd = _Server(self.bind, _Handler)
        httpd.app = self
        httpd.app_token = self.token
        httpd.token_store = self.token_store
        httpd.query_light_gate = threading.BoundedSemaphore(self.query_light_gate)
        httpd.query_heavy_gate = threading.BoundedSemaphore(self.query_heavy_gate)
        httpd.query_light_wait = self.query_light_wait
        httpd.query_heavy_wait = self.query_heavy_wait
        httpd.query_auth = self.query_auth
        httpd.admin_token = self.admin_token
        httpd.query_core = self.query_core
        httpd.writes_enabled = self.writes_enabled
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="watch-api", daemon=True)
        self._thread.start()
        self.lifecycle.start()
        self.log(f"[远程] 接口监听 http://{self.bind[0]}:{self.bind[1]}/watch-api/")
        return True

    def stop(self) -> None:
        self.lifecycle.stop()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        if self.query_core is not None:
            self.query_core.close()
