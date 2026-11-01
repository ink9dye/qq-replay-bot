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
from .roles import lock_filenames

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


class StartupLock:
    """一个启动锁文件；关窗或进程退出时由 Windows 释放文件锁，不会留下僵尸锁。"""

    def __init__(self, root: Path, filename: str = 'startup.lock'):
        self.path = Path(root) / 'data' / filename
        self._handle = None
        self.acquired = False

    def acquire(self) -> bool:
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        self.acquired = True
        return True

    def release(self) -> None:
        handle, self._handle, self.acquired = self._handle, None, False
        if handle is None:
            return
        try:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


@contextmanager
def startup_lock(root: Path, filename: str = 'startup.lock'):
    """防止连续双击，在服务尚未绑定端口时启动出两个副本。"""
    lock = StartupLock(root, filename)
    try:
        yield lock.acquire()
    finally:
        lock.release()


@contextmanager
def startup_locks(root: Path, roles=None):
    """按角色占用启动锁。

    全开（roles=None 或 all）仍只占 bot-runtime.lock；
    拆分后 collector 占 collector.lock，QQ 占 bot.lock，两者互不阻塞。
    """
    locks = []
    try:
        for filename in lock_filenames(roles):
            lock = StartupLock(root, filename)
            if not lock.acquire():
                yield False
                return
            locks.append(lock)
        yield True
    finally:
        for lock in reversed(locks):
            lock.release()


_CONSOLE_HANDLERS = []


def set_console_title(title: str) -> bool:
    """给独立控制台窗口一个可区分的标题。"""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.SetConsoleTitleW(str(title)))
    except (AttributeError, OSError):
        return False


def install_close_handler(cleanup) -> bool:
    """关闭控制台窗口时先清理，再让系统结束进程。

    Ctrl+C 不在这里处理，仍走 KeyboardInterrupt 的原有退出路径。
    """
    if os.name != 'nt':
        return False
    try:
        import ctypes
        from ctypes import wintypes
        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handle(event):
            if event not in (2, 5, 6):  # CTRL_CLOSE_EVENT / LOGOFF / SHUTDOWN
                return False
            try:
                cleanup()
            except Exception:
                pass
            print('已停止', flush=True)
            return True

        handler = handler_type(handle)
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
        kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
        if not kernel32.SetConsoleCtrlHandler(handler, True):
            return False
        _CONSOLE_HANDLERS.append(handler)
        return True
    except (AttributeError, OSError, ValueError):
        return False


# 每个后台服务一个可见窗口，关窗即结束该服务。
# collector 是拆分时的备用入口（手工/诊断）；正常拆分由 PyCharm 固定 watch Run 承担观战。
ROLE_PROCESSES = (
    ('collector', 'bot.collector', 'collector.lock', '观战采集'),
    ('share', 'bot.share_service', 'share.lock', '分享'),
    ('qq', 'bot.qq_service', 'bot.lock', 'QQ机器人'),
)
def qq_lifecycle_mode(config: dict) -> str:
    """qq.lifecycle_mode：supervisor（默认）或 legacy；其它值按 supervisor。"""
    raw = str(((config or {}).get("qq") or {}).get("lifecycle_mode") or "supervisor").strip().lower()
    return "legacy" if raw == "legacy" else "supervisor"


def role_processes(config: dict):
    """按配置选择 QQ 入口；share/collector 不变。"""
    if qq_lifecycle_mode(config) == "legacy":
        qq = ("qq", "bot.qq_service", "bot.lock", "QQ机器人")
    else:
        qq = ("qq", "bot.qq_supervisor_service", "qq-supervisor.lock", "QQ运行窗口")
    return (
        ("collector", "bot.collector", "collector.lock", "观战采集"),
        ("share", "bot.share_service", "share.lock", "分享"),
        qq,
    )


_ROLE_PROCESS_HANDLES = []


def watch_roles(config: dict):
    """读 watch 角色：优先 data/watch-status.json（share 独立后 8787 只代表分享进程），8787 /health 兼容回退。"""
    from .watch_status import read as read_watch_status
    data_dir = Path(str((config or {}).get('data_dir') or (ROOT / 'data')))
    payload = read_watch_status(data_dir)
    roles = (payload or {}).get('roles')
    if isinstance(roles, list) and roles:
        return frozenset(str(role) for role in roles)
    share = config.get('share') or {}
    host = str(share.get('bind_host') or '127.0.0.1')
    if host in ('0.0.0.0', '::', 'localhost'):
        host = '127.0.0.1'
    port = int(share.get('bind_port', 8787))
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://{host}:{port}/health', timeout=1) as response:
            payload = json.loads(response.read(4096))
    except (OSError, ValueError, AttributeError):
        return None
    roles = payload.get('service_roles')
    if not isinstance(roles, list):
        return None
    return frozenset(str(role) for role in roles)


def role_process_running(root: Path, filename: str) -> bool:
    """角色锁已被占用就说明对应服务在跑；探测后立刻释放，不抢锁。"""
    probe = StartupLock(root, filename)
    if probe.acquire():
        probe.release()
        return False
    return True


def ensure_role_processes(root: Path, config: dict | None = None, *, popen=None, names=None):
    """只补缺：已占用角色锁或 8787 健康检查已就绪的服务跳过，不重启、不杀进程。

    names 限定要补的窗口；拆模式下 start-all 只负责 QQ，观战由 PyCharm 固定 watch Run 拉起。
    """
    if popen is None:
        popen = subprocess.Popen
    if config is not None and not role_process_running(root, 'collector.lock'):
        # 8787 已被本程序占用，但 collector 锁没被占：可能是原来的全开 watch。
        # 全开 watch 一个进程已经包含观战/分享/QQ，不能再起拆分进程去抢端口；
        # 但拆分后的观战进程只含 watch+share，它跑着不代表 QQ 也在跑，仍要补 QQ 窗口。
        if bot_state(config) == 'ready':
            roles = watch_roles(config)
            if roles is None or 'qq' in roles:
                print('检测到全开 watch 已运行（8787健康检查通过），拆分服务跳过。', flush=True)
                return [], []
            print('检测到拆分后的观战进程在运行（只含观战/分享），继续补起 QQ 窗口。', flush=True)
    wanted = None if names is None else tuple(names)
    started, failed = [], []
    for name, module, lock_name, title in role_processes(config):
        if wanted is not None and name not in wanted:
            continue
        if name == 'share':
            share_cfg = (config or {}).get('share') or {}
            if str(share_cfg.get('mode') or 'inline').strip().lower() != 'process':
                print('分享服务仍由 watch 进程内联提供（share.mode!=process），不单独启动。', flush=True)
                continue
        if name == "qq" and qq_lifecycle_mode(config) == "supervisor" and role_process_running(root, "bot.lock"):
            print("检测到旧版 QQ 服务仍在运行（bot.lock）；请先关闭旧 QQ 机器人窗口，supervisor 不双开。", flush=True)
            failed.append("qq")
            continue
        if role_process_running(root, lock_name):
            print(f'{title}服务已运行，跳过。', flush=True)
            continue
        try:
            process = popen([sys.executable, '-m', module], cwd=str(root),
                            env=dict(os.environ, PYTHONUTF8='1'),
                            creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
        except OSError as exc:
            failed.append(name)
            print(f'无法打开{title}服务窗口：{exc}', flush=True)
            continue
        _ROLE_PROCESS_HANDLES.append(process)
        started.append(name)
        print(f'已打开{title}服务窗口（python -m {module}）。', flush=True)
    return started, failed


def service_split_enabled(config: dict) -> bool:
    """config.json 的 service_mode=split 才拆进程；缺省 unified 保持原行为。"""
    return str((config or {}).get('service_mode') or '').strip().lower() == 'split'


def share_process_mode(config: dict) -> bool:
    """share.mode=process：分享/隧道交给独立 share_service，start-all 要负责补起 share 窗口。"""
    return str(((config or {}).get('share') or {}).get('mode') or 'inline').strip().lower() == 'process'


def split_role_names(config: dict) -> tuple:
    """拆分模式要补的角色：process 先 share 再 QQ；inline 仍只补 QQ（行为不变）。"""
    return ('share', 'qq') if share_process_mode(config) else ('qq',)


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
                          clock=time.monotonic, login_timeout=300, bot_timeout=25, attempt=False,
                          start_bot=None) -> bool:
    # 默认仍走 start.bat；拆分模式或测试可以传入自己的启动回调。
    if start_bot is None:
        def start_bot():
            spawn(root / 'start.bat')
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
        start_bot()
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
    start_bot()
    deadline = clock() + bot_timeout
    while clock() < deadline:
        if await asyncio.to_thread(state, config) == 'ready':
            print('启动完成。请保持 NapCat 和录像程序窗口开着。', flush=True)
            return True
        await pause(1)
    print('录像程序未能启动，请查看新打开的录像程序窗口里的提示。', flush=True)
    return False


def main() -> None:
    from . import pycharm_link
    watch_error = None
    role_failures = []
    try:
        config = read_config(ROOT / 'config.json')
        if not (config.get('qq') or {}).get('access_token'):
            print('接口 Token 尚未填写，请先运行 setup.bat。')
            raise SystemExit(2)
        split = service_split_enabled(config)
        start_called = [False]
        watch_requested = [False]

        def request_watch():
            # 观战只由 PyCharm 固定 watch Run 承担：拆分模式也不另开 collector 控制台窗口。
            watch_requested[0] = True
            return pycharm_link.launch(ROOT)

        if split:
            def start_bot():
                start_called[0] = True
                # 拆分模式：先补起 QQ 窗口，最后请求 PyCharm 运行固定 watch（观战+分享）。
                started, failed = ensure_role_processes(ROOT, config, names=split_role_names(config))
                role_failures.extend(failed)
                request_watch()
                return started
        else:
            # 全开模式：start-all 直接复用 pycharm_link.launch()，不再经 start.bat 中转；
            # start.bat 仍保留给手动启动。
            def start_bot():
                start_called[0] = True
                return request_watch()
        with startup_lock(ROOT) as acquired:
            if not acquired:
                print('已有启动窗口正在等待登录，请切换到那个窗口完成扫码。')
                return
            web_ready = ensure_statistics_web(ROOT)
            print('统计网页：'+('已就绪。' if web_ready else '未就绪，请检查8788端口和统计资料。'),flush=True)
            # 顺序：网页 → share（process）→ supervisor → 最后请求 PyCharm watch。
            if split and qq_lifecycle_mode(config) == "supervisor":
                started, failed = ensure_role_processes(ROOT, config, names=split_role_names(config))
                role_failures.extend(failed)
                start_called[0] = True
                ok = not failed
                if bot_state(config) != "ready":
                    try:
                        request_watch()
                    except (pycharm_link.IDEError, OSError) as exc:
                        if pycharm_link.watch_status(ROOT):
                            print(f"watch已在运行，未能打开PyCharm文件：{exc}", flush=True)
                        else:
                            watch_error = exc
                            print(f"未能启动PyCharm观战：{exc}", flush=True)
                print("supervisor 模式：QQ 运行窗口已按需补起；登录/掉线请查看该窗口，start-all 不代为登录。", flush=True)
            else:
                ok = asyncio.run(launch_services(ROOT, config, attempt=True, start_bot=start_bot))
            if split and qq_lifecycle_mode(config) != "supervisor":
                if not start_called[0]:
                    # 观战早已在跑时 launch_services 会提前返回：QQ 窗口仍按锁补起（重复启动会被拒）。
                    _started, failed = ensure_role_processes(ROOT, config, names=split_role_names(config))
                    role_failures.extend(failed)
                if bot_state(config) != 'ready' and not watch_requested[0]:
                    try:
                        request_watch()
                    except (pycharm_link.IDEError, OSError) as exc:
                        if pycharm_link.watch_status(ROOT):
                            print(f'watch已在运行，未能打开PyCharm文件：{exc}', flush=True)
                        else:
                            watch_error = exc
                            print(f'未能启动PyCharm观战：{exc}', flush=True)
                            print('未完成 PyCharm 自动运行授权，请手动在 PyCharm 运行 watch.py。', flush=True)
            elif ok and not start_called[0]:
                # 8787 已经显示 watch 在跑：launch() 只打开文件，不会再请求第二份。
                try:
                    request_watch()
                except (pycharm_link.IDEError, OSError) as exc:
                    if pycharm_link.watch_status(ROOT):
                        # watch 已经在跑，只是没法打开 IDE 文件；不能因此回滚或报启动失败。
                        print(f'watch已在运行，未能打开PyCharm文件：{exc}', flush=True)
                    else:
                        watch_error = exc
                        print(f'未能启动PyCharm观战：{exc}', flush=True)
                        print('未完成 PyCharm 自动运行授权，请手动在 PyCharm 运行 watch.py。', flush=True)
        if role_failures:
            print('部分服务窗口未能打开：' + '、'.join(role_failures), flush=True)
        if not ok:print('启动未全部完成。请按上方提示处理；窗口会保留。',flush=True)
        raise SystemExit(0 if ok and web_ready and not role_failures and watch_error is None else 1)
    except pycharm_link.IDEError as exc:
        # PyCharm 自动运行失败：网页、NapCat 等已经起来的服务不回滚、不杀进程。
        print(f'未能启动PyCharm观战：{exc}', flush=True)
        print('未完成 PyCharm 自动运行授权，请手动在 PyCharm 运行 watch.py。', flush=True)
        raise SystemExit(1) from None
    except (OSError, ValueError):
        print('无法启动，请检查 config.json 和项目内的启动文件。')
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print('已取消等待；已经打开的程序窗口会保留。')
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()


def qq_lifecycle_candidate(root, *, qq_health="missing", existing_napcat=False, enabled=True):
    """P14 候选入口：只生成 start-all 计划，不启动/不杀进程；尚未接入 main。"""
    from .qq_lifecycle import LifecycleState, plan_start
    state = LifecycleState.load(Path(root) / "data" / "qq-lifecycle.json")
    return plan_start(root, state, qq_health=qq_health,
                      existing_napcat=existing_napcat, enabled=enabled)
