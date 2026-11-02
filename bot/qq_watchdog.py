# -*- coding: utf-8 -*-
"""QQ 接口看门狗：巡检进程/端口/心跳；异常受控恢复，不自动登录。"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'data' / 'qq-status.json'
LOG = ROOT / 'data' / 'logs' / 'qq-watchdog.log'
EVENTS = ROOT / 'data' / 'logs' / 'qq-watchdog-events.jsonl'
WATCH_STATE = ROOT / 'data' / 'qq-watchdog.json'
WIN_EVENTS = ROOT / 'data' / 'logs' / 'qq-windows-events.jsonl'
QQ_LOG = ROOT / 'data' / 'logs' / 'qq-service.log'
_LAST_ROLES = {}
_LAST_WIN_EVENT = 0.0


def log(message):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as handle:
        handle.write(line + '\n')


def event(kind, **fields):
    row = dict(time=datetime.now().astimezone().isoformat(timespec='seconds'), event=kind, **fields)
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def _ps_json(script):
    try:
        proc = subprocess.run(['powershell', '-NoProfile', '-Command', script],
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=15)
        raw = (proc.stdout or '').strip()
        if not raw:
            return []
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def processes():
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='NapCatWinBootMain.exe' "
        "OR Name='QQ.exe' OR Name='cloudflared.exe'\" | Select-Object Name,ProcessId,ParentProcessId,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    rows = _ps_json(script)
    roles = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get('Name') or '')
        cmd = str(row.get('CommandLine') or '')
        pid = row.get('ProcessId')
        role = None
        if 'bot.qq_service' in cmd:
            role = 'qq_service'
        elif 'qq_supervisor_service' in cmd:
            role = 'supervisor'
        elif name == 'NapCatWinBootMain.exe':
            role = 'napcat'
        elif 'share_service' in cmd:
            role = 'share'
        elif 'bot.launcher' in cmd:
            role = 'launcher'
        elif name == 'cloudflared.exe':
            role = 'cloudflared'
        if role and pid:
            roles.setdefault(role, []).append(int(pid))
    return roles


def port_open(port=3001):
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def qq_status():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def record_roles(roles):
    global _LAST_ROLES
    current = {role: tuple(sorted(pids)) for role, pids in roles.items()}
    for role in sorted(set(_LAST_ROLES) | set(current)):
        old, new = _LAST_ROLES.get(role, ()), current.get(role, ())
        if old == new:
            continue
        if old and not new:
            event('process_disappeared', role=role, pids=list(old))
            log(f'进程消失：{role} {list(old)}')
        elif new and not old:
            event('process_appeared', role=role, pids=list(new))
            log(f'进程出现：{role} {list(new)}')
        else:
            event('process_changed', role=role, old=list(old), new=list(new))
            log(f'进程变化：{role} {list(old)} -> {list(new)}')
    _LAST_ROLES = current


def windows_events():
    global _LAST_WIN_EVENT
    now = time.time()
    if now - _LAST_WIN_EVENT < 300:
        return
    _LAST_WIN_EVENT = now
    script = (
        "$since=(Get-Date).AddMinutes(-10);"
        "Get-WinEvent -FilterHashtable @{LogName='Application';StartTime=$since;Id=1000,1001,1026} "
        "-ErrorAction SilentlyContinue | Select-Object TimeCreated,Id,ProviderName,Message | "
        "ConvertTo-Json -Compress"
    )
    rows = _ps_json(script)
    for row in rows:
        if not isinstance(row, dict):
            continue
        with WIN_EVENTS.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def start_napcat():
    log('恢复：拉起 NapCat 登录窗口')
    subprocess.Popen(['cmd.exe', '/d', '/c', str(ROOT / 'start-napcat.bat')],
                     cwd=str(ROOT), stdin=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))


def start_qq_service():
    log('恢复：补起 bot.qq_service')
    output = QQ_LOG.open('a', encoding='utf-8', errors='replace')
    subprocess.Popen([sys.executable, '-m', 'bot.qq_service'], cwd=str(ROOT),
                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def check(restart=True, cooldown=600):
    roles = processes()
    record_roles(roles)
    windows_events()
    data = qq_status()
    state = data.get('state')
    heartbeat = data.get('last_heartbeat_at') or 0
    age = time.time() - float(heartbeat) if heartbeat else None
    qq_running = bool(roles.get('qq_service'))
    healthy = port_open(3001) and state == 'online' and age is not None and age < 180 and qq_running
    result = dict(time=datetime.now().astimezone().isoformat(timespec='seconds'),
                  port3001=port_open(3001), state=state, heartbeat_age=age,
                  roles={k: list(v) for k, v in roles.items()}, healthy=healthy)
    log('健康 ' + json.dumps(result, ensure_ascii=False) if healthy
        else '异常 ' + json.dumps(result, ensure_ascii=False))
    if healthy:
        WATCH_STATE.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result
    if not restart:
        result['action'] = 'alert-only'
        WATCH_STATE.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result
    last = 0.0
    try:
        last = float(json.loads(WATCH_STATE.read_text(encoding='utf-8')).get('recovery_at') or 0)
    except (OSError, ValueError):
        pass
    if time.time() - last < cooldown:
        result['action'] = 'cooldown'
    elif not result['port3001'] and not roles.get('napcat'):
        start_napcat()
        result['action'] = 'start_napcat'
        result['recovery_at'] = time.time()
    elif result['port3001'] and not qq_running:
        start_qq_service()
        result['action'] = 'start_qq_service'
        result['recovery_at'] = time.time()
    else:
        result['action'] = 'wait'
    WATCH_STATE.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def main():
    once = '--once' in sys.argv
    restart = '--no-restart' not in sys.argv
    log(f'watchdog 启动 restart={restart} pid={__import__("os").getpid()}')
    while True:
        try:
            check(restart=restart)
        except Exception:
            log('watchdog 异常：' + traceback.format_exc()[-2000:])
        if once:
            return
        time.sleep(60)


if __name__ == '__main__':
    main()
