"""cpolar 免费隧道的运行时接线（候选实现，默认不启用）。

纯逻辑在 bot/cpolar_tunnel.py（命令、地址解析、模式、发布前自检）；这里只负责：
启动子进程、读输出拿地址、发布前自检、写状态文件、心跳。
对外接口与 bot.tunnel.QuickTunnel 保持一致（ensure_started / current_url / last_error / origin），
所以 service.py 可以按 share.tunnel_mode 直接切换，改动面只有构造那一处。

免费版只允许 1 条隧道：用 data/cpolar-tunnel.lock 防重复启动，状态文件里带上 origin/pid，
别的进程（拆分后的 QQ 进程）只读状态文件判断地址是否还新鲜，不重复启动。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from .cpolar_tunnel import command, hostname_from_line, public_url_ok
from .launcher import startup_lock

STATE_FILENAME = 'cpolar-state.json'
LOCK_FILENAME = 'cpolar-tunnel.lock'


class CpolarTunnel:
    """和 QuickTunnel 对齐的最小接口；不读配置、不决定模式（模式由 service 选）。"""

    def __init__(self, root: Path, data_dir: Path, origin: str, config: dict | None = None, *,
                 popen=None, check=public_url_ok, clock=time.monotonic,
                 wall=time.time, pause=time.sleep):
        config = config or {}
        parsed = urlsplit(str(origin))
        self.root, self.data_dir = Path(root), Path(data_dir)
        self.origin = origin
        self.port = int(parsed.port or 0)
        self.state_path = self.data_dir / STATE_FILENAME
        self.log_path = self.data_dir / 'logs' / 'cpolar.log'
        # 优先读配置；否则按"项目内 tools/cpolar/cpolar.exe → tools/cpolar.exe"找。
        # 本机客户端的 authtoken 已经配在 C:\Users\12783\.cpolar\cpolar.yml，
        # 子进程默认读同一个用户配置，不需要在 config.json 里再写 token。
        configured = str(config.get('cpolar_executable') or '').strip()
        if configured:
            self.executable = Path(configured)
        else:
            candidates = [self.root / 'tools' / 'cpolar' / 'cpolar.exe',
                          self.root / 'tools' / 'cpolar.exe']
            self.executable = next((path for path in candidates if path.is_file()), candidates[0])
        self.heartbeat_seconds = float(config.get('cpolar_heartbeat_seconds') or 90)
        self.start_timeout = float(config.get('cpolar_start_timeout_seconds') or 25)
        self.min_retry_seconds = float(config.get('cpolar_retry_seconds') or 30)
        self.popen = popen or subprocess.Popen
        self.check = check
        self.clock = clock
        self.wall = wall
        self.pause = pause
        self.last_attempt = float('-inf')
        self.last_error = ''
        self._process = None
        self._last_health_check = 0.0

    # ---- 状态文件 ----
    def _state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        if value.get('provider') != 'cpolar' or value.get('origin') != self.origin:
            return {}
        return value

    def _write_state(self, url: str, pid: int | None) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'provider': 'cpolar', 'origin': self.origin, 'url': url,
                   'pid': pid, 'updated_at': time.time()}
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.state_path)

    def _fresh(self, state: dict) -> bool:
        try:
            age = self.wall() - float(state.get('updated_at') or 0)
        except (TypeError, ValueError):
            return False
        return bool(state.get('url')) and 0 <= age <= self.heartbeat_seconds

    # ---- 对外接口 ----
    def current_url(self) -> str:
        """只读状态文件：拆分后 QQ 进程不启动隧道，只读 collector 写下的地址。"""
        state = self._state()
        if not self._fresh(state):
            return ''
        url = str(state.get('url') or '')
        return url if url.startswith('https://') else ''

    def ensure_started(self) -> bool:
        state = self._state()
        alive = self._process is not None and self._process.poll() is None
        if alive and self._fresh(state):
            # 自己还在跑：只按心跳刷状态，不再启动第二条（免费版只允许 1 条）。
            url = self.current_url()
            now = self.clock()
            if url and now - self._last_health_check >= 60:
                self._last_health_check = now
                if not self.check(url):
                    self.last_error = 'cpolar 公网自检失败，准备重连'
                    self._stop(self._process)
                    self._process = None
                    try:
                        self.state_path.unlink()
                    except OSError:
                        pass
                    self.last_attempt = float('-inf')
                    return False
            if self.wall() - float(state.get('updated_at') or 0) > max(5.0, self.heartbeat_seconds / 3):
                self._write_state(url or state.get('url', ''), self._process.pid)
            self.last_error = ''
            return bool(url)
        if not alive and self._fresh(state):
            # 另一进程起的隧道还新鲜：直接用，不抢占、不再开一条。
            self.last_error = ''
            return True
        if self.clock() - self.last_attempt < self.min_retry_seconds:
            return False
        self.last_attempt = self.clock()
        if not self.executable.is_file():
            self.last_error = ('找不到 cpolar 程序：' + str(self.executable)
                               + '（放到项目 tools/cpolar/cpolar.exe，或在 config.json 的 share.cpolar_executable 指定）')
            return False
        with startup_lock(self.root, LOCK_FILENAME) as acquired:
            if not acquired:
                self.last_error = '已有另一条 cpolar 隧道在启动（免费版只允许 1 条）。'
                return False
            latest = self._state()
            if self._fresh(latest):
                return True
            return self._start()

    def _start(self) -> bool:
        args = command(str(self.executable), self.port)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            output = self.log_path.open('ab')
            process = self.popen(args, cwd=str(self.root), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            self.last_error = '无法启动 cpolar：' + str(exc)
            return False
        deadline = self.clock() + self.start_timeout
        url = ''
        try:
            while self.clock() < deadline:
                line = process.stdout.readline()
                if line:
                    text = line.decode('utf-8', 'replace')
                    output.write(line)
                    output.flush()
                    found = hostname_from_line(text)
                    if found:
                        url = found
                        break
                    continue
                if process.poll() is not None:
                    break
                self.pause(0.2)
        finally:
            output.close()
        if not url:
            self.last_error = 'cpolar 没有给出公开地址；详见 data/logs/cpolar.log。'
            self._stop(process)
            return False
        if not self.check(url):
            self.last_error = 'cpolar 地址自检未通过（/health 不是 200）；先不发布。'
            self._stop(process)
            return False
        self._process = process
        self._write_state(url, process.pid)
        self.last_error = ''
        return True

    @staticmethod
    def _stop(process) -> None:
        try:
            if process.poll() is None:
                process.terminate()
        except OSError:
            pass
