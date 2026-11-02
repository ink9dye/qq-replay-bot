#!/usr/bin/env python3
"""Watchdog: restart dead services, email state transitions."""
import json
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from email.message import EmailMessage
from pathlib import Path

ROOT = Path('/opt/ygobot')
ENV = Path('/etc/ygobot/notify.env')
STATE = ROOT / 'data' / 'health-state.json'
LOG = ROOT / 'data' / 'logs' / 'health.log'
UNITS = ('ygo-collector', 'ygo-web', 'ygo-qq')
ALERT_EVERY = 1800


def log(message):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open('a', encoding='utf-8') as handle:
            handle.write(time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + message + '\n')
    except OSError:
        pass


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def load_env():
    cfg = {}
    if ENV.exists():
        for line in ENV.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            cfg[key.strip()] = value.strip()
    return cfg


def send_pushplus(subject, body, cfg):
    token = cfg.get('PUSHPLUS_TOKEN', '').strip()
    if not token:
        return False
    payload = json.dumps({
        'token': token,
        'title': subject,
        'content': body,
        'template': 'txt',
    }).encode('utf-8')
    request = urllib.request.Request(
        'https://www.pushplus.plus/send',
        data=payload,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
        if data.get('code') == 200:
            return True
        log('push rejected: %r' % (data,))
        return False
    except Exception as exc:  # noqa: BLE001
        log('push failed: %r' % (exc,))
        return False


def send_smtp(subject, body, cfg):
    user = cfg.get('SMTP_USER', '')
    password = cfg.get('SMTP_PASS', '')
    to = cfg.get('SMTP_TO', user)
    if not (user and password and to):
        log('mail skipped: notify.env missing')
        return False
    message = EmailMessage()
    message['From'] = user
    message['To'] = to
    message['Subject'] = subject
    message.set_content(body)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg.get('SMTP_HOST', 'smtp.qq.com'),
                              int(cfg.get('SMTP_PORT', '465')),
                              context=context, timeout=20) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001
        log('mail failed: %r' % (exc,))
        return False


def notify(subject, body):
    cfg = load_env()
    if cfg.get('PUSHPLUS_TOKEN', '').strip():
        return send_pushplus(subject, body, cfg)
    return send_smtp(subject, body, cfg)


def unit_exists(unit):
    out = run(['systemctl', 'cat', unit])
    return bool(out and out.returncode == 0)


def active(unit):
    out = run(['systemctl', 'is-active', unit])
    return bool(out and out.stdout.strip() == 'active')


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            return True
    except OSError:
        return False


def docker_running(name):
    out = run(['docker', 'inspect', '-f', '{{.State.Running}}', name])
    return bool(out and out.returncode == 0 and out.stdout.strip() == 'true')


def docker_exists(name):
    out = run(['docker', 'inspect', '-f', '{{.State.Status}}', name])
    return bool(out and out.returncode == 0)


def file_age(path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 10 ** 9


def load_state():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    state = load_state()
    now = time.time()
    issues = []

    for unit in UNITS:
        if not unit_exists(unit):
            continue
        if not active(unit):
            log('restart %s' % unit)
            run(['systemctl', 'restart', unit], timeout=40)
            time.sleep(5)
            if not active(unit):
                issues.append('%s 未运行，自动重启失败' % unit)
            else:
                log('%s restarted ok' % unit)

    if shutil.which('docker') and docker_exists('napcat') and not docker_running('napcat'):
        log('restart napcat')
        run(['docker', 'restart', 'napcat'], timeout=60)
        time.sleep(8)
        if not docker_running('napcat'):
            issues.append('NapCat 容器未运行，自动重启失败')

    for port in (8787, 8788):
        if not port_open(port):
            issues.append('端口 %d 未监听' % port)

    if docker_exists('napcat') and not port_open(3001):
        issues.append('端口 3001（OneBot）未监听')

    age = file_age(ROOT / 'data' / 'logs' / 'watch-metrics.jsonl')
    if age > 900:
        issues.append('watch-metrics 已 %.0f 分钟未更新' % (age / 60))

    usage = shutil.disk_usage('/')
    percent = usage.used / usage.total * 100
    if percent > 80:
        issues.append('磁盘使用 %.0f%%' % percent)

    mem = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('MemAvailable', 'MemTotal'):
                mem[key] = int(value.strip().split()[0])
    except OSError:
        pass
    if mem.get('MemAvailable', 10 ** 9) < 150 * 1024:
        issues.append('可用内存低于 150MB（%d MB）' % (mem.get('MemAvailable', 0) // 1024))

    host = socket.gethostname()
    for issue in issues:
        prior = state.get(issue) or {}
        if now - prior.get('ts', 0) < ALERT_EVERY and prior.get('ts'):
            continue
        state[issue] = {'ts': now, 'active': True}
        notify('[ygo] 异常：' + issue.split('，')[0],
               issue + '\n\n主机：' + host + '\n时间：' + time.strftime('%Y-%m-%d %H:%M:%S'))
        log('alert: ' + issue)

    for key, value in list(state.items()):
        if value.get('active') and key not in issues:
            value['active'] = False
            value['ts'] = now
            notify('[ygo] 已恢复：' + key.split('，')[0], key + '\n\n主机：' + host)
            log('recovered: ' + key)

    save_state(state)
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('test-mail', 'test-notify'):
        ok = notify('[ygo] 测试通知', '通知通道可用，之后异常会推送到这里。')
        print('sent' if ok else 'failed')
        raise SystemExit(0 if ok else 1)
    raise SystemExit(main())
