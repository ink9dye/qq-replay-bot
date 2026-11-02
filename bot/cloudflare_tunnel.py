# -*- coding: utf-8 -*-
"""Cloudflare Quick Tunnel（免费、无域名、固定到进程生命周期的 trycloudflare URL）。"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .cpolar_tunnel import public_url_ok

URL_PATTERN = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com', re.I)
STATE_FILENAME = 'cloudflare-state.json'
LOCK_FILENAME = 'cloudflare-tunnel.lock'
HEARTBEAT = 90.0


class CloudflareTunnel:
    def __init__(self, root: Path, data_dir: Path, origin: str, config: dict | None = None, *,
                 popen=None, check=public_url_ok, clock=time.monotonic, wall=time.time):
        config = config or {}
        self.root, self.data_dir, self.origin = Path(root), Path(data_dir), origin
        self.state_path = self.data_dir / STATE_FILENAME
        self.log_path = self.data_dir / 'logs' / 'cloudflare.log'
        self.executable = Path(str(config.get('cloudflare_executable') or r'D:\cloudflared\cloudflared.exe'))
        self.start_timeout = float(config.get('cloudflare_start_timeout_seconds') or 30)
        self.min_retry_seconds = float(config.get('cloudflare_retry_seconds') or 20)
        self.popen = popen or subprocess.Popen
        self.check = check
        self.clock = clock
        self.wall = wall
        self._process = None
        self.last_attempt = float('-inf')
        self.last_error = ''
        self._last_health_check = 0.0

    def _state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) and value.get('origin') == self.origin else {}

    def _fresh(self, state: dict) -> bool:
        try:
            age = self.wall() - float(state.get('updated_at') or 0)
        except (TypeError, ValueError):
            return False
        return bool(state.get('url')) and 0 <= age <= HEARTBEAT

    def current_url(self) -> str:
        state = self._state()
        url = str(state.get('url') or '')
        return url if self._fresh(state) and url.startswith('https://') else ''

    def _write_state(self, url: str, pid: int | None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'provider': 'cloudflare', 'origin': self.origin, 'url': url,
                   'pid': pid, 'updated_at': self.wall()}
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.state_path)

    def ensure_started(self) -> bool:
        state = self._state()
        alive = self._process is not None and self._process.poll() is None
        if alive and self._fresh(state):
            url = self.current_url()
            now = self.clock()
            if url and now - self._last_health_check >= 60:
                self._last_health_check = now
                if not self.check(url):
                    self.last_error = 'Cloudflare 公网自检失败，准备重连'
                    self._stop(self._process)
                    self._process = None
                    try:
                        self.state_path.unlink()
                    except OSError:
                        pass
                    self.last_attempt = float('-inf')
                    return False
            if self.wall() - float(state.get('updated_at') or 0) > HEARTBEAT / 3:
                self._write_state(url or str(state.get('url') or ''), getattr(self._process, 'pid', None))
            self.last_error = ''
            return bool(url)
        if not alive and self._fresh(state):
            return True
        if self.clock() - self.last_attempt < self.min_retry_seconds:
            return False
        self.last_attempt = self.clock()
        if not self.executable.is_file():
            self.last_error = f'找不到 cloudflared：{self.executable}'
            return False
        return self._start()

    def _start(self) -> bool:
        args = [str(self.executable), 'tunnel', '--url', self.origin, '--no-autoupdate', '--protocol', 'http2']
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            output = self.log_path.open('ab')
            process = self.popen(args, cwd=str(self.root), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            self.last_error = f'无法启动 cloudflared：{exc}'
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
                    found = URL_PATTERN.search(text)
                    if found:
                        url = found.group(0).rstrip('/')
                        break
                    continue
                if process.poll() is not None:
                    break
                time.sleep(0.2)
        finally:
            output.close()
        if not url:
            self.last_error = 'cloudflared 没有给出 trycloudflare 地址；详见 data/logs/cloudflare.log。'
            self._stop(process)
            return False
        healthy = False
        for _ in range(12):
            if self.check(url):
                healthy = True
                break
            time.sleep(2)
        if not healthy:
            self.last_error = 'Cloudflare 地址自检未通过（/health 不是 200）；先不发布。'
            self._stop(process)
            return False
        self._process = process
        self._write_state(url, process.pid)
        self.last_error = ''
        return True

    @staticmethod
    def _stop(process) -> None:
        try:
            if process and process.poll() is None:
                process.terminate()
        except OSError:
            pass
