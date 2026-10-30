"""一键启动：复用已运行的 NapCat，等登录成功，再启动录像程序。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .setup import probe_napcat, read_config
from .napcat_start import existing_napcat
from .account_guard import webui_login_id, AccountMismatch

ROOT = Path(__file__).resolve().parent.parent


def ensure_statistics_web(root, *, port=8788, popen=subprocess.Popen, pause=time.sleep):
    """Start the missing standalone web; never stop or replace an existing listener."""
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def status():
        try:
            with opener.open(f'http://127.0.0.1:{port}/health',timeout=1) as response:
                value=json.load(response)
            return 'ready' if value.get('service')=='qq-statistics-web' and value.get('status')=='running' else 'busy'
        except urllib.error.HTTPError:return 'busy'
        except (OSError,ValueError,AttributeError):
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=1):return 'busy'
            except OSError:return 'stopped'
    current=status()
    if current=='ready':return True
    if current=='busy':return False
    popen([sys.executable,'-m','bot.statistics_server','--root',str(root),'--port',str(port)],
        cwd=str(root),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(20):
        pause(.25)
        current=status()
        if current=='ready':return True
        if current=='busy':return False
    return False


@contextmanager
def startup_lock(root: Path, filename: str = 'startup.lock'):
    """防止连续双击，在服务尚未绑定端口时启动出两个副本。"""
    import msvcrt
    path = root / 'data' / filename
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


def request_qq_resume(data_dir: Path, expected: str = '') -> str:
    """写人工恢复信号；只让机器人重连本机接口，不登录、不扫码。

    不删除 qq-recovery.json：进程收到请求后只清重试冷却，
    故障事件与提醒去重状态保留，恢复后仍只提醒一次。
    """
    request = data_dir / 'qq-resume.request'
    temporary = request.with_name(request.name + '.tmp')
    request_id = uuid.uuid4().hex
    payload = {'request_id': request_id}
    if expected:
        payload['expected_self_id'] = str(expected)
    temporary.write_text(json.dumps(payload), encoding='utf8')
    temporary.replace(request)
    return request_id


def qq_status_state(data_dir: Path) -> str:
    try:
        return str(json.loads((data_dir / 'qq-status.json').read_text(encoding='utf8')).get('state') or '')
    except (OSError, ValueError, AttributeError):
        return ''


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


def spawn_batch(path: Path, *flags) -> None:
    if not path.is_file():
        raise OSError('启动文件不存在')
    if any(flag not in ('--attempt',) for flag in flags):raise ValueError('unsupported startup option')
    command = os.environ.get('COMSPEC', r'C:\Windows\System32\cmd.exe')
    # cmd /s /c 的外层引号保留带空格路径；路径来自本项目，不包含用户指令或密钥。
    arguments=(' '+ ' '.join(flags)) if flags else ''
    subprocess.Popen(f'"{command}" /d /s /c ""{path}"{arguments}"', cwd=str(path.parent),
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


def kill_napcat() -> None:
    """结束 NapCat 实例（含它启动的 QQ 子进程），不影响用户自己开的 QQ。"""
    try:
        subprocess.run(['taskkill', '/f', '/im', 'NapCatWinBootMain.exe'],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
        "Where-Object { $_.CommandLine -like '*--enable-logging*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(['powershell', '-NoProfile', '-Command', script],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def restart_napcat(root: Path) -> None:
    """结束旧 NapCat，再用快速登录重新打开。"""
    kill_napcat()
    spawn_batch(root / 'start-napcat.bat')


def kill_lagrange() -> None:
    """结束 Lagrange 进程。"""
    try:
        subprocess.run(['taskkill', '/f', '/im', 'Lagrange.OneBot.exe'],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def restart_lagrange(root: Path) -> None:
    """结束旧 Lagrange，再重新打开（自动重连登录）。"""
    kill_lagrange()
    spawn_batch(root / 'start-lagrange.bat')


async def launch_services(root: Path, config: dict, *, spawn=spawn_batch, probe=probe_napcat,
                          state=bot_state, port_open=ws_port_open, pause=asyncio.sleep,
                          clock=time.monotonic, login_timeout=300, bot_timeout=25, attempt=False) -> bool:
    status = await asyncio.to_thread(state, config)
    if status == 'busy':
        print('录像服务端口被占用，可能是旧版程序。请先关闭旧录像程序窗口，再双击 start-all.bat。', flush=True)
        return False
    data_dir = Path(config.get('data_dir') or root / 'data')
    was_paused=(data_dir / 'qq-paused.json').exists()
    if was_paused and attempt:
        print('本次主动尝试QQ连接：旧暂停记录不阻止打开NapCat；验证成功后才恢复机器人连接。',flush=True)
    if was_paused and not attempt:
        print('QQ处于暂停状态，不会自动登录。观战和网页可继续运行；处理官方QQ提示后请运行 resume-qq.bat。', flush=True)
        if status == 'ready':
            print('观战服务已运行；QQ仍处于暂停状态，不能称为全部启动成功。',flush=True)
            return False
        spawn(root / 'start.bat')
        deadline = clock() + bot_timeout
        while clock() < deadline:
            if await asyncio.to_thread(state, config) == 'ready':
                print('观战服务已运行；QQ仍处于暂停状态。',flush=True)
                return False
            await pause(1)
        return False
    if not await asyncio.to_thread(port_open, config):
        if await asyncio.to_thread(existing_napcat, root):
            print('NapCat实例已存在，等待你在现有窗口完成登录；不会重复打开或重启QQ。', flush=True)
        else:
            print('正在打开 NapCat。若出现二维码，请用工具号扫码并确认登录。', flush=True)
            if attempt:spawn(root / 'start-napcat.bat','--attempt')
            else:spawn(root / 'start-napcat.bat')
    else:
        print('NapCat 已在运行，正在检查工具号登录状态。', flush=True)
    deadline = clock() + login_timeout
    last_identity_check = None
    while clock() < deadline:
        expected = str((config.get('qq') or {}).get('expected_self_id') or '')
        if not attempt and expected and not await asyncio.to_thread(port_open, config) and (last_identity_check is None or clock() - last_identity_check >= 15):
            last_identity_check = clock()
            actual = await asyncio.to_thread(webui_login_id, root)
            if actual and actual != expected:
                print(str(AccountMismatch(expected, actual)), flush=True)
                return False
        ok, message = await probe(config, timeout=4)
        if ok:
            print('工具号已在线。', flush=True)
            if was_paused and attempt:
                try:running=json.loads((data_dir/'qq-status.json').read_text(encoding='utf8'))
                except (OSError,ValueError):running={}
                if expected and running.get('expected_self_id') not in (None,expected) and running.get('account_handoff_version')!=1:
                    print('当前QQ已在线，但运行中的机器人仍使用旧工具号。请在观战安全空档通过PyCharm固定watch Run加载一次换号支持；本次不重启观战。',flush=True)
                    return False
                # Existing running clients already consume this manual-resume signal.
                request_qq_resume(data_dir, expected)
                print('已核实工具号在线，已请求现有机器人恢复连接；不会重启观战。',flush=True)
            elif attempt and qq_status_state(data_dir) == 'cooldown':
                # 冷却中双击 start-all 也算人工处理：请求立即重连；故障提醒状态保留，恢复后只提醒一次。
                request_qq_resume(data_dir, expected)
                print('工具号在线，但机器人接口处于重连冷却；已请求立即恢复连接并保留故障提醒状态（约5秒内重连，可用 check-status.bat 确认；不会登录QQ）。',flush=True)
            break
        if 'Token 验证失败' in message:
            print(message, flush=True)
            return False
        if '登录账号错误' in message:
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
            web_ready=ensure_statistics_web(ROOT)
            print('统计网页：'+('已就绪。' if web_ready else '未就绪，请检查8788端口和统计资料。'),flush=True)
            ok = asyncio.run(launch_services(ROOT, config,attempt=True))
        if not ok:print('启动未全部完成。请按上方提示处理；窗口会保留。',flush=True)
        raise SystemExit(0 if ok and web_ready else 1)
    except (OSError, ValueError):
        print('无法启动，请检查 config.json 和项目内的启动文件。')
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print('已取消等待；已经打开的程序窗口会保留。')
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
