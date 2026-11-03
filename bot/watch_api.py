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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

MAX_BODY = 8 * 1024 * 1024          # 控制请求体上限
MAX_UPLOAD = 4 * 1024 * 1024        # 单个录像上限
_SAFE_NAME = re.compile(r"[^0-9A-Za-z._\-()\u4e00-\u9fff ]+")


def _safe_part(text: str, fallback: str = "unknown") -> str:
    cleaned = _SAFE_NAME.sub("_", str(text or "")).strip(" .")
    return cleaned[:80] or fallback


class InsufficientStorageError(OSError):
    pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "ygobot-watch/1"
    protocol_version = "HTTP/1.1"

    # ---- 基础 ----------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: D401 - 走自己的日志
        return

    def _send(self, status: int, payload=None) -> None:
        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _auth(self) -> bool:
        # 旧版 remote.token 是兼容总令牌；每设备令牌从 SQLite 哈希表验证。
        self.auth_worker = ""
        token = getattr(self.server, "app_token", "")
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            got = header[7:].strip()
        else:
            got = (self.headers.get("X-Watch-Token") or "").strip()
        if token and got and hmac.compare_digest(got, token):
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
                return True
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
        if not self._auth():
            return
        parts = urlsplit(self.path)
        if parts.path == "/watch-api/status":
            self._send(200, dict(ok=True, **self.server.app.snapshot()))
            return
        self._send(404, dict(ok=False, error="not_found"))

    def do_POST(self):
        if not self._auth():
            return
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
            job = self.server.app.claim(worker, int(data.get("capacity") or 1), client_ip,
                                        float(data.get("wait") or 0))
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
            cancel = self.server.app.heartbeat(worker, active, client_ip)
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
            ok = self.server.app.report(worker, job, str(data.get("outcome") or ""),
                                        data.get("detail") if isinstance(data.get("detail"), dict) else {})
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
            result = self.server.app.save_upload(job_id, name, body, players, recorded)
        except InsufficientStorageError:
            self._send(507, dict(ok=False, error="接收磁盘剩余空间不足，暂停接收新录像。"))
            return
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
                 token_store=None):
        self.jobs = jobs
        self.replay_root = Path(replay_root)
        self.token = str(token or "")
        self.token_store = token_store
        self.log = log
        self.bind = bind
        self.max_upload = max_upload
        self.receipt_path = Path(receipt_path) if receipt_path else self.replay_root.parent / "watch-receipts.json"
        self._httpd = None
        self._thread = None

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
        folder = _safe_part(focus or (players[0] if players else ""))
        target_dir = self.replay_root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        evidence = name.lower().endswith(".evidence.json")
        suffix = ".yrp.evidence.json" if evidence else ".yrp"
        safe_name = name if _SAFE_NAME.sub("", name) == name else _safe_part(name[: -len(suffix)]) + suffix
        path = target_dir / safe_name
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
                from .watch_receipts import record_receipt
                record_receipt(self.receipt_path, digest, self.jobs.upload_source(job_id), players or [folder], recorded)
            except OSError:
                self.log('[远程] 来源记录写入失败，录像接收继续')
            self.jobs.enqueue_ingest(str(path), players or [folder], recorded, digest)
            self.jobs.note_upload(job_id)
        self.log(f"[远程] 收到{'小局证据' if evidence else '录像'} "
                 f"{path.relative_to(self.replay_root)}（{len(body)} 字节，任务 {job_id}）")
        return dict(path=str(path), digest=digest)

    # ---- 生命周期 --------------------------------------------------------
    def start(self) -> bool:
        if self._httpd is not None:
            return True
        if not self.token and self.token_store is None:
            self.log("[远程] 未配置 token 且没有每设备令牌库，接口不启动")
            return False
        httpd = _Server(self.bind, _Handler)
        httpd.app = self
        httpd.app_token = self.token
        httpd.token_store = self.token_store
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="watch-api", daemon=True)
        self._thread.start()
        self.log(f"[远程] 接口监听 http://{self.bind[0]}:{self.bind[1]}/watch-api/")
        return True

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
