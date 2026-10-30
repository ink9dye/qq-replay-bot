"""用 Windows 自带 SSH 管理 localhost.run 免费录像隧道。"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .launcher import startup_lock


def valid_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                and parsed.port is not None and 0 < parsed.port < 65536
                and not parsed.username and not parsed.password
                and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def valid_hostname(hostname: str) -> bool:
    return isinstance(hostname, str) and bool(re.fullmatch(r'[a-z0-9-]+\.(?:lhr\.life|localhost\.run)', hostname))


class QuickTunnel:
    def __init__(self, root: Path, data_dir: Path, origin: str, metrics_port: int = 47879):
        if not valid_origin(origin):
            raise ValueError('分享隧道只允许连接 127.0.0.1 的录像 HTTP 服务')
        self.root, self.data_dir = Path(root), Path(data_dir)
        self.origin, self.metrics_port = origin, int(metrics_port)
        if not 0 < self.metrics_port < 65536 or self.metrics_port == urlsplit(origin).port:
            raise ValueError('隧道状态端口无效或与录像服务冲突')
        self.state_path = self.data_dir / 'tunnel-state.json'
        self.executable = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32/OpenSSH/ssh.exe'
        self.log_path = self.data_dir / 'logs/tunnel.log'
        self.last_attempt = float('-inf')
        self.last_error = ''

    def _state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8'))
            if (value.get('origin') == self.origin and value.get('metrics_port') == self.metrics_port
                    and value.get('provider') == 'localhost.run' and value.get('instance')):
                return value
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def _get_status(self) -> dict:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{self.metrics_port}/status', timeout=1) as response:
            return json.loads(response.read(4096))

    def _status(self) -> dict:
        state = self._state()
        if not state:
            return {}
        try:
            status = self._get_status()
            if (status.get('instance') == state['instance'] and status.get('origin') == self.origin
                    and status.get('service') == 'qq-replay-tunnel'):
                return status
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {}

    def current_url(self) -> str:
        status = self._status()
        hostname = status.get('hostname', '')
        return 'https://' + hostname if status.get('ready') and valid_hostname(hostname) else ''

    def _metrics_open(self) -> bool:
        try:
            with socket.create_connection(('127.0.0.1', self.metrics_port), timeout=0.5):
                return True
        except OSError:
            return False

    def ensure_started(self) -> bool:
        if self._metrics_open():
            if self._status():
                self.last_error = ''
                return True
            self.last_error = '隧道状态端口被其他服务占用，请检查 data/logs/tunnel.log。'
            return False
        if time.monotonic() - self.last_attempt < 30:
            return False
        self.last_attempt = time.monotonic()
        if not self.executable.is_file():
            self.last_error = 'Windows OpenSSH 客户端未安装，免费公网分享不可用。'
            return False
        with startup_lock(self.root, 'quick-tunnel.lock') as acquired:
            if not acquired or self._metrics_open():
                return False
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            state = {'origin': self.origin, 'metrics_port': self.metrics_port,
                     'provider': 'localhost.run', 'instance': secrets.token_hex(16)}
            temporary = self.state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state), encoding='utf-8')
            os.replace(temporary, self.state_path)
            try:
                with self.log_path.open('ab') as output:
                    subprocess.Popen([
                        sys.executable, '-m', 'bot.ssh_tunnel', '--origin', self.origin,
                        '--metrics-port', str(self.metrics_port), '--data-dir', str(self.data_dir),
                        '--instance', state['instance'], '--ssh', str(self.executable),
                    ], cwd=str(self.root), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError:
                self.last_error = '免费隧道启动失败，请检查 data/logs/tunnel.log。'
                return False
            self.last_error = ''
            return True
