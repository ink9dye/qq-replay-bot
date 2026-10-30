"""一键启动：复用已运行的 NapCat，等登录成功，再启动录像程序。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .setup import probe_napcat, read_config

ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def startup_lock(root: Path):
    """防止连续双击，在服务尚未绑定端口时启动出两个副本。"""
    import msvcrt
    path = root / 'data' / 'startup.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def ws_port_open(config: dict) -> bool:
    try:
        parsed = urlsplit(config['qq']['onebot_ws'])
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == 'wss' else 80)), timeout=1):
            return True
    except (OSError, ValueError, KeyError):
        return False


def bot_state(config: dict) -> str:
    """只认本程序的状态接口；其他服务占用端口时不重复启动。"""
    share = config.get('share') or {}
    host = str(share.get('bind_host') or '127.0.0.1')
    if host in ('0.0.0.0', '::', 'localhost'):
        host = '127.0.0.1'
    port = int(share.get('bind_port', 8787))
    address = f'http://{host}:{port}/health'
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(address, timeout=1) as response:
            payload = json.loads(response.read(1024))
            if payload.get('service') == 'qq-replay-bot' and payload.get('status') == 'running':
                return 'ready'
        return 'busy'
    except urllib.error.HTTPError:
        return 'busy'
    except (OSError, ValueError, AttributeError):
        try:
            with socket.create_connection((host, port), timeout=1):
                return 'busy'
        except OSError:
            return 'stopped'


def spawn_batch(path: Path) -> None:
    if not path.is_file():
        raise OSError('启动文件不存在')
    command = os.environ.get('COMSPEC', r'C:\Windows\System32\cmd.exe')
    # cmd /s /c 的外层引号保留带空格路径；路径来自本项目，不包含用户指令或密钥。
    subprocess.Popen(f'"{command}" /d /s /c ""{path}""', cwd=str(path.parent),
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


async def launch_services(root: Path, config: dict, *, spawn=spawn_batch, probe=probe_napcat,
                          state=bot_state, port_open=ws_port_open, pause=asyncio.sleep,
                          clock=time.monotonic, login_timeout=300, bot_timeout=25) -> bool:
    status = await asyncio.to_thread(state, config)
    if status == 'busy':
        print('录像服务端口被占用，可能是旧版程序。请先关闭旧录像程序窗口，再双击 start-all.bat。', flush=True)
        return False
    if not await asyncio.to_thread(port_open, config):
        print('正在打开 NapCat。若出现二维码，请用工具号扫码并确认登录。', flush=True)
        spawn(root / 'start-napcat.bat')
    else:
        print('NapCat 已在运行，正在检查工具号登录状态。', flush=True)
    deadline = clock() + login_timeout
    while clock() < deadline:
        ok, message = await probe(config, timeout=4)
        if ok:
            print('工具号已在线。', flush=True)
            break
        if 'Token 验证失败' in message:
            print(message, flush=True)
            return False
        await pause(2)
    else:
        print('等待登录超时。完成扫码后，再双击 start-all.bat 即可，已开的 NapCat 会复用。', flush=True)
        return False
    status = await asyncio.to_thread(state, config)
    if status == 'ready':
        print('录像程序也已运行，无需重复启动。', flush=True)
        return True
    if status == 'busy':
        print('录像服务端口已被占用，请检查是否有旧程序窗口。', flush=True)
        return False
    print('正在打开录像程序……', flush=True)
    spawn(root / 'start.bat')
    deadline = clock() + bot_timeout
    while clock() < deadline:
        if await asyncio.to_thread(state, config) == 'ready':
            print('启动完成。请保持 NapCat 和录像程序窗口开着。', flush=True)
            return True
        await pause(1)
    print('录像程序未能启动，请查看新打开的录像程序窗口里的提示。', flush=True)
    return False


def main() -> None:
    try:
        config = read_config(ROOT / 'config.json')
        if not (config.get('qq') or {}).get('access_token'):
            print('接口 Token 尚未填写，请先运行 setup.bat。')
            raise SystemExit(2)
        with startup_lock(ROOT) as acquired:
            if not acquired:
                print('已有启动窗口正在等待登录，请切换到那个窗口完成扫码。')
                return
            ok = asyncio.run(launch_services(ROOT, config))
        raise SystemExit(0 if ok else 1)
    except (OSError, ValueError):
        print('无法启动，请检查 config.json 和项目内的启动文件。')
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print('已取消等待；已经打开的程序窗口会保留。')
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
