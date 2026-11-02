#!/usr/bin/env python3
"""Watchdog: restart dead services, email state transitions."""
import asyncio
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

# QQ 会话“假在线”检测：在线但长时间收不到任何群消息，通常是 QQ 端掉了、NapCat 还在报 online。
QQ_SILENT_ALERT = 'QQ 可能假在线（长时间没有新消息）'
QQ_LOGIN_ALERT = 'QQ 未登录（需要扫码）'
QQ_API_ALERT = 'QQ 接口无响应（OneBot 未连接）'
QQ_SILENT_HOURS = 4
QQ_ACTIVE_HOURS = (10, 24)
EXTRA = {}
DRY = False



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


def wait_active(unit, timeout=20):
    """systemctl restart 返回时服务可能仍在启动；给启动留出时间，避免误报。"""
    deadline = time.monotonic() + timeout
    while True:
        if active(unit):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            return True
    except OSError:
        return False


def wait_port(port, timeout=20):
    """服务刚重启时端口可能晚于 systemd active；避免把启动窗口误报为故障。"""
    deadline = time.monotonic() + timeout
    while True:
        if port_open(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def http_ok(url, timeout=5):
    """端口通不代表上游活着：nginx 在、share 挂了时 8787 仍是 502/端口通。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
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


def qq_probe(timeout=10):
    """问 OneBot：QQ 是否真在线、最近一条群消息是什么时候。

    返回 dict(login=bool, newest=float|None, groups=int)；接口不通返回 None。
    只做只读查询，不发送任何 QQ 消息。
    """
    try:
        sys.path.insert(0, str(ROOT))
        import watch  # 项目自带的 OneBot WS 客户端，模块级无副作用
    except Exception:  # noqa: BLE001
        return None
    try:
        cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    qq = cfg.get('qq') or {}
    url = qq.get('onebot_ws') or 'ws://127.0.0.1:3001'
    token = qq.get('access_token') or ''
    headers = {'Authorization': 'Bearer ' + token} if token else {}

    async def probe():
        client = watch.WebSocketClient(url)
        await asyncio.wait_for(client.connect(headers=headers), timeout=timeout)
        counter = {'n': 0}

        async def call(action, params):
            counter['n'] += 1
            echo = 'hc%d' % counter['n']
            await client.send_text(json.dumps({'action': action, 'params': params, 'echo': echo}))
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                try:
                    raw = await asyncio.wait_for(client.recv_text(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if payload.get('echo') == echo:
                    return payload

        try:
            login = await call('get_login_info', {})
            info = (login or {}).get('data') or {}
            if login is None or login.get('status') == 'failed' or not info.get('user_id'):
                return {'login': False, 'newest': None, 'groups': 0}
            listing = await call('get_group_list', {})
            groups = (listing or {}).get('data') or []
            newest = None
            for group in groups[:3]:
                gid = group.get('group_id')
                if not gid:
                    continue
                history = await call('get_group_msg_history', {'group_id': gid, 'count': 1})
                data = (history or {}).get('data') or {}
                messages = data.get('messages') if isinstance(data, dict) else data
                for message in messages or []:
                    stamp = message.get('time')
                    if isinstance(stamp, (int, float)) and (newest is None or stamp > newest):
                        newest = stamp
            return {'login': True, 'newest': newest, 'groups': len(groups)}
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        return asyncio.run(probe())
    except Exception:  # noqa: BLE001
        return None


def check_qq():
    """QQ 链路健康：在线、且最近确实有群消息进来（防“假在线”）。"""
    if not (docker_exists('napcat') and port_open(3001)):
        return []
    probe = qq_probe()
    if probe is None:
        return [(QQ_API_ALERT, 'OneBot 连接或查询超时；检查 napcat 容器、3001 端口和机器人进程。')]
    if not probe.get('login'):
        return [(QQ_LOGIN_ALERT, '账号不在线，需要在 127.0.0.1:6099 WebUI 扫码登录。')]
    newest = probe.get('newest')
    if newest:
        idle = time.time() - newest
        hour = time.localtime().tm_hour
        if idle > QQ_SILENT_HOURS * 3600 and QQ_ACTIVE_HOURS[0] <= hour < QQ_ACTIVE_HOURS[1]:
            return [(QQ_SILENT_ALERT,
                     '最近 %.1f 小时没有收到任何群消息（监控 %d 个群）。'
                     '通常是 QQ 端静默掉线、NapCat 仍报 online；需要重启 napcat 并扫码。'
                     % (idle / 3600, probe.get('groups', 0)))]
    return []



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
    restarted = False

    for unit in UNITS:
        if not unit_exists(unit):
            continue
        if not active(unit):
            if DRY:
                issues.append('%s 未运行' % unit)
                continue
            log('restart %s' % unit)
            run(['systemctl', 'restart', unit], timeout=40)
            restarted = True
            if not wait_active(unit):
                issues.append('%s 未运行，自动重启失败' % unit)
            else:
                log('%s restarted ok' % unit)

    if shutil.which('docker') and docker_exists('napcat') and not docker_running('napcat'):
        if DRY:
            issues.append('NapCat 容器未运行')
        else:
            log('restart napcat')
            run(['docker', 'restart', 'napcat'], timeout=60)
            time.sleep(8)
            if not docker_running('napcat'):
                issues.append('NapCat 容器未运行，自动重启失败')

    # 8787 由 nginx 监听，8789 才是 share 本体；两个都要在，nginx 在/上游死也算故障。
    for port in (8787, 8789, 8788):
        # 刚重启的进程可能还在加载（M3 初始化、卡表索引等），不能按立即探测判故障。
        if not (wait_port(port, 30) if restarted else port_open(port)):
            issues.append('端口 %d 未监听' % port)

    if not http_ok('http://127.0.0.1:8787/health'):
        issues.append('8787 /health 非 200（nginx 上游异常）')

    if docker_exists('napcat') and not port_open(3001):
        issues.append('端口 3001（OneBot）未监听')

    # QQ 假在线/未登录：端口和进程都正常，但消息实际进不来。
    for qq_issue, detail in check_qq():
        EXTRA[qq_issue] = detail
        issues.append(qq_issue)

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
    if DRY:
        print(json.dumps({'issues': issues, 'detail': EXTRA}, ensure_ascii=False, indent=2))
        return 0
    for issue in issues:
        prior = state.get(issue) or {}
        if now - prior.get('ts', 0) < ALERT_EVERY and prior.get('ts'):
            continue
        state[issue] = {'ts': now, 'active': True}
        body = issue + (('\n' + EXTRA[issue]) if EXTRA.get(issue) else '')
        notify('[ygo] 异常：' + issue.split('，')[0],
               body + '\n\n主机：' + host + '\n时间：' + time.strftime('%Y-%m-%d %H:%M:%S'))
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
    if len(sys.argv) > 1 and sys.argv[1] in ('check', 'dry-run'):
        # 只体检不动手：打印当前问题，不重启服务、不发通知。
        DRY = True
        raise SystemExit(main())
    raise SystemExit(main())
