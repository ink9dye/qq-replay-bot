# -*- coding: utf-8 -*-
"""启动后确保 Tailscale Funnel + 8787 公网入口可用；失败只报告，不阻塞本机服务。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_config():
    try:
        return json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def tailscale_exe(config):
    return Path(str(((config.get('share') or {}).get('tailscale_executable')
                     or r'C:\Program Files\Tailscale\tailscale.exe')))


def run(args, env=None):
    try:
        result = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=25, env=env)
        return result.returncode, (result.stdout or '') + (result.stderr or '')
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def auth_key_path(config):
    share = config.get('share') or {}
    return Path(str(share.get('tailscale_auth_key_file')
                     or (ROOT / 'data' / 'secrets' / 'tailscale-authkey.txt')))


def read_auth_key(config):
    path = auth_key_path(config)
    try:
        key = path.read_text(encoding='utf-8-sig').strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ''
    return key if key.startswith('tskey-auth-') else ''


def auth_key_notice(config):
    """返回 (是否即将过期/已过期, 提示)。meta 的 expires_at 是创建时记录的 90 天假设。"""
    path = auth_key_path(config)
    if not path.is_file():
        return False, ''
    meta_path = path.parent / 'tailscale-authkey.meta.json'
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        expires = float(meta.get('expires_at') or 0)
    except (OSError, ValueError, TypeError):
        return True, 'auth key 已配置，但没有到期日记录；请检查 data/secrets/tailscale-authkey.meta.json。'
    days = (expires - time.time()) / 86400
    if days <= 0:
        return True, 'Tailscale auth key 已过期，请重新生成并替换 data/secrets/tailscale-authkey.txt。'
    if days <= 14:
        return True, f'Tailscale auth key 将在 {days:.0f} 天后过期，请提前重新生成。'
    return False, f'Tailscale auth key 剩余约 {days:.0f} 天。'


def try_auth_key(exe, key):
    env = os.environ.copy()
    env['TS_AUTHKEY'] = key
    return run([str(exe), 'up', '--hostname=ink-y9000p'], env=env)


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def restart_service():
    return run(['powershell.exe', '-NoProfile', '-Command', 'Restart-Service Tailscale -Force'])


def tailscale_state(exe):
    code, raw = run([str(exe), 'status', '--json'])
    if code != 0:
        return code, 'unknown', False
    try:
        payload = json.loads(raw)
        return 0, str(payload.get('BackendState') or 'unknown'), bool((payload.get('Self') or {}).get('Online'))
    except ValueError:
        return 1, 'invalid', False


def healthy(url, attempts=4, delay=4):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for index in range(attempts):
        try:
            with opener.open(url.rstrip('/') + '/health', timeout=15) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        if index + 1 < attempts:
            time.sleep(delay)
    return False


def main():
    config = load_config()
    share = config.get('share') or {}
    exe = tailscale_exe(config)
    url = str(share.get('tailscale_url') or '').rstrip('/')
    log_path = ROOT / 'data' / 'logs' / 'public-startup.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [time.strftime('%Y-%m-%d %H:%M:%S')]
    key_expiring, key_message = auth_key_notice(config)
    if key_message:
        lines.append(key_message)

    def finish(code, message):
        lines.append(message)
        try:
            with log_path.open('a', encoding='utf-8') as handle:
                handle.write('\n'.join(lines) + '\n')
        except OSError:
            pass
        print(message)
        return code

    if not exe.is_file():
        return finish(1, f'公网启动检查失败：找不到 Tailscale：{exe}')
    code, backend, self_online = tailscale_state(exe)
    lines.append(f'Tailscale backend={backend} online={self_online}')
    if backend not in ('Running',) or not self_online:
        key = read_auth_key(config)
        if key:
            lines.append('Tailscale 未登录，尝试 auth key 自动登录')
            try_auth_key(exe, key)
            time.sleep(6)
            code, backend, self_online = tailscale_state(exe)
            lines.append(f'Tailscale auth 后 backend={backend} online={self_online}')
        if (backend not in ('Running',) or not self_online) and is_admin():
            lines.append('尝试重启 Tailscale 服务')
            restart_service()
            time.sleep(10)
            if key:
                try_auth_key(exe, key)
                time.sleep(5)
            code, backend, self_online = tailscale_state(exe)
            lines.append(f'Tailscale 修复后 backend={backend} online={self_online}')
        if backend not in ('Running',) or not self_online:
            hint = ('已配置 auth key 但仍失败。' if key else
                    '未配置 auth key；请运行 public-repair.cmd（UAC）或把 key 放到 data\\secrets\\tailscale-authkey.txt。')
            return finish(1, f'公网启动检查失败：Tailscale 状态 {backend}。{hint}')

    code, funnel = run([str(exe), 'funnel', 'status'])
    lines.append(funnel.strip()[:800])
    if code != 0 or 'Funnel on' not in funnel or (url and url not in funnel):
        code, output = run([str(exe), 'funnel', '--bg', '--yes', '8787'])
        lines.append(output.strip()[:800])
        if code != 0:
            return finish(1, '公网启动检查失败：Funnel 无法启用；请检查 Tailscale 登录/权限。')

    if not healthy('http://127.0.0.1:8787'):
        return finish(1, '公网启动检查失败：本地 8787 未就绪。')
    if url and not healthy(url, attempts=6, delay=5):
        return finish(1, f'公网启动检查失败：{url}/health 不通。')
    message = f'公网入口正常：{url or "未配置固定域名"}'
    if key_message:
        message += '；' + key_message
    return finish(3 if key_expiring else 0, message)


if __name__ == '__main__':
    raise SystemExit(main())
