# -*- coding: utf-8 -*-
"""P14 独立入口：在 Job 中启动本项目 NapCat 与 QQ 服务。

只在用户手动运行 start-qq-supervisor.cmd 时执行；不放入 start-all 默认路径。
外部已有 NapCat 时不接管、不杀，只提示。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from .launcher import install_close_handler, set_console_title, startup_lock
from .napcat_start import existing_napcat
from .qq_lifecycle import LifecycleState
from .qq_supervisor import Ownership, Supervisor, WindowsJobApi
from .setup import probe_napcat, read_config

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / 'data' / 'logs' / 'qq-supervisor.log'
STATE_PATH = ROOT / 'data' / 'qq-lifecycle.json'


def log(message: str) -> None:
    stamp = time.strftime('%H:%M:%S')
    line = f'[{stamp}] {message}'
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
    except OSError:
        pass


def _project_processes(root: Path) -> list[dict]:
    try:
        return list(existing_napcat(root) or [])
    except OSError as exc:
        log(f'枚举本项目 NapCat/QQ 失败：{exc}')
        return []


def _spawn_batch(path: Path) -> None:
    command = os.environ.get('COMSPEC') or 'cmd.exe'
    subprocess.Popen([command, '/d', '/s', '/c', str(path)], cwd=str(path.parent),
                     creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))


def _spawn_qq_service(root: Path):
    return subprocess.Popen([sys.executable, '-m', 'bot.qq_service'], cwd=str(root),
                            creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))


def _wait_processes(root: Path, timeout: float, predicate) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [item for item in _project_processes(root) if predicate(item)]
        if found:
            return found
        time.sleep(0.3)
    return []


def _login_ok(config: dict) -> bool:
    try:
        return bool(asyncio.run(probe_napcat(config, timeout=4))[0])
    except Exception:
        return False


def _cleanup(supervisor: Supervisor, ownership: Ownership, pids: list[int]) -> None:
    # Job 先终止/关闭；pid_tree 模式只结束记录的 PID，不按镜像名杀。
    actions = supervisor.stop(ownership, pids)
    log('停止完成：' + ','.join(actions))


def main() -> int:
    set_console_title('QQ机器人运行窗口（关闭即停止 NapCat/QQ）')
    root = ROOT
    config = read_config(root / 'config.json')
    existing = _project_processes(root)
    if existing:
        log('检测到本项目已有 NapCat/QQ 实例：外部启动，跳过 spawn/attach，不做结束。')
        log('请使用现有 NapCat 窗口与旧 start-all；本入口不接管外部实例。')
        return 0

    with startup_lock(root, 'qq-supervisor.lock') as acquired:
        if not acquired:
            log('另一个 QQ supervisor 正在运行，未重复启动。')
            return 2
        api = WindowsJobApi()
        supervisor = Supervisor({}, api, log)
        mode = supervisor.prepare_job(os.getpid())
        if mode == 'pid_tree':
            log('警告：Job 不可用，回退受管 PID 树；只结束本入口记录的 PID。')
        job_started = mode in ('job', 'nested_job')
        try:
            log('启动 NapCat（Job 已先创建）')
            _spawn_batch(root / 'start-napcat.bat')
            created = _wait_processes(root, 30, lambda item: item.get('Name') == 'NapCatWinBootMain.exe')
            if not created:
                log('30 秒内未发现 NapCat 进程，停止；未登录、未重启 QQ。')
                return 2
            # 立即把启动后出现的项目进程放入 Job，随后校验。
            for item in _project_processes(root):
                pid = int(item.get('ProcessId') or 0)
                if pid and job_started:
                    if supervisor.attach(pid):
                        log(f'已 attach {item.get("Name")} pid={pid}')
                    else:
                        log(f'warning: attach 失败 pid={pid}，转 PID 树')
                        job_started = False
            napcat_pid = int(created[0].get('ProcessId') or 0)
            log(f'NapCat pid={napcat_pid}，等待登录（不自动扫码）')
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if _login_ok(config):
                    log('NapCat 登录成功')
                    break
                time.sleep(3)
            else:
                log('等待登录超时；关闭 Job，未启动 QQ 服务。')
                if job_started:
                    _cleanup(supervisor, Ownership(True, 'timeout'), [napcat_pid])
                else:
                    _cleanup(supervisor, Ownership(True, 'timeout'), [])
                return 2

            log('启动 QQ 服务')
            qq_process = _spawn_qq_service(root)
            if job_started:
                if not supervisor.attach(qq_process.pid):
                    log(f'warning: QQ 服务 pid={qq_process.pid} 未进入 Job，转 PID 树')
                    job_started = False
            pids = [napcat_pid, qq_process.pid]
            if job_started:
                leaks = supervisor.verify_subtree(napcat_pid, pids)
                if leaks:
                    log(f'警告：以下 PID 不在 Job：{leaks}')
            state = LifecycleState(supervisor_pid=os.getpid(), supervisor_started_at=time.time(),
                                   napcat_pid=napcat_pid, napcat_created_at=time.time(),
                                   napcat_image='NapCatWinBootMain.exe', qq_pid=qq_process.pid,
                                   job_name='qq-replay-bot', owned=True)
            state.save(STATE_PATH)
            install_close_handler(lambda: _cleanup(supervisor, Ownership(True, 'close'), pids))
            log('运行中：关闭本窗口将先关 Job，再写日志；QQ 41520 不会被旧路径误杀。')
            qq_process.wait()
            _cleanup(supervisor, Ownership(True, 'qq_exit'), pids)
            return 0
        except Exception as exc:
            log(f'启动失败：{type(exc).__name__}: {exc}')
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
