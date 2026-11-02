# -*- coding: utf-8 -*-
"""Tailscale Funnel：由本机 tailscaled 提供固定 https://<node>.<tailnet>.ts.net。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


class TailscaleTunnel:
    def __init__(self, root: Path, data_dir: Path, origin: str, config: dict | None = None):
        config = config or {}
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.origin = origin
        self.executable = Path(str(config.get('tailscale_executable') or r'C:\Program Files\Tailscale\tailscale.exe'))
        self.url = str(config.get('tailscale_url') or '').rstrip('/')
        self.last_error = ''
        self._last_lookup = 0.0
        self._cache_seconds = 300

    def _derive_url(self):
        if self.url:
            return self.url
        if not self.executable.is_file():
            self.last_error = f'找不到 tailscale：{self.executable}'
            return ''
        try:
            raw = subprocess.check_output([str(self.executable), 'status', '--json'],
                                          cwd=str(self.root), stderr=subprocess.DEVNULL, timeout=10)
            data = json.loads(raw.decode('utf-8', 'replace'))
            dns = str((data.get('Self') or {}).get('DNSName') or '').rstrip('.')
            if dns:
                self.url = 'https://' + dns
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.last_error = f'Tailscale 状态读取失败：{exc}'
        return self.url

    def current_url(self) -> str:
        return self._derive_url()

    def ensure_started(self) -> bool:
        return bool(self._derive_url())

    def stop(self) -> None:
        pass
