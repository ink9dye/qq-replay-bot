"""远程观战端（小程序）。

跑在任意一台有公网 IP 的电脑上：向服务器认领观战任务 -> 用**本机**萌卡账号登录
拿 u16Secret -> 连萌卡房间录制 -> 把 .yrp 上传回服务器 -> 上报结果。服务器负责
发现房间、分配、去重和入库。

    python tools/watch_worker.py --config tools/worker.json

账号密码只放在本机 `worker.json`（已 gitignore），不写日志、不上传服务器；服务器
下发的任务里没有入房口令，口令由本机每次进场前用 `watch.build_join_password` 算。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
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
# 服务器端派发没有"已派发"去重（capacity>1 时同一房间可能被连续派两次），
# 客户端按 room.key 兜底：同一房间同时只录一份，30 分钟内不重复录。
ROOM_LOCK = threading.Lock()
ROOM_ACTIVE: set[str] = set()
ROOM_SEEN: dict[str, float] = {}
ROOM_DEDUPE_SECONDS = 1800.0
ACCOUNT_LOCK = threading.Lock()
ACCOUNT_SIGNIN_LOCK = threading.Lock()
# 只在本机内存里保存登录结果；密码不会写日志，也不会进任务上报。
ACCOUNT: dict = {"account": "", "password": "", "secret": 0, "nickname": "", "signed_at": 0.0}


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


def ping(server: str, token: str) -> None:
    """启动时确认接口可达、token 有效、服务端确实开了远程观战。"""
    status, _ = _request(server, token, "/watch-api/status", timeout=15)
    if status == 200:
        return
    if status in (401, 403):
        raise SystemExit(f"接口认证失败（HTTP {status}）：worker.json 的 token 与服务端 remote.token 不一致")
    if status == 404:
        raise SystemExit("服务器没有启用远程观战（HTTP 404）：先确认 config.json 的 remote.enabled=true")
    if status in (502, 503, 504):
        raise SystemExit(f"服务端 8791 没有监听（HTTP {status}）：remote.enabled 还是 false，或 collector 未重启")
    raise SystemExit(f"服务器接口不可用（HTTP {status}）")


def claim(server: str, token: str, name: str, capacity: int, wait: float = 15.0) -> dict:
    """长轮询：服务器没任务就挂住最多 wait 秒，有任务立刻返回。

    wait 必须小于 `_request` 的 20 秒超时，否则客户端会先超时、白白丢掉这次认领。
    """
    status, data = _json_request(server, token, "/watch-api/claim",
                                 dict(worker=name, capacity=capacity, wait=wait))
    if status == 200:
        return data.get("job") or {}
    if status == 204:
        return {}
    raise RuntimeError(f"认领失败（HTTP {status}）：检查服务端 remote.enabled 与 worker.json 的 token")


def heartbeat(server: str, token: str, name: str, active: list[str]) -> None:
    _json_request(server, token, "/watch-api/heartbeat", dict(worker=name, active=active))


def report(server: str, token: str, name: str, job_id: str, outcome: str, detail: dict) -> None:
    status, _ = _json_request(server, token, "/watch-api/report",
                              dict(worker=name, job=job_id, outcome=outcome, detail=detail))
    if status != 200:
        # 任务过期（409）时录像仍可能已经上传成功；这里只把失败原因打出来。
        print(f"[worker] 上报失败（HTTP {status}）：任务 {job_id} {outcome}", flush=True)


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


def load_account(cfg: dict) -> dict:
    """账号密码只从本机 worker.json 读；缺了就拒绝启动，避免领了任务却进不去。"""
    account = str(cfg.get("account") or "").strip()
    password = str(cfg.get("password") or "")
    if not account or not password:
        raise SystemExit("worker.json 缺少 account/password（只存本机，不要提交）")
    return dict(account=account, password=password)


def sign_in_account(creds: dict) -> dict:
    """本机登录萌卡换 u16Secret。密码只在内存里，不打印、不上传。"""
    secret, username = watch.sign_in(creds["account"], creds["password"])
    state = dict(secret=int(secret), nickname=str(username or creds["account"]), signed_at=time.time())
    with ACCOUNT_LOCK:
        ACCOUNT.update(state)
    return dict(state)


def account_state() -> dict:
    with ACCOUNT_LOCK:
        return dict(ACCOUNT)


def refresh_account(creds: dict, max_age: float = 150.0) -> dict:
    """任务开始前保证 u16Secret 不超过 max_age 秒；优先用会话 token，必要时才重新登录。"""
    state = account_state()
    if state.get("secret") and time.time() - float(state.get("signed_at") or 0) <= max_age:
        return state
    with ACCOUNT_SIGNIN_LOCK:
        state = account_state()
        if state.get("secret") and time.time() - float(state.get("signed_at") or 0) <= max_age:
            return state
        temp = dict(account=creds["account"], password=creds["password"], join_mode="mycard")
        try:
            watch.refresh_secret(temp)
        except Exception as exc:
            if state.get("secret"):
                print(f"[worker] 凭据刷新失败，沿用旧值：{type(exc).__name__}", flush=True)
                return state
            raise
        state = dict(secret=int(temp.get("mycard_secret") or 0),
                     nickname=str(temp.get("nickname") or state.get("nickname") or ""),
                     signed_at=time.time())
        with ACCOUNT_LOCK:
            ACCOUNT.update(state)
        return state


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


def job_config(job: dict, creds: dict, work_dir: Path, state: dict | None = None) -> dict:
    """把任务里的服务器信息和本机账号拼成 watch.Spectator 的配置。

    `mycard_secret` 是任务开始时的快照；Spectator.run() 每次进场前会走
    `refresh_secret_shared()`（60 秒节流）重新取，拿不到才沿用旧值。
    """
    server_info = job.get("server") or {}
    state = state or account_state()
    return dict(host=str(server_info.get("host") or "tiramisu.moenext.com"),
                port=int(server_info.get("port") or 8911),
                version=int(server_info.get("version") or watch.DEFAULT_VERSION),
                nickname=str(state.get("nickname") or job.get("nickname") or "Spectator"),
                account=creds["account"],
                password=creds["password"],
                mycard_secret=int(state.get("secret") or 0),
                join_mode="mycard",
                deck_dir=str(work_dir / "decks"))


def _room_claim(key: str) -> tuple[bool, str]:
    """同一房间并发只放一份；刚录完的 30 分钟内不再录（拖长局/重复派发）。"""
    now = time.time()
    with ROOM_LOCK:
        for seen_key, at in list(ROOM_SEEN.items()):
            if now - at >= ROOM_DEDUPE_SECONDS:
                ROOM_SEEN.pop(seen_key, None)
        if key in ROOM_ACTIVE:
            return False, "active"
        if key in ROOM_SEEN:
            return False, "recent"
        ROOM_ACTIVE.add(key)
        return True, ""


def _room_release(key: str) -> None:
    with ROOM_LOCK:
        ROOM_ACTIVE.discard(key)
        ROOM_SEEN[key] = time.time()


def run_job(server: str, token: str, name: str, job: dict, work_dir: Path, creds: dict) -> None:
    job_id = str(job.get("id") or "")
    room = _room_from_job(job)
    room_key = room.key or job_id
    claimed = False
    duplicate = ""
    config = job_config(job, creds, work_dir, refresh_account(creds))
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
    print(f"[worker] 认领 {room.key}（{room.title}）{room.players}", flush=True)
    outcome = "failed"
    error = ""
    try:
        claimed, why = _room_claim(room_key)
        if not claimed:
            duplicate = why
            print(f"[worker] 跳过重复任务 {job_id}：{room_key} 已在录/刚录过（{why}）", flush=True)
            outcome = "noreplay"
            return
        outcome = asyncio.run(spectator.run()) or "failed"
    except Exception as exc:  # 远程端任何异常都只影响这一局
        error = f"{type(exc).__name__}: {exc}"[:200]
        print(f"[worker] 任务异常 {job_id}：{error}", flush=True)
        outcome = "failed"
    finally:
        detail = dict(saved=saved["games"], uploaded=saved["uploaded"], bytes=saved["bytes"])
        if error:
            detail["error"] = error
        if duplicate:
            detail.update(duplicate=True, reason=duplicate)
        report(server, token, name, job_id, outcome, detail)
        if claimed:
            _room_release(room_key)
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
    parser.add_argument("--config", default="", help="worker.json（推荐）；命令行参数可覆盖")
    parser.add_argument("--server", default="", help="服务器地址，例如 http://101.201.108.188:8787")
    parser.add_argument("--token", default="", help="watch.remote.token")
    parser.add_argument("--name", default="", help="本机名字，例 pc-a")
    parser.add_argument("--capacity", type=int, default=0, help="同时观战几路（默认 1）")
    parser.add_argument("--work-dir", default="", help="临时目录，默认系统临时目录")
    parser.add_argument("--once", action="store_true", help="只领一个任务就退出（调试用）")
    args = parser.parse_args()

    cfg = {}
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise SystemExit("worker.json 必须是对象")
    server = (args.server or cfg.get("server") or "").strip()
    token = (args.token or cfg.get("token") or "").strip()
    name = (args.name or cfg.get("name") or "").strip()[:64]
    capacity = int(args.capacity or cfg.get("capacity") or 1)
    work_dir = args.work_dir or cfg.get("work_dir") or ""
    if not server or not token or not name:
        raise SystemExit("缺少 server/token/name（命令行或 worker.json）")

    capacity = max(1, min(16, capacity))
    creds = load_account(cfg)
    ping(server, token)
    print("[worker] 本机账号登录萌卡…", flush=True)
    try:
        sign_in_account(creds)
    except Exception as exc:
        raise SystemExit(f"萌卡登录失败（{type(exc).__name__}）：{exc}")
    print("[worker] 萌卡凭据已就绪（口令由本机按局计算）", flush=True)

    work_root = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="ygowatch-"))
    work_root.mkdir(parents=True, exist_ok=True)
    print(f"[worker] {name} 连接 {server}，并发 {capacity}，临时目录 {work_root}", flush=True)

    def _stop(signum, frame):
        STOP.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    threading.Thread(target=heartbeat_loop, args=(server, token, name),
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
            job = claim(server, token, name, capacity)
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
        pool.submit(run_job, server, token, name, job, work_root, creds)
        if args.once:
            break
    pool.shutdown(wait=True)
    print("[worker] 已退出", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
