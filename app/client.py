"""远程观战客户端核心：领任务、登录萌卡、进房录制、上传、上报。

不依赖 Tkinter，可被 GUI 或测试直接驱动。协议与录制逻辑复用仓库根目录的
``watch.py``；本文件只负责远程任务循环、并发调度、停止/取消和 UI 事件。
"""
from __future__ import annotations

import asyncio
import json
import random
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import watch

from .config import AppConfig, normalize_server
from .paths import APP_VERSION, RotatingLog
from .redact import Redactor

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
DONE_STAGES = ("done", "error", "skipped", "stopped")


class ClientError(RuntimeError):
    pass


class AuthError(ClientError):
    pass


@dataclass
class Timeouts:
    request: float = 20.0
    claim_wait: float = 8.0
    heartbeat: float = 10.0
    retry_max: float = 20.0
    join_interval: float = 20.0
    join_jitter: float = 5.0


@dataclass
class AccountSlot:
    """一个萌卡账号占用的并发槽位。同一账号同一时刻只进一间房。"""

    account: str
    password: str
    secret: int = 0
    secret_at: float = 0.0
    nickname: str = ""
    job_id: str = ""
    state: str = "idle"          # idle 空闲 / busy 观战中 / login 登录中 / error 不可用
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "account": self.account,
            "nickname": self.nickname or self.account,
            "state": self.state,
            "job_id": self.job_id,
            "error": self.error,
        }


@dataclass
class Job:
    id: str
    room_key: str
    title: str
    players: list[str]
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    stage: str = "queued"
    status: str = "准备中"
    detail: str = ""
    account: str = ""
    saved: int = 0
    uploaded: int = 0
    uploaded_bytes: int = 0
    packets: int = 0

    def as_dict(self) -> dict:
        end = self.finished_at or time.time()
        return {
            "id": self.id,
            "room_key": self.room_key,
            "title": self.title,
            "players": list(self.players),
            "account": self.account,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": max(0.0, end - self.started_at),
            "stage": self.stage,
            "status": self.status,
            "detail": self.detail,
            "saved": self.saved,
            "uploaded": self.uploaded,
            "uploaded_bytes": self.uploaded_bytes,
            "packets": self.packets,
        }


def _status_text(code: int) -> str:
    return {
        400: "请求参数不正确",
        401: "接口令牌无效或已轮换",
        403: "接口拒绝访问，请确认令牌权限",
        404: "服务器没有启用远程观战接口",
        409: "任务已过期（服务器可能刚重启）",
        413: "上传内容超过服务器限制",
        429: "请求过于频繁，请稍后再试",
        502: "服务器接口暂不可用（网关错误）",
        503: "服务器接口暂不可用（服务未启动）",
        504: "服务器接口响应超时",
        507: "服务器接收磁盘空间不足",
    }.get(code, f"服务器返回 HTTP {code}")


def format_bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


class WatchClient:
    """一个客户端实例对应一次“开始观战 → 停止”的运行。"""

    def __init__(self, config: AppConfig, data_dir: Path,
                 emit: Callable[[str, dict], None] | None = None,
                 timeouts: Timeouts | None = None,
                 watch_module=watch,
                 writer_factory=None,
                 spectator_factory=None):
        self.config = config
        self.data_dir = Path(data_dir)
        self.emit_raw = emit or (lambda kind, data: None)
        self.timeouts = timeouts or Timeouts()
        self.watch = watch_module
        self.writer_factory = writer_factory or watch_module.ReplayWriter
        self.spectator_factory = spectator_factory or watch_module.Spectator

        self._log_file = RotatingLog(self.data_dir / "logs", "client.log")
        self._redactor = Redactor([config.token] + [item.password for item in config.real_accounts()])
        self.watch.log = self._log

        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._job_threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._state = "stopped"
        self._state_detail = ""
        self._jobs: dict[str, Job] = {}
        self._jobs_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = {"saved": 0, "uploaded": 0, "bytes": 0, "jobs": 0, "failed": 0}
        self._session_started_at = 0.0
        # 每个账号一份独立凭据/昵称/占用状态，一个账号只负责一间房。
        self._slots: list[AccountSlot] = [
            AccountSlot(account=item.account.strip(), password=item.password)
            for item in config.real_accounts()
        ]
        self._slots_lock = threading.Lock()

        self._room_lock = threading.Lock()
        self._room_active: set[str] = set()
        self._room_seen: dict[str, float] = {}
        self._join_lock = threading.Lock()
        self._last_join_at = 0.0
        self._last_claim_error = ""
        self._last_claim_error_at = 0.0

    # ------------------------------------------------------------------ 公共状态
    @property
    def state(self) -> str:
        return self._state

    @property
    def state_detail(self) -> str:
        return self._state_detail

    @property
    def nickname(self) -> str:
        with self._slots_lock:
            names = [slot.nickname or slot.account for slot in self._slots]
        return "、".join(names)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def jobs_snapshot(self) -> list[dict]:
        with self._jobs_lock:
            return [job.as_dict() for job in sorted(self._jobs.values(),
                                                    key=lambda item: item.started_at, reverse=True)]

    def accounts_snapshot(self) -> list[dict]:
        with self._slots_lock:
            return [slot.as_dict() for slot in self._slots]

    def stats_snapshot(self) -> dict:
        with self._stats_lock:
            stats = dict(self._stats)
        stats.update(active=self._active_count(), capacity=self.config.capacity,
                     worker=self.config.name, nickname=self.nickname,
                     accounts=self.accounts_snapshot(),
                     session_started_at=self._session_started_at)
        return stats

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if self.is_running():
            return False
        self._stop.clear()
        self._state = "starting"
        self._state_detail = "正在检查服务器…"
        self._session_started_at = time.time()
        self._stats = {"saved": 0, "uploaded": 0, "bytes": 0, "jobs": 0, "failed": 0}
        self._reset_slots()
        self._emit_state()
        self._emit_stats()
        self._thread = threading.Thread(target=self._run, name="watch-client", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if not self.is_running() and self._state in ("stopped", "error"):
            return
        if self._stop.is_set():
            return
        self._stop.set()
        self._set_state("stopping", "正在断开当前观战并上报…")
        self._log("正在停止观战客户端…", "warn")

    def wait(self, timeout: float | None = None) -> bool:
        """等领任务线程和所有任务线程都结束。

        只等领任务线程是不够的：临时目录清理、证据补传都在任务线程里，
        上一版停止后立刻断言会看到残留的 work/job-* 目录。
        """
        deadline = None if timeout is None else time.time() + timeout
        threads = [self._thread]
        with self._jobs_lock:
            threads.extend(self._job_threads)
        for thread in [item for item in threads if item]:
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            thread.join(remaining)
        alive = [item for item in threads if item and item.is_alive()]
        return not alive

    # ------------------------------------------------------------------ 事件/日志
    def _reset_slots(self) -> None:
        with self._slots_lock:
            for slot in self._slots:
                slot.secret = 0
                slot.secret_at = 0.0
                slot.nickname = ""
                slot.job_id = ""
                slot.state = "idle"
                slot.error = ""

    def _emit(self, kind: str, **data) -> None:
        try:
            self.emit_raw(kind, data)
        except Exception:
            pass

    def _emit_state(self) -> None:
        self._emit("state", state=self._state, detail=self._state_detail)

    def _set_state(self, state: str, detail: str = "") -> None:
        if self._state == state and self._state_detail == detail:
            return
        self._state = state
        self._state_detail = detail
        self._emit_state()

    def _emit_jobs(self) -> None:
        self._emit("jobs", jobs=self.jobs_snapshot())

    def _emit_stats(self) -> None:
        self._emit("stats", stats=self.stats_snapshot())

    def _log(self, message: str, level: str = "info") -> None:
        text = self._redactor(str(message or ""))
        if not text:
            return
        self._log_file.write(text)
        self._emit("log", level=level, text=text)

    def _watch_log(self, message: str) -> None:
        self._log(message, "info")

    # ------------------------------------------------------------------ 主流程
    def _run(self) -> None:
        fatal = False
        try:
            self._log(f"客户端 {self.config.name} 启动，接口 {normalize_server(self.config.server)}")
            info = self._ping()
            self._emit("connection", ok=True, info=info)
            self._log(self._connection_text(info), "success")
            if self._stop.is_set():
                return

            count = len(self._slots)
            self._set_state("authenticating", f"正在登录 {count} 个萌卡账号…")
            usable = self._login_all()
            if not usable:
                raise AuthError("所有萌卡账号都登录失败，请检查账号密码或网络")
            if self._stop.is_set():
                return

            self._set_state("running", f"在线待命 · {self.config.name} · {usable} 个账号")
            self._log(f"{usable}/{count} 个账号可用，最多同时观战 {usable} 间", "success")
            self._start_heartbeat()
            self._claim_loop()
        except AuthError as exc:
            fatal = True
            self._log(str(exc), "error")
            self._set_state("error", str(exc))
        except ClientError as exc:
            fatal = True
            self._log(str(exc), "error")
            self._set_state("error", str(exc))
        except Exception as exc:  # 未预期异常也要让界面恢复
            fatal = True
            self._log(f"客户端异常：{type(exc).__name__}: {exc}", "error")
            self._set_state("error", f"{type(exc).__name__}: {exc}")
        finally:
            self._stop.set()
            self._emit_stats()
            if self._state != "error":
                self._set_state("stopped", self._state_detail if self._state == "stopped" else "已停止")
            if not fatal:
                self._log("观战客户端已停止", "info")

    def _ping(self) -> dict:
        status, raw = self._request("GET", "/watch-api/status", timeout=15)
        if status != 200:
            if status in (401, 403):
                raise AuthError(_status_text(status))
            raise ClientError(_status_text(status))
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise ClientError("服务器状态接口返回的不是 JSON")
        if not isinstance(payload, dict):
            raise ClientError("服务器状态接口返回格式异常")
        return self._connection_info(payload)

    @staticmethod
    def _connection_info(payload: dict) -> dict:
        workers = payload.get("workers") if isinstance(payload.get("workers"), dict) else {}
        online = 0
        for item in workers.values():
            if isinstance(item, dict) and item.get("online"):
                online += 1
        return {
            "online_workers": online,
            "pending": int(payload.get("pending") or 0),
            "claimed": int(payload.get("claimed") or 0),
            "ip_cap": payload.get("ip_cap"),
        }

    @staticmethod
    def _connection_text(info: dict) -> str:
        return (f"服务器连接成功 · 在线客户端 {info.get('online_workers', 0)} 个 · "
                f"待认领 {info.get('pending', 0)} 个")

    # ------------------------------------------------------------------ 心跳/领任务
    def _active_ids(self) -> list[str]:
        with self._jobs_lock:
            return [job.id for job in self._jobs.values() if job.stage not in DONE_STAGES]

    def _active_count(self) -> int:
        return len(self._active_ids())

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                  name="watch-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            active = self._active_ids()
            payload = {"worker": self.config.name}
            if active:
                payload["active"] = active
            try:
                status, raw = self._post_json("/watch-api/heartbeat", payload, timeout=15)
                if status == 200:
                    failures = 0
                elif status in (401, 403):
                    self._log("心跳认证失败，请检查接口令牌", "warn")
                    failures += 1
                else:
                    failures += 1
            except ClientError:
                failures += 1
            if failures and failures in (3, 10, 30):
                self._log(f"心跳连续失败 {failures} 次，客户端会继续重试", "warn")
            self._stop.wait(self.timeouts.heartbeat)

    def _claim_loop(self) -> None:
        errors = 0
        while not self._stop.is_set():
            if self._active_count() >= max(1, int(self.config.capacity)):
                self._stop.wait(0.5)
                continue
            try:
                job = self._claim()
                errors = 0
                if self._state == "reconnecting":
                    self._set_state("running", f"在线待命 · {self.config.name} · {self.config.capacity} 个账号")
                    self._log("服务器连接已恢复", "success")
                if self._stop.is_set():
                    if job:
                        self._report(str(job.get("id") or ""), "failed", {"stopped": True})
                    break
                if not job:
                    self._stop.wait(0.8)
                    continue
                self._spawn_job(job)
            except AuthError as exc:
                raise
            except ClientError as exc:
                errors += 1
                message = str(exc)
                now = time.time()
                if message != self._last_claim_error or now - self._last_claim_error_at > 30:
                    self._log(f"认领任务失败：{message}，将自动重试", "warn")
                    self._last_claim_error = message
                    self._last_claim_error_at = now
                self._set_state("reconnecting", f"网络异常，{min(30, 2 ** min(errors, 4))} 秒后重试")
                self._stop.wait(min(30, 2 ** min(errors, 4)))
            except Exception as exc:
                errors += 1
                self._log(f"认领循环异常：{type(exc).__name__}: {exc}", "error")
                self._stop.wait(min(30, 2 ** min(errors, 4)))

    def _claim(self) -> dict:
        payload = {"worker": self.config.name, "capacity": int(self.config.capacity),
                   "wait": min(15.0, self.timeouts.claim_wait)}
        status, raw = self._post_json("/watch-api/claim", payload,
                                      timeout=max(20.0, self.timeouts.claim_wait + 12))
        if status == 204:
            return {}
        if status == 200:
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                raise ClientError("认领接口返回的不是 JSON")
            job = data.get("job") if isinstance(data, dict) else None
            return job if isinstance(job, dict) else {}
        if status in (401, 403):
            raise AuthError(_status_text(status))
        raise ClientError(_status_text(status))

    # ------------------------------------------------------------------ 任务
    def _spawn_job(self, payload: dict) -> None:
        job_id = str(payload.get("id") or "").strip()
        if not job_id:
            return
        room = payload.get("room") if isinstance(payload.get("room"), dict) else {}
        players = [str(name) for name in (room.get("players") or []) if str(name).strip()]
        title = str(room.get("title") or room.get("join_id") or room.get("key") or "未知房间")
        with self._jobs_lock:
            existing = self._jobs.get(job_id)
            if existing and existing.stage not in DONE_STAGES:
                self._log(f"忽略重复派发的任务：{title}（{job_id[:8]}）", "warn")
                return

        slot = self._take_slot(job_id)
        if slot is None:
            # 并发等于账号数，正常不会走到这里；真出现就退回给服务器重派。
            self._log(f"没有空闲账号，暂不接收任务：{title}", "warn")
            self._report(job_id, "retry", {"reason": "no_idle_account"})
            return

        job = Job(id=job_id, room_key=str(room.get("key") or job_id), title=title,
                  players=players, account=slot.account)
        with self._jobs_lock:
            self._jobs[job_id] = job
            self._trim_jobs_locked()
        self._emit_jobs()
        self._log(f"认领任务：{title}" + (f"（{' vs '.join(players)}）" if players else "")
                  + f" · 账号 {slot.account}")
        thread = threading.Thread(target=self._run_job, args=(payload, job, slot),
                                  name=f"watch-job-{job_id[:8]}", daemon=True)
        with self._jobs_lock:
            self._job_threads = [item for item in self._job_threads if item.is_alive()]
            self._job_threads.append(thread)
        thread.start()

    def _trim_jobs_locked(self) -> None:
        if len(self._jobs) <= 100:
            return
        done = [job for job in self._jobs.values() if job.stage in DONE_STAGES]
        done.sort(key=lambda item: item.started_at)
        for job in done[:max(0, len(self._jobs) - 100)]:
            self._jobs.pop(job.id, None)

    def _update_job(self, job: Job, **fields) -> None:
        with self._jobs_lock:
            for key, value in fields.items():
                setattr(job, key, value)
        self._emit_jobs()

    def _job_by_id(self, job_id: str) -> Job | None:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def _run_job(self, payload: dict, job: Job, slot: AccountSlot) -> None:
        room = self._room_from_job(payload)
        job_id = job.id
        claimed = False
        duplicate = ""
        error = ""
        outcome = "failed"
        saved = {"games": 0, "uploaded": 0, "bytes": 0}
        target_dir = self.data_dir / "work" / job_id
        seen_digests: set[str] = set()

        def on_saved(written, players, when, digest) -> None:
            if digest in seen_digests:
                for path in written:
                    Path(path).unlink(missing_ok=True)
                return
            seen_digests.add(digest)
            recorded = when.timestamp() if hasattr(when, "timestamp") else time.time()
            paths = [Path(path) for path in written]
            if not paths:
                return
            saved["games"] += 1
            first = paths[0]
            if self.config.keep_local and first.is_file():
                local = self._keep_local_copy(job_id, first)
                if local:
                    self._log(f"已在本机保留录像：{local}", "success")
            uploaded_ok = False
            size = 0
            if first.is_file():
                size = first.stat().st_size
                uploaded_ok = self._upload(first, job_id, [str(item) for item in players], recorded)
                if uploaded_ok:
                    saved["uploaded"] += 1
                    saved["bytes"] += size
            job.saved = saved["games"]
            job.uploaded = saved["uploaded"]
            job.uploaded_bytes = saved["bytes"]
            self._update_job(job, saved=job.saved, uploaded=job.uploaded,
                             uploaded_bytes=job.uploaded_bytes,
                             detail=f"已保存 {job.saved} 个小局，上传 {job.uploaded} 个")
            with self._stats_lock:
                self._stats["saved"] += 1
                if uploaded_ok:
                    self._stats["uploaded"] += 1
                    self._stats["bytes"] += size
            self._emit_stats()
            if uploaded_ok:
                self._log(f"已上传录像 {first.name}（{format_bytes(size)}）", "success")
            else:
                self._log(f"录像 {first.name} 上传未成功，服务器可能未入库", "warn")

        try:
            self._update_job(job, stage="waiting", status="准备中", detail="正在准备账号凭据…")
            if self._stop.is_set():
                outcome = "stopped"
                return
            try:
                state = self._refresh_account(slot)
            except Exception as exc:
                error = f"凭据准备失败：{type(exc).__name__}: {exc}"
                self._log(f"任务 {job_id[:8]} 账号 {slot.account} {error}", "error")
                outcome = "failed"
                return

            claimed, why = self._room_claim(job.room_key)
            if not claimed:
                duplicate = why
                outcome = "noreplay"
                self._update_job(job, stage="skipped", status="重复跳过",
                                 detail="同一房间已在录制或 30 分钟内录过")
                self._log(f"跳过重复任务：{job.title}（{why}）", "warn")
                return

            config = self._job_config(payload, room, state, slot)
            target_dir.mkdir(parents=True, exist_ok=True)
            writer = self.writer_factory(target_dir,
                                          [str(item) for item in (payload.get("focus") or [])],
                                          "exact")
            writer.on_saved = on_saved
            spectator = self.spectator_factory(config, room, writer.for_room(room.players))

            self._update_job(job, stage="waiting", status="等待进场",
                             detail="为避免平台限制，正在错峰进场…")
            if not self._pace_join():
                outcome = "stopped"
                return
            if self._stop.is_set():
                outcome = "stopped"
                return

            self._update_job(job, stage="connecting", status="连接中",
                             detail=f"正在用账号 {slot.account} 连接萌卡房间…")
            self._log(f"进入房间：{room.title or room.key}"
                      + (f"（{' vs '.join(room.players)}）" if room.players else "")
                      + f" · 账号 {slot.account}")
            outcome = asyncio.run(self._run_spectator(spectator, job)) or "failed"
        except asyncio.CancelledError:
            outcome = "stopped"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:220]
            outcome = "stopped" if self._stop.is_set() else "failed"
            self._log(f"任务 {job_id[:8]} 异常：{error}", "error")
        finally:
            if self._stop.is_set() and outcome not in ("saved", "noreplay"):
                outcome = "stopped"
            # 先还账号再收尾：任务线程结束前把槽位放回去，认领循环才能接下一间。
            self._release_slot(slot)
            # 胜负证据由 Spectator 在 save() 返回后补写；此时再统一上传，
            # 避免在 on_saved 回调里提前删掉 .yrp 导致证据永远写不出来。
            self._upload_evidence(target_dir, job_id, room.players)
            detail = dict(saved=saved["games"], uploaded=saved["uploaded"], bytes=saved["bytes"])
            if error:
                detail["error"] = error
            if duplicate:
                detail.update(duplicate=True, reason=duplicate)
            if outcome == "stopped":
                detail["stopped"] = True
            self._finish_job(job, outcome, detail, duplicate, error)
            if claimed:
                self._room_release(job.room_key)
            try:
                shutil.rmtree(target_dir, ignore_errors=True)
            except OSError:
                pass

    async def _run_spectator(self, spectator, job: Job) -> str:
        task = asyncio.create_task(spectator.run())
        last_marker = None
        try:
            while not task.done():
                if self._stop.is_set():
                    task.cancel()
                    break
                packets = int(getattr(spectator, "packets_received", 0) or 0)
                duel_started = bool(getattr(spectator, "duel_started", False))
                duel_ended = bool(getattr(spectator, "duel_ended", False))
                if duel_ended:
                    status, stage = "等待录像", "waiting_replay"
                elif duel_started:
                    status, stage = "观战中", "watching"
                elif packets:
                    status, stage = "已进入房间", "watching"
                else:
                    status, stage = "连接中", "connecting"
                marker = (stage, packets)
                if marker != last_marker:
                    last_marker = marker
                    self._update_job(job, stage=stage, status=status,
                                     packets=packets,
                                     detail=f"已接收 {packets} 个数据包" if packets else "正在等待房间数据…")
                try:
                    await asyncio.wait({task}, timeout=1.5)
                except asyncio.CancelledError:
                    raise
            if task.done():
                return task.result()
        except asyncio.CancelledError:
            pass
        finally:
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            try:
                await asyncio.wait_for(spectator.close(), timeout=5)
            except Exception:
                pass
        return "stopped"

    def _finish_job(self, job: Job, outcome: str, detail: dict, duplicate: str, error: str) -> None:
        status_map = {
            "saved": ("已完成", "done"),
            "noreplay": ("无录像", "done"),
            "stopped": ("已停止", "stopped"),
            "failed": ("失败", "error"),
            "version": ("版本不一致", "error"),
            "gone": ("房间已结束", "done"),
            "unstable": ("连接不稳定", "error"),
            "config": ("配置错误", "error"),
            "retry": ("未完成", "error"),
            "lost": ("连接中断", "error"),
        }
        status, stage = status_map.get(outcome, (outcome or "结束", "done"))
        if duplicate:
            status, stage = "重复跳过", "skipped"
        text = f"保存 {detail.get('saved', 0)} 个小局，上传 {detail.get('uploaded', 0)} 个"
        if error:
            text = error
        elif duplicate:
            text = "同一房间已在录制或 30 分钟内录过"
        self._update_job(job, stage=stage, status=status, detail=text,
                         finished_at=time.time(), saved=int(detail.get("saved") or 0),
                         uploaded=int(detail.get("uploaded") or 0),
                         uploaded_bytes=int(detail.get("bytes") or 0))
        with self._stats_lock:
            self._stats["jobs"] += 1
            if outcome in ("failed", "version", "unstable", "config", "retry", "lost"):
                self._stats["failed"] += 1
        self._emit_stats()
        self._report(job.id, outcome, detail)
        self._log(f"任务结束：{job.title} · {status} · {text}",
                  "success" if outcome == "saved" else "warn" if outcome in ("noreplay", "gone", "stopped", "retry") else "error")

    def _upload_evidence(self, target_dir: Path, job_id: str, players: list[str]) -> None:
        if not target_dir.is_dir():
            return
        for sidecar in target_dir.rglob("*.yrp.evidence.json"):
            if not sidecar.is_file():
                continue
            try:
                recorded = sidecar.stat().st_mtime
            except OSError:
                recorded = time.time()
            if self._upload(sidecar, job_id, [str(item) for item in players], recorded):
                self._log(f"已上传小局胜负证据：{sidecar.name}", "success")
            else:
                self._log(f"小局胜负证据上传失败：{sidecar.name}", "warn")

    def _report(self, job_id: str, outcome: str, detail: dict) -> None:
        try:
            status, raw = self._post_json("/watch-api/report", {
                "worker": self.config.name, "job": job_id,
                "outcome": outcome, "detail": detail,
            }, timeout=20)
            if status != 200:
                self._log(f"上报任务 {job_id[:8]} 失败：HTTP {status}", "warn")
        except ClientError as exc:
            self._log(f"上报任务 {job_id[:8]} 网络失败：{exc}", "warn")

    # ------------------------------------------------------------------ 账号/房间
    def _take_slot(self, job_id: str) -> AccountSlot | None:
        """借一个空闲且可用的账号给这个任务；借不到返回 None。"""
        with self._slots_lock:
            for slot in self._slots:
                if slot.state == "idle" and not slot.job_id:
                    slot.state = "busy"
                    slot.job_id = job_id
                    return slot
        return None

    def _release_slot(self, slot: AccountSlot) -> None:
        with self._slots_lock:
            slot.job_id = ""
            if slot.state == "busy":
                slot.state = "idle"
        self._emit_stats()

    @staticmethod
    def _sign_in(watch_module, account: str, password: str) -> tuple[int, str]:
        try:
            secret, username = watch_module.sign_in(account, password)
        except PermissionError as exc:
            raise AuthError(f"萌卡登录失败：{exc}") from exc
        except RuntimeError as exc:
            raise AuthError(f"萌卡登录失败：{exc}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ClientError(f"萌卡登录网络异常：{exc}") from exc
        return int(secret), str(username or account)

    def _login(self, slot: AccountSlot, quiet: bool = False) -> bool:
        with self._slots_lock:
            slot.state = "login"
        try:
            secret, nickname = self._sign_in(self.watch, slot.account, slot.password)
        except (AuthError, ClientError) as exc:
            with self._slots_lock:
                slot.error = str(exc)
                # 手里还有旧口令就继续把这间录完，别把正在跑的任务一起废掉。
                slot.state = "error" if not slot.secret else ("busy" if slot.job_id else "idle")
            if not quiet:
                self._log(f"账号 {slot.account} 登录失败：{exc}", "error")
            self._emit("account", ok=False, account=slot.account, error=str(exc))
            self._emit_stats()
            return False
        with self._slots_lock:
            slot.secret = secret
            slot.secret_at = time.time()
            slot.nickname = nickname
            slot.error = ""
            slot.state = "busy" if slot.job_id else "idle"
        self._log(f"萌卡账号已登录：{slot.account}（{nickname}）", "success")
        self._emit("account", ok=True, account=slot.account, username=nickname)
        self._emit_stats()
        return True

    def _login_all(self) -> int:
        """逐个登录所有账号，返回可用账号数。账号之间留一点间隔，别一起打萌卡。"""
        usable = 0
        for index, slot in enumerate(list(self._slots)):
            if self._stop.is_set():
                break
            if index:
                self._stop.wait(0.4)
            if self._login(slot):
                usable += 1
        return usable

    def _refresh_account(self, slot: AccountSlot) -> dict:
        """取这个账号的入房口令；凭据约 4 分钟轮换，超过 150 秒就重新登录。"""
        with self._slots_lock:
            fresh = bool(slot.secret) and (time.time() - slot.secret_at) <= 150
            secret, nickname = slot.secret, slot.nickname
        if fresh:
            return {"secret": secret, "nickname": nickname}
        try:
            self._login(slot, quiet=True)
        except Exception as exc:  # 兜底：登录流程本身崩了也别让任务线程裸奔
            self._log(f"账号 {slot.account} 凭据刷新异常：{type(exc).__name__}: {exc}", "warn")
        with self._slots_lock:
            if slot.secret:
                return {"secret": slot.secret, "nickname": slot.nickname}
            error = slot.error or "账号不可用"
        raise ClientError(f"账号 {slot.account} {error}")

    def _job_config(self, payload: dict, room, state: dict, slot: AccountSlot) -> dict:
        server = payload.get("server") if isinstance(payload.get("server"), dict) else {}
        return {
            "host": str(server.get("host") or "tiramisu.moenext.com"),
            "port": int(server.get("port") or 8911),
            "version": self.watch.parse_version(server.get("version") or self.watch.DEFAULT_VERSION),
            "nickname": str(state.get("nickname") or slot.nickname or slot.account or self.config.name),
            "account": slot.account,
            "password": slot.password,
            "mycard_secret": int(state.get("secret") or 0),
            "join_mode": "mycard",
            "deck_dir": str(self.data_dir / "decks"),
        }

    def _room_from_job(self, payload: dict):
        room_data = payload.get("room") if isinstance(payload.get("room"), dict) else {}
        seats = {}
        for pos, name in (room_data.get("seats") or {}).items():
            try:
                seats[int(pos)] = str(name)
            except (TypeError, ValueError):
                continue
        return self.watch.RoomInfo(
            key=str(room_data.get("key") or ""),
            title=str(room_data.get("title") or ""),
            join_id=str(room_data.get("join_id") or ""),
            players=[str(name) for name in (room_data.get("players") or [])],
            started=bool(room_data.get("started")),
            seats=seats,
        )

    def _room_claim(self, key: str) -> tuple[bool, str]:
        key = key or "unknown"
        now = time.time()
        with self._room_lock:
            for seen_key, at in list(self._room_seen.items()):
                if now - at >= 1800:
                    self._room_seen.pop(seen_key, None)
            if key in self._room_active:
                return False, "active"
            if key in self._room_seen:
                return False, "recent"
            self._room_active.add(key)
            return True, ""

    def _room_release(self, key: str) -> None:
        with self._room_lock:
            self._room_active.discard(key or "unknown")
            self._room_seen[key or "unknown"] = time.time()

    def _pace_join(self) -> bool:
        """两次新连接至少隔 20 秒 + 0–5 秒抖动；停止时立刻返回 False。"""
        with self._join_lock:
            target = self._last_join_at + self.timeouts.join_interval + random.uniform(
                0.0, max(0.0, self.timeouts.join_jitter))
            now = time.time()
            wait = target - now
            while wait > 0:
                if self._stop.is_set():
                    return False
                time.sleep(min(0.5, wait))
                wait = target - time.time()
            self._last_join_at = time.time()
            return not self._stop.is_set()

    def _keep_local_copy(self, job_id: str, source: Path) -> Path | None:
        try:
            folder = self.data_dir / "local-replays" / time.strftime("%Y-%m-%d")
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / source.name
            index = 2
            while target.exists():
                target = folder / f"{source.stem} ({index}){source.suffix}"
                index += 1
            shutil.copy2(source, target)
            return target
        except OSError as exc:
            self._log(f"本机保留录像失败：{exc}", "warn")
            return None

    # ------------------------------------------------------------------ HTTP/上传
    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None, timeout: float | None = None) -> tuple[int, bytes]:
        url = normalize_server(self.config.server) + path
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", "Bearer " + self.config.token)
        request.add_header("User-Agent", f"MoeCardWatch/{APP_VERSION}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeouts.request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ClientError(f"网络请求失败：{exc}") from exc

    def _post_json(self, path: str, payload: dict, timeout: float | None = None) -> tuple[int, bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request("POST", path, body,
                             {"Content-Type": "application/json; charset=utf-8"}, timeout)

    def _upload(self, path: Path, job_id: str, players: list[str], recorded_at: float) -> bool:
        if self._stop.is_set() and not path.is_file():
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size <= 0:
            self._log(f"跳过空文件：{path.name}", "warn")
            return False
        if size > MAX_UPLOAD_BYTES:
            self._log(f"录像 {path.name} 为 {format_bytes(size)}，超过服务器 4 MB 上限", "error")
            return False
        try:
            body = path.read_bytes()
        except OSError as exc:
            self._log(f"读取录像失败：{path.name}（{exc}）", "warn")
            return False
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Players": urllib.parse.quote(",".join(str(item) for item in players)),
            "X-Recorded-At": f"{recorded_at:.3f}",
        }
        query = f"?job={urllib.parse.quote(job_id)}&name={urllib.parse.quote(path.name)}"
        last = ""
        for attempt in range(3):
            if self._stop.is_set():
                return False
            try:
                status, raw = self._request("POST", "/watch-api/upload" + query, body, headers, timeout=60)
            except ClientError as exc:
                last = str(exc)
                status, raw = 0, b""
            if status == 200:
                return True
            last = f"HTTP {status} {raw[:120]!r}"
            if status not in (0, 408, 429, 500, 502, 503, 504):
                break
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        self._log(f"上传失败 {path.name}：{last}", "warn")
        return False

    # ------------------------------------------------------------------ 静态探测
    @staticmethod
    def test_connection(server: str, token: str) -> dict:
        try:
            base = normalize_server(server)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not base:
            return {"ok": False, "error": "请填写服务器地址"}
        request = urllib.request.Request(base + "/watch-api/status", method="GET")
        request.add_header("Authorization", "Bearer " + token)
        request.add_header("User-Agent", f"MoeCardWatch/{APP_VERSION}")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return {"ok": False, "error": f"网络请求失败：{exc}"}
        if status != 200:
            return {"ok": False, "error": _status_text(status), "http": status}
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return {"ok": False, "error": "状态接口返回的不是 JSON"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "状态接口返回格式异常"}
        info = WatchClient._connection_info(payload)
        return {"ok": True, "error": "", "http": status, **info}

    @staticmethod
    def verify_account(account: str, password: str) -> dict:
        if not account.strip() or not password:
            return {"ok": False, "error": "请先填写萌卡账号和密码"}
        try:
            _secret, username = watch.sign_in(account.strip(), password)
            return {"ok": True, "username": str(username or account)}
        except PermissionError as exc:
            return {"ok": False, "error": f"登录已过期：{exc}"}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return {"ok": False, "error": f"网络请求失败：{exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def verify_accounts(accounts: list[tuple[str, str]]) -> list[dict]:
        """逐个验证多组账号，返回每组的结果（顺序与传入一致）。"""
        results: list[dict] = []
        for index, (account, password) in enumerate(accounts):
            if index:
                time.sleep(0.4)
            result = WatchClient.verify_account(account, password)
            result["account"] = account
            results.append(result)
        return results
