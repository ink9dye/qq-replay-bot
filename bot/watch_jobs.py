"""远程观战任务板。

服务器（大程序）发现房间后把"观战任务"放到这里，远程端（小程序，可跑在
不同公网 IP 的电脑/手机上）来认领、录制、上传录像。服务器只负责发现、
分配、去重和入库，不再自己连每一个房间。

线程模型：
- `offer_sync()` 在观战进程的事件循环外（executor 线程）等待认领；
- HTTP API 线程调用 `claim()/heartbeat()/report()`；
- 上传的录像先进 `ingest` 队列，由观战主循环在事件循环里取出入库，
  避免 SQLite 跨线程写。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid

# 任务默认租赁时长：远程端认领后必须在此时间内 report 或继续 heartbeat，
# 否则任务被视为丢失（房间一般 15 分钟，租赁只要覆盖"连上+记录"的启动阶段）。
DEFAULT_LEASE = 120.0
# 任务在板上最长停留时间，超时后不再被认领（房间多半已结束）。
DEFAULT_JOB_TTL = 180.0
# 远程端超过这个时间没有 claim/heartbeat 就视为离线。
DEFAULT_WORKER_TTL = 45.0


class Job:
    __slots__ = ("id", "payload", "created", "expires", "claimed_by", "claimed_at",
                 "lease_until", "uploaded", "last_upload_at", "done", "event", "outcome", "detail", "generation")

    def __init__(self, payload: dict, ttl: float):
        self.id = uuid.uuid4().hex[:12]
        self.payload = payload
        self.created = time.time()
        self.expires = self.created + float(ttl)
        self.claimed_by = ""
        self.generation = ""
        self.claimed_at = 0.0
        self.lease_until = 0.0
        self.uploaded = 0
        self.last_upload_at = 0.0
        self.done = False
        self.event = threading.Event()
        self.outcome = ""
        self.detail: dict = {}


class RemoteJobs:
    """任务板 + 在线端登记 + 上传入库队列。所有公开方法都可跨线程调用。"""

    def __init__(self, lease: float = DEFAULT_LEASE, job_ttl: float = DEFAULT_JOB_TTL,
                 worker_ttl: float = DEFAULT_WORKER_TTL,
                 single_ip_max: int = 7, multi_ip_max: int = 6):
        self.lease = float(lease)
        self.job_ttl = float(job_ttl)
        self.worker_ttl = float(worker_ttl)
        # 单个公网 IP 的并发上限：只有 1 个 IP 时 7；出现多个 IP 时**每个 IP 各自 6**
        # （不是所有 IP 合计 6），平台风控口径。
        self.single_ip_max = max(1, int(single_ip_max))
        self.multi_ip_max = max(1, int(multi_ip_max))
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._workers: dict[str, dict] = {}
        self._heartbeat_jobs: dict[str, set[str]] = {}
        self.ingest: "queue.Queue[dict]" = queue.Queue()
        self._reports: list[dict] = []
        self.stats = dict(offered=0, claimed=0, reported=0, unclaimed=0,
                          expired=0, uploaded=0, failed=0)
        self.on_release = None

    # ---- 服务器侧：派发 -------------------------------------------------
    def offer_sync(self, payload: dict, wait: float) -> str:
        """放入任务并阻塞等待认领；返回认领的 worker 名，超时返回空串。"""
        with self._lock:
            self._prune(time.time())
            job = Job(payload, self.job_ttl)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self.stats["offered"] += 1
        job.event.wait(max(0.0, float(wait)))
        with self._lock:
            if job.done:
                return job.claimed_by
            if job.claimed_by:
                return job.claimed_by
            if not job.event.is_set():
                self.stats["unclaimed"] += 1
            self._drop(job.id)
            return ""

    def cancel(self, job_id: str) -> None:
        with self._lock:
            self._drop(job_id, 'cancelled', True)

    def _refresh(self, worker):
        info = self._workers.get(worker)
        if info is not None:
            assigned = {key for key, job in self._jobs.items() if job.claimed_by == worker and not job.done}
            reported = self._heartbeat_jobs.get(worker, set())
            info['active'] = len(assigned | reported)

    def _bind(self, worker, generation):
        info = self._workers.get(worker)
        if info is not None and info.get('generation', '') != generation:
            self._invalidate(worker, info.get('generation', ''), 'token_replaced')
            self._workers.pop(worker, None)
        info = self._workers.setdefault(worker, dict(active=0, ip='', seen=0., capacity=1, generation=generation))
        return info

    def _invalidate(self, worker, generation, reason):
        info = self._workers.get(worker)
        if info is None or info.get('generation', '') != generation:
            return
        for key, job in list(self._jobs.items()):
            if job.claimed_by == worker and job.generation == generation:
                self._drop(key, reason, True)
        for key in list(self._heartbeat_jobs.pop(worker, set())):
            if self.on_release: self.on_release(key, worker, generation, reason)
        info.update(active=0, seen=0., invalidated=True, retired_at=time.time())

    def invalidate(self, worker, generation, reason='token_invalid'):
        with self._lock:
            self._invalidate(worker, generation, reason)

    def generations(self):
        with self._lock:
            return [(name, info.get('generation', '')) for name, info in self._workers.items()]

    def release_job(self, worker, generation, job_id, reason):
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.claimed_by == worker and job.generation == generation:
                self._drop(job_id, reason, True)
            info = self._workers.get(worker)
            if info and info.get('generation', '') == generation:
                self._heartbeat_jobs.get(worker, set()).discard(job_id)
                self._refresh(worker)

    def drain_reports(self) -> list[dict]:
        with self._lock:
            items, self._reports = self._reports, []
        return items

    def drain_ingest(self) -> list[dict]:
        items = []
        while True:
            try:
                items.append(self.ingest.get_nowait())
            except queue.Empty:
                return items

    def enqueue_ingest(self, path, players: list[str], recorded_at: float, digest: str) -> None:
        self.ingest.put(dict(path=path, players=list(players),
                             recorded_at=float(recorded_at), digest=digest))

    # ---- 远程端：认领/心跳/上报 -----------------------------------------
    def claim(self, worker: str, capacity: int = 1, ip: str = "", wait: float = 0.0, generation: str = '') -> dict:
        """认领任务。wait>0 时长轮询：没任务就挂住，最多等 wait 秒。

        每个 worker 的 capacity 之外，还要受“单公网 IP 并发上限”约束：
        在线 IP 只有 1 个时 7，多于 1 个时每个 IP 6。
        """
        deadline = time.time() + max(0.0, float(wait or 0.0))
        while True:
            now = time.time()
            with self._lock:
                self._prune(now)
                info = self._bind(worker, generation)
                info.update(seen=now, capacity=max(1, int(capacity or 1)), ip=ip or info.get("ip", ""))
                client_ip = str(info.get("ip") or "")
                ip_cap = self._ip_cap(now)
                if info["active"] < info["capacity"] and (
                        not client_ip or self._active_for_ip(client_ip) < ip_cap):
                    for job_id in self._order:
                        job = self._jobs.get(job_id)
                        if job is None or job.claimed_by or job.done:
                            continue
                        job.claimed_by = worker
                        job.generation = generation
                        job.claimed_at = now
                        job.lease_until = now + self.lease
                        self._refresh(worker)
                        self.stats["claimed"] += 1
                        job.event.set()
                        payload = dict(job.payload)
                        payload.update(id=job.id, lease=self.lease)
                        return payload
            if now >= deadline:
                return {}
            time.sleep(min(0.5, max(0.05, deadline - now)))

    def heartbeat(self, worker: str, active: list[str] | None = None, ip: str = "", generation: str = '') -> list[str]:
        """续租；返回服务器认为应该放弃的任务 id（房间已结束/任务过期）。

        已被认领的任务由租约（lease）管生死，不能再按 job_ttl 丢：一局 Bo3 常常
        十几分钟，若还按 180 秒的 TTL 清理，录像上传后就找不到 job、report 会 409。
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            info = self._bind(worker, generation)
            info["seen"] = now
            if ip:
                info["ip"] = ip
            live = set(active or [])
            if active is not None:
                self._heartbeat_jobs[worker] = live
                info['reported_at'] = now
            for job_id in list(self._jobs):
                job = self._jobs[job_id]
                if job.claimed_by != worker:
                    continue
                if active is not None and job_id not in live:
                    # 远程端已经不在跑这个任务了：释放，允许重新派发
                    self._drop(job_id, 'client_stopped', True)
                    continue
                job.lease_until = now + self.lease
            keep = [job_id for job_id, job in self._jobs.items() if job.claimed_by == worker]
            # 信任客户端上报的 active：collector 重启后任务板是空的，但客户端可能还在录旧任务；
            # 这时若把 active 重置为 0，会误判空闲并疯狂空派发。
            self._refresh(worker)
            return []

    def report(self, worker: str, job_id: str, outcome: str, detail: dict | None = None, generation: str = '', verified_owner=False) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and (job.claimed_by != worker or job.generation != generation):
                return False
            if job is None:
                # Clients can finish old recordings after a collector restart.
                # Accept only ownership explicitly recovered from a fresh heartbeat.
                if not verified_owner and self._upload_source_unlocked(job_id) != worker: return False
                job = Job({}, 0)
                job.claimed_by = worker
            job.outcome = str(outcome or "")
            job.detail = dict(detail or {})
            job.done = True
            self.stats["reported"] += 1
            if job.outcome not in ("saved", "noreplay"):
                self.stats["failed"] += 1
            self._reports.append(dict(worker=worker, job=job_id, outcome=job.outcome,
                                      detail=job.detail, room=job.payload.get("room", {}).get("key", ""),
                                      at=time.time()))
            self._drop(job_id, 'completed')
            self._heartbeat_jobs.get(worker, set()).discard(job_id)
            self._refresh(worker)
            return True

    def upload_source(self, job_id: str) -> str:
        """Prefer assigned ownership; recover old job ownership only from explicit heartbeats."""
        with self._lock:
            return self._upload_source_unlocked(job_id)

    def _upload_source_unlocked(self, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if job is not None and job.claimed_by: return job.claimed_by
        now = time.time()
        matches = [worker for worker, active in self._heartbeat_jobs.items()
                   if job_id in active and now-float(self._workers.get(worker,{}).get('seen') or 0)<=self.worker_ttl]
        return matches[0] if len(matches)==1 else ''

    def note_upload(self, job_id: str = "") -> None:
        with self._lock:
            self.stats["uploaded"] += 1
            job = self._jobs.get(job_id) if job_id else None
            if job is not None:
                job.uploaded += 1
                job.last_upload_at = time.time()

    # ---- 状态 -----------------------------------------------------------
    def _online_ips(self, now: float) -> set[str]:
        return {str(info.get("ip")) for info in self._workers.values()
                if info.get("ip") and now - float(info.get("seen") or 0) <= self.worker_ttl}

    def _ip_cap(self, now: float) -> int:
        return self.single_ip_max if len(self._online_ips(now)) <= 1 else self.multi_ip_max

    def _active_for_ip(self, ip: str) -> int:
        return sum(int(info.get("active") or 0) for info in self._workers.values()
                   if str(info.get("ip") or "") == str(ip)
                   and time.time() - float(info.get('seen') or 0) <= self.worker_ttl)

    def online_workers(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for info in self._workers.values()
                       if now - float(info.get("seen") or 0) <= self.worker_ttl)

    def free_slots(self, now: float | None = None) -> int:
        """当前还能接几单（同时考虑 worker capacity 和单 IP 上限；多 IP 时每个 IP 各自 6）。"""
        now = time.time() if now is None else float(now)
        with self._lock:
            self._prune(now)
            cap = self._ip_cap(now)
            total = 0
            per_ip = {}
            for info in self._workers.values():
                if now - float(info.get("seen") or 0) > self.worker_ttl:
                    continue
                ip = str(info.get("ip") or "")
                ip_free = cap - self._active_for_ip(ip) if ip else cap
                available = max(0, min(int(info.get("capacity") or 1) - int(info.get("active") or 0), ip_free))
                if ip: per_ip[ip] = min(ip_free, per_ip.get(ip, 0) + available)
                else: total += available
            return total + sum(max(0, value) for value in per_ip.values())

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._prune(now)
            workers = {name: dict(active=info.get("active", 0), capacity=info.get("capacity", 1),
                                  client_reported_active=len(self._heartbeat_jobs.get(name, set())),
                                  server_active=sum(j.claimed_by == name and not j.done for j in self._jobs.values()),
                                  seen=info.get('seen', 0), sampled_at=now,
                                  reported_at=info.get('reported_at'),
                                  ip=info.get("ip", ""),
                                  online=(now - float(info.get("seen") or 0) <= self.worker_ttl))
                       for name, info in self._workers.items()}
            # 只读服务端已经知道的事实：哪个 worker 认领了哪一间、这间已收到几个录像。
            # 不要求客户端上报局数；页面上局数无法得知时留空。
            rooms = []
            for job in self._jobs.values():
                if not job.claimed_by:
                    continue
                payload_room = job.payload.get("room") if isinstance(job.payload.get("room"), dict) else {}
                players = payload_room.get("players") if isinstance(payload_room.get("players"), list) else []
                rooms.append(dict(
                    job=job.id, worker=job.claimed_by,
                    room=str(payload_room.get("key") or "")[:80],
                    title=str(payload_room.get("title") or "")[:120],
                    players=[str(p)[:100] for p in players[:4]],
                    claimed_at=float(job.claimed_at or 0),
                    uploaded=int(job.uploaded or 0),
                    last_upload_at=float(job.last_upload_at or 0),
                    focus=[str(p)[:100] for p in (job.payload.get("focus") or [])[:4]],
                    reason=str(job.payload.get("reason") or "")[:80]))
            rooms.sort(key=lambda row: (row["worker"], row["claimed_at"] or 0))
            pending = sum(1 for job in self._jobs.values() if not job.claimed_by)
            return dict(stats=dict(self.stats), workers=workers,
                        pending=pending, claimed=len(self._jobs) - pending,
                        ips=sorted(self._online_ips(now)), ip_cap=self._ip_cap(now),
                        rooms=rooms)

    # ---- 内部 -----------------------------------------------------------
    def _drop(self, job_id: str, reason='released', retry=False) -> None:
        job = self._jobs.pop(job_id, None)
        if job is not None:
            job.done = True
            job.event.set()
            if job.claimed_by:
                self._heartbeat_jobs.get(job.claimed_by, set()).discard(job_id)
                self._refresh(job.claimed_by)
                if self.on_release: self.on_release(job_id, job.claimed_by, job.generation, reason)
                if retry:
                    self._reports.append(dict(worker=job.claimed_by,job=job_id,outcome=reason,
                        room=job.payload.get('room', {}).get('key', ''),detail={},at=time.time(),retry=True))
        try:
            self._order.remove(job_id)
        except ValueError:
            pass

    def _prune(self, now: float) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.done:
                self._drop(job_id, 'completed')
                continue
            # 未认领的任务才按 TTL 过期；已认领的看租约，避免长对局被误清。
            if not job.claimed_by and now >= job.expires:
                self.stats["expired"] += 1
                self._drop(job_id)
                continue
            if job.claimed_by and job.lease_until and now > job.lease_until:
                self.stats["expired"] += 1
                self._drop(job_id, 'lease_expired', True)
        for worker, info in list(self._workers.items()):
            if now - float(info.get('reported_at') or info.get('seen') or 0) > self.lease:
                for key in self._heartbeat_jobs.pop(worker, set()):
                    if self.on_release: self.on_release(key, worker, info.get('generation', ''), 'lease_expired')
                self._refresh(worker)
            if now - float(info.get('retired_at') or info.get('seen') or 0) > 86400:
                self._workers.pop(worker, None)
