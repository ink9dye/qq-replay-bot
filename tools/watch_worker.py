"""远程观战端（小程序）。

跑在任意一台有公网 IP 的电脑上：向服务器认领观战任务 -> 连萌卡房间录制 ->
把 .yrp 上传回服务器 -> 上报结果。服务器负责发现房间、分配、去重和入库。

    python tools/watch_worker.py --server http://101.201.108.188:8787 \
        --token <watch.remote.token> --name pc-a --capacity 2

依赖：只用到仓库里的 watch.py（标准库）。不读取、不保存账号凭据——入房口令
由服务器随任务下发。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import watch  # noqa: E402

STOP = threading.Event()
ACTIVE: dict[str, dict] = {}
ACTIVE_LOCK = threading.Lock()


def _request(server: str, token: str, path: str, body: bytes | None = None,
             headers: dict | None = None, timeout: float = 20.0) -> tuple[int, bytes]:
    url = server.rstrip("/") + path
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    request.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"请求失败：{exc}") from exc


def _json_request(server: str, token: str, path: str, payload: dict) -> tuple[int, dict]:
    status, raw = _request(server, token, path, json.dumps(payload).encode("utf-8"),
                           {"Content-Type": "application/json; charset=utf-8"})
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return status, {}


def claim(server: str, token: str, name: str, capacity: int) -> dict:
    status, data = _json_request(server, token, "/watch-api/claim",
                                 dict(worker=name, capacity=capacity))
    return data.get("job") or {} if status == 200 else {}


def heartbeat(server: str, token: str, name: str, active: list[str]) -> None:
    _json_request(server, token, "/watch-api/heartbeat", dict(worker=name, active=active))


def report(server: str, token: str, name: str, job_id: str, outcome: str, detail: dict) -> None:
    _json_request(server, token, "/watch-api/report",
                  dict(worker=name, job=job_id, outcome=outcome, detail=detail))


def upload(server: str, token: str, job_id: str, path: Path, players: list[str],
           recorded_at: float) -> bool:
    body = path.read_bytes()
    headers = {"Content-Type": "application/octet-stream",
               "X-Players": quote(",".join(str(item) for item in players)),
               "X-Recorded-At": f"{recorded_at:.3f}"}
    query = f"?job={quote(job_id)}&name={quote(path.name)}"
    status, raw = _request(server, token, "/watch-api/upload" + query, body, headers, timeout=60)
    if status != 200:
        print(f"[worker] 上传失败 {path.name}：{status} {raw[:120]!r}", flush=True)
        return False
    return True


def _room_from_job(job: dict) -> watch.RoomInfo:
    room = job.get("room") or {}
    seats = {}
    for pos, name in (room.get("seats") or {}).items():
        try:
            seats[int(pos)] = str(name)
        except (TypeError, ValueError):
            continue
    return watch.RoomInfo(key=str(room.get("key") or ""), title=str(room.get("title") or ""),
                          join_id=str(room.get("join_id") or ""),
                          players=[str(name) for name in room.get("players") or []],
                          started=bool(room.get("started")), seats=seats)


def run_job(server: str, token: str, name: str, job: dict, work_dir: Path) -> None:
    job_id = str(job.get("id") or "")
    room = _room_from_job(job)
    server_info = job.get("server") or {}
    config = dict(host=str(server_info.get("host") or "tiramisu.moenext.com"),
                  port=int(server_info.get("port") or 8911),
                  version=int(server_info.get("version") or watch.DEFAULT_VERSION),
                  nickname=str(job.get("nickname") or "Spectator"),
                  deck_dir=str(work_dir / "decks"))
    target_dir = work_dir / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    writer = watch.ReplayWriter(target_dir, [str(item) for item in (job.get("focus") or [])],
                                "exact")
    saved = {"games": 0, "uploaded": 0, "bytes": 0}
    seen: set[str] = set()

    def on_saved(written, players, when, digest) -> None:
        if digest in seen:
            for path in written:
                Path(path).unlink(missing_ok=True)
            return
        seen.add(digest)
        recorded = when.timestamp() if hasattr(when, "timestamp") else time.time()
        for index, path in enumerate(written):
            path = Path(path)
            if not path.is_file():
                continue
            if index == 0:
                if upload(server, token, job_id, path, [str(item) for item in players], recorded):
                    saved["uploaded"] += 1
                    saved["bytes"] += path.stat().st_size
            sidecar = Path(str(path) + ".evidence.json")
            if sidecar.is_file():
                upload(server, token, job_id, sidecar, [str(item) for item in players], recorded)
                sidecar.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        saved["games"] += 1

    writer.on_saved = on_saved
    spectator = watch.Spectator(config, room, writer.for_room(room.players))
    spectator.fixed_password = str(job.get("password") or "")
    print(f"[worker] 认领 {room.key}（{room.title}）{room.players}", flush=True)
    outcome = "failed"
    try:
        outcome = asyncio.run(spectator.run()) or "failed"
    except Exception as exc:  # 远程端任何异常都只影响这一局
        print(f"[worker] 任务异常 {job_id}：{exc}", flush=True)
        outcome = "failed"
    finally:
        report(server, token, name, job_id, outcome, dict(saved=saved["games"],
                                                          uploaded=saved["uploaded"],
                                                          bytes=saved["bytes"]))
        with ACTIVE_LOCK:
            ACTIVE.pop(job_id, None)
        try:
            for leftover in target_dir.rglob("*"):
                if leftover.is_file():
                    leftover.unlink()
            target_dir.rmdir()
        except OSError:
            pass
        print(f"[worker] 结束 {room.key}：{outcome}，上传 {saved['uploaded']} 个", flush=True)


def heartbeat_loop(server: str, token: str, name: str) -> None:
    while not STOP.is_set():
        with ACTIVE_LOCK:
            active = list(ACTIVE)
        try:
            heartbeat(server, token, name, active)
        except RuntimeError:
            pass
        STOP.wait(10.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="远程观战端")
    parser.add_argument("--server", required=True, help="服务器地址，例如 http://101.201.108.188:8787")
    parser.add_argument("--token", required=True, help="watch.remote.token")
    parser.add_argument("--name", required=True, help="本机名字，例 pc-a")
    parser.add_argument("--capacity", type=int, default=1, help="同时观战几路（默认 1）")
    parser.add_argument("--work-dir", default="", help="临时目录，默认系统临时目录")
    parser.add_argument("--once", action="store_true", help="只领一个任务就退出（调试用）")
    args = parser.parse_args()

    name = args.name.strip()[:64]
    capacity = max(1, min(16, int(args.capacity or 1)))
    work_root = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="ygowatch-"))
    work_root.mkdir(parents=True, exist_ok=True)
    print(f"[worker] {name} 连接 {args.server}，并发 {capacity}，临时目录 {work_root}", flush=True)

    def _stop(signum, frame):
        STOP.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    threading.Thread(target=heartbeat_loop, args=(args.server, args.token, name),
                     daemon=True, name="heartbeat").start()
    pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="job")
    errors = 0
    while not STOP.is_set():
        with ACTIVE_LOCK:
            free = capacity - len(ACTIVE)
        if free <= 0:
            STOP.wait(1.0)
            continue
        try:
            job = claim(args.server, args.token, name, capacity)
            errors = 0
        except RuntimeError as exc:
            errors += 1
            print(f"[worker] 认领失败：{exc}", flush=True)
            STOP.wait(min(30.0, 2.0 * errors))
            continue
        if not job:
            STOP.wait(1.0)
            continue
        job_id = str(job.get("id") or "")
        with ACTIVE_LOCK:
            ACTIVE[job_id] = dict(room=(job.get("room") or {}).get("key", ""), at=time.time())
        pool.submit(run_job, args.server, args.token, name, job, work_root)
        if args.once:
            break
    pool.shutdown(wait=True)
    print("[worker] 已退出", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
