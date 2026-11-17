#!/usr/bin/env python3
"""Watchdog: restart dead services, email state transitions."""
import asyncio
import datetime
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

# ---- 告警策略（2026-11-08 v2：少而准，只说人话） ----
# 设计目标：
#   1) 抖动不报：持续 hold 秒才发第一条，恢复后要稳定 clear_hold 秒才算恢复；
#   2) 同一件事只报一次：重复提醒间隔 repeat（默认 6 小时），恢复只对 P1 发一封；
#   3) P2（内存低、端口抖动等）不发“已恢复”，只在每天 09:00 的日报里汇总；
#   4) 23:00–08:00 只发 P1，其余进日报，避免半夜打扰；
#   5) 每条消息都写清：发生了什么 / 影响 / 你要做什么 / 不管会怎样。
STATE_VERSION = 2
DIGEST_HOUR = 9
QUIET_HOURS = (23, 8)          # [23:00, 08:00) 只发 P1

# WAL 停滞检测（2026-11-10）：稳态是 128MB（journal_size_limit），
# 超过 512MB 且 6 小时没回落，就一定是有读事务钉住了 checkpoint——比 2GB 那条兜底早得多。
WAL_SOFT_BYTES = 512 * 1024 ** 2
WAL_CLEAR_BYTES = 256 * 1024 ** 2
WAL_STALL_SECONDS = 6 * 3600
EVENT_LOG = ROOT / 'data' / 'logs' / 'alert-events.jsonl'
SEV1 = 'P1'   # 需要人处理
SEV2 = 'P2'   # 知道即可，可自愈

# 资源压力与发送失败（2026-11-17 补）：11-17 13:32 那次“机器人忽然没反应”，
# 可用内存 300–450MB、三个服务全 active、端口全在、/health 200，旧判据一条都不响；
# 真实信号是 PSI——13:32:52 起 memory full avg10 从 0.23 飙到 43.39→46.39（全体进程 46% 时间在等回收），
# 同期 ygo-qq 的 cgroup 从 6MB 被换到 220MB，NapCat 的 sendMsg 回调被拖到 8 秒超时。
# 所以这里看 PSI，不看内存绝对值（稳态 available 就在 350–420MB，按绝对值设阈值只会制造噪音）。
# 阈值 35 是按 11-17 全天数据定的：白天常态 full_avg10 在 15–25，凌晨偶发 36/48 单点，
# 只有 13:32–13:33 那次是 43.39→46.39 连续两跳。配合“连续两次巡检超标才报”（hold=60），
# 回放全天只会命中这一次，不产生噪音。
PSI_MEMORY_FULL10 = 35.0   # memory full avg10 超过它：全体进程已被内存回收拖住
PSI_IO_FULL10 = 35.0       # io full avg10 超过它：磁盘排队已经影响全体进程
QQ_DIAG_LOG = ROOT / 'data' / 'logs' / 'qq-diagnostics.jsonl'
QQ_SEND_WINDOW = 300       # 发送失败统计窗口（秒）
QQ_SEND_FAIL_ALERT = 3     # 窗口内发送超时达到这次数就报（本次 2 分钟内 3 次）

# 网页接口级观测（2026-11-17 补）：14:10–14:49 有 47 次 503、一条 matrix-cell 请求持全局查询锁 163 秒，
# 期间 ygo-web active、8787 端口在、/health 200、PSI 正常——旧判据因此全绿。
# /health 200 是结构性的：statistics_server.py:40 把 /health 和 /favicon.ico 直接短路返回，根本不碰库，
# 所以"数据库/全局锁"这一层的任何故障都不会体现在 /health 上。
# 真实信号只留在 data/logs/web-access.jsonl 里：status、elapsed_ms、lock_wait_ms、lock_hold_ms、lock_task。
# 这里按"用户实际看到了什么"判：报错次数、单请求耗时、等锁超时。只读日志尾部，不连库不发请求。
WEB_ACCESS_LOG = ROOT / 'data' / 'logs' / 'web-access.jsonl'
WEB_WINDOW = 600            # 统计窗口（秒）。窗口跨两次巡检，重复告警靠 repeat 压住，不靠短窗口
WEB_5XX_ALERT = 5           # 窗口内 5xx 达到这次数才算“有报错”（占比低于一半时按 P2 报）
WEB_5XX_SEVERE = 20         # 窗口内 5xx 达到这次数直接按 P1 报（11-16 白天最差 37 次/10 分钟）
WEB_SLOW_MS = 30000         # 单请求超过 30 秒：用户已经在盯着转圈
WEB_STALL_MS = 60000        # 单请求或单次持全局锁超过 60 秒：别人全在排队，按 P1 报
WEB_TIMEOUT_ALERT = 3       # 窗口内等锁超时达到这次数：全局查询锁被长期占住

CHECK_META = {
    'memory_low': {
        'sev': SEV2,
        'title': '内存快用完了',
        'impact': '网页和统计查询可能变慢；极端情况下整机会卡住，网页和 QQ 一起没反应。',
        'action': [
            '一般不用马上处理：系统自带内存泄压，必要时会自动重启网页服务。',
            '若连续 1 小时都低于 80MB，ssh 后运行 free -m 和 ps aux --sort=-rss | head，看是谁在占用。',
        ],
        'consequence': '继续恶化可能整机卡死，只能到阿里云控制台重启。外部探活会在完全无响应时另行通知。',
        'recovered': '可用内存已回到安全线以上',
    },
    'disk_high': {
        'sev': SEV1,
        'title': '磁盘快满了',
        'impact': '磁盘写满后采集和数据库写入会直接失败，网页也可能打不开。',
        'action': [
            'ssh 后运行 df -h /opt 看占用。',
            '删除 /opt/ygobot/data/backup 里超过 7 天的旧备份，保留最近 2 份。',
            '不要删录像目录和 bot.sqlite3。',
        ],
        'consequence': '磁盘满后采集中断，数据库可能损坏，恢复需要停机清理。',
        'recovered': '磁盘占用已降回安全线',
    },
    'metrics_stale': {
        'sev': SEV1,
        'title': '采集心跳停了',
        'impact': '新对局不会入库，网页统计会停在旧数据，QQ 查询也查不到新对局。',
        'action': [
            'ssh 后运行 systemctl status ygo-collector --no-pager -n 30 看退出原因。',
            '若报 database is locked，按 docs/topics/ALERTING_AND_WAL_20261106.md 处理 WAL；否则 restart ygo-collector。',
        ],
        'consequence': '停着期间的录像和对局不会自动补采。',
        'recovered': '采集心跳已恢复，新对局会继续入库',
    },
    'health_bad': {
        'sev': SEV1,
        'title': '网站健康检查失败',
        'impact': '网页可能打不开或一直转圈；如果只持续几十秒，通常只是后台在忙。',
        'action': [
            '通常不用做：多数情况 1 分钟内自愈，持续 10 分钟以上才会按“需要处理”再通知。',
            '若网页确实打不开，ssh 后运行 systemctl status ygo-web --no-pager -n 30。',
        ],
        'consequence': '持续失败时用户打不开网页；短时抖动没有实际影响。',
        'recovered': '网站健康检查已恢复正常',
    },
    'qq_login': {
        'sev': SEV1,
        'title': 'QQ 掉线了，需要扫码',
        'impact': '机器人在群里不会回话，订阅推送也会停。',
        'action': [
            '先确认没有人在电脑上登录同一个 QQ（同号互踢会掉线），这是第一嫌疑。',
            '等 10 分钟，再用同一部手机、同一网络扫码；不要连续扫。',
            '按 docs/core/QQ_OBSERVATION_PERIOD_RULES.md §4 的掉线 SOP 处理，不要换号。',
        ],
        'consequence': '机器人一直不回话，直到重新登录；观察期内不要用换号解决。',
        'recovered': 'QQ 已重新登录，收发恢复',
    },
    'qq_api': {
        'sev': SEV1,
        'title': 'QQ 接口连不上',
        'impact': '机器人收不到也发不出消息（NapCat / OneBot 链路断了）。',
        'action': [
            'ssh 后运行 docker ps | grep napcat 和 ss -lntp | grep 3001，确认容器与端口。',
            '观察期内不要直接重启 NapCat；先看 data/logs/qq-diagnostics.jsonl 和 data/qq-status.json。',
        ],
        'consequence': 'QQ 功能一直不可用，直到接口恢复。',
        'recovered': 'QQ 接口已恢复',
    },
    'qq_silent': {
        'sev': SEV1,
        'title': 'QQ 可能假在线',
        'impact': '表面看机器人“在线”，但实际收不到群消息，所有指令都不会响应。',
        'action': [
            '先看 data/qq-status.json 和 data/logs/qq-diagnostics.jsonl，确认最后一条消息时间。',
            '按 docs/core/QQ_OBSERVATION_PERIOD_RULES.md §4 处理；不要连扫、不要换号、不要升级版本。',
        ],
        'consequence': '所有 QQ 指令继续无响应，但监控上仍显示在线。',
        'recovered': 'QQ 消息收发已恢复',
    },
    'wal_stall': {
        'sev': SEV2,
        'title': '数据库临时文件一直没缩回去',
        'impact': '不影响数据正确性，但 WAL 会一直变大并占盘；每次写入都要往更长的文件追加，查询会慢慢变慢。',
        'action': [
            '先看 data/logs/wal-guard.jsonl：04:15 那次是 ok 还是 busy。',
            '一直 busy 多半是某个统计查询长时间占着读快照；重启 ygo-web 会让它立刻缩回 128MB'
            '（2026-11-10 15:42 实测 37 秒，全程不用手动 TRUNCATE）。',
            '重启前确认没在录：cat data/capture-status.json，看 active_observations 是否为 0。',
        ],
        'consequence': '按每小时 200–300MB 估算，几天内会把盘占满；届时采集与写入会直接失败。',
        'recovered': 'WAL 已回落到 128MB 附近',
    },
    'psi_memory': {
        'sev': SEV2,
        'title': '机器被内存回收拖住了',
        'impact': '所有进程都会卡顿几十秒到几分钟：网页查询变慢、QQ 消息发不出去，'
                  '用户看到的就是“机器人忽然没反应”。对局采集本身不丢数据。',
        'action': [
            '先看压力曲线：ls -t data/logs/memory-samples/*.jsonl | head -1 | xargs tail -n 10，'
            '看 psi_full_avg10 和 ygo-qq 的 cgroup 是否同时暴涨（QQ 进程被换出是典型特征）。',
            '再看后台任务：systemctl list-units "side-inference*" "ygobot-card-report*" --no-pager。'
            '这两个每十几分钟全扫一遍库，正好压在低内存上。',
            '通常 2–5 分钟自己回落；持续 10 分钟以上再考虑给定时任务错峰。'
            '不要为此重启 NapCat。',
        ],
        'consequence': '压力期间群里的指令会超时丢回复（send_group_msg 8 秒超时无法确认送达），'
                       '用户以为机器人坏了并重复发指令。',
        'recovered': '内存压力已回落',
    },
    'psi_io': {
        'sev': SEV2,
        'title': '磁盘排队太长',
        'impact': '所有读写都在排队，网页查询和 QQ 收发一起变慢。',
        'action': [
            'ssh 后运行 cat /proc/pressure/io，确认 full avg10 是否仍然高。',
            '多半是后台全库扫描（side-inference / card-report 预计算）叠加采集写入；'
            '等它跑完通常就回落，不需要动手。',
        ],
        'consequence': '持续高排队会把 QQ 发送回调拖到超时，群里表现为没反应。',
        'recovered': '磁盘压力已回落',
    },
    'qq_send_fail': {
        'sev': SEV1,
        'title': '消息发不出去（QQ 发送超时）',
        'impact': '群里发指令后没有回复，或回复只到一半。消息可能实际已送达，也可能真丢了，'
                  '机器人侧拿到的是超时，无法确认。',
        'action': [
            '先判断是不是整机被压住了：cat /proc/pressure/memory /proc/pressure/io，'
            '再看 data/logs/memory-samples/ 下最新那份采样。',
            '再看 data/logs/qq-diagnostics.jsonl：error_type=TimeoutError 是机器忙，'
            'retcode 非 0 才是 QQ 侧拒收。',
            '按踩坑记录 L35 的规矩不要自动重发（会重复）；确认真的没到再人工补发。',
        ],
        'consequence': '用户白等；同一指令反复重发还会刷屏。',
        'recovered': 'QQ 发送已恢复正常',
    },
    'web_5xx': {
        'sev': SEV1,
        'title': '网页接口大面积报错（503）',
        'impact': '统计页多个区块同时显示“暂时不可用”，用户点进去是空的或报错；'
                  'QQ 查询走另一条路径，一般不受影响。',
        'action': [
            '先看是谁在报错：tail -n 300 /opt/ygobot/data/logs/web-access.jsonl '
            '| grep \'"status": 50\' ，上面这条已经把接口名和占比写出来了。',
            '再看是不是锁竞争：同一批日志里 lock_timeout=true 或 lock_hold_ms 很大，'
            '说明有单个请求长期占着全局查询锁，其他接口全在排队等它（14:10–14:49 就是这种）。',
            '确认是锁问题就重启 ygo-web（会打断长查询、立刻放锁）：systemctl restart ygo-web；'
            '重启前先 cat data/capture-status.json 确认 active_observations 为 0。',
        ],
        'consequence': '统计页持续不可用；用户会反复刷新，请求越多排队越长，问题会自我放大。',
        'recovered': '网页接口已恢复正常返回',
    },
    'web_5xx_mild': {
        'sev': SEV2,
        'title': '网页接口有零星报错（503）',
        'impact': '统计页个别区块偶尔报错，刷新一下通常能出来。',
        'action': [
            '一般不用处理：看清上面写的接口名，若是 card-report / deck-naming 这类"正在计算"的接口，'
            '等后台算完就好。',
            '同一接口连续几小时都这样，再看 tail -n 300 data/logs/web-access.jsonl 里的 elapsed_ms，'
            '耗时接近超时上限就是锁排队，不是接口本身错。',
        ],
        'consequence': '用户偶尔要刷新一次；不影响数据正确性。',
        'recovered': '网页接口报错已减少',
    },
    'web_stall': {
        'sev': SEV1,
        'title': '网页有请求卡死超过 1 分钟',
        'impact': '这条请求占着全局查询锁期间，其他所有统计接口都在排队，最后一起超时返回 503，'
                  '页面上多处同时转圈。',
        'action': [
            '看这条日志的 lock_task 和 lock_hold_ms：持锁几十秒以上，就是单个重查询把全局锁占住了，'
            '不是机器慢（PSI 应该正常、available 也够）。',
            '短期：systemctl restart ygo-web 立刻放锁（确认 active_observations 为 0 再重启）。',
            '长期：把这个接口从全局锁里摘出去、加并发闸，别让它挡住别人。',
        ],
        'consequence': '卡住的那几十秒到几分钟里，所有统计接口一起 503，用户看到整页“暂时不可用”。',
        'recovered': '网页请求耗时已回落到正常范围',
    },
    'web_slow': {
        'sev': SEV2,
        'title': '网页有请求慢到几十秒',
        'impact': '这一条请求期间，同一时间进来的其他接口会在锁上排队，页面上会看到多处一起转圈。',
        'action': [
            '看日志里的 lock_hold_ms：持锁时间长说明是单个重查询占着全局锁，不是机器慢。',
            '通常自己会结束；同一接口反复出现就按“摘锁 / 加并发闸”处理。',
        ],
        'consequence': '偶发一两次影响不大；变成常态就说明有接口在长期占锁。',
        'recovered': '网页请求耗时已回落到正常范围',
    },
}



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



def load_state(path=None):
    try:
        return json.loads((path or STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    target = path or STATE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def issue_id(detail):
    """把一条人类可读的 issue 映射成稳定 id，状态和去重都按 id 走。"""
    if detail.startswith('可用内存低于 '): return 'memory_low'
    if detail.startswith('内存压力过高：'): return 'psi_memory'
    if detail.startswith('磁盘压力过高：'): return 'psi_io'
    if detail.startswith('QQ 发送失败：'): return 'qq_send_fail'
    if detail.startswith('网页接口大面积报错：'): return 'web_5xx'
    if detail.startswith('网页接口报错：'): return 'web_5xx_mild'
    if detail.startswith('网页接口卡死：'): return 'web_stall'
    if detail.startswith('网页接口卡住：'): return 'web_slow'
    if detail.startswith('磁盘使用 '): return 'disk_high'
    if detail.startswith('watch-metrics 已 '): return 'metrics_stale'
    if detail.startswith('8787 /health '): return 'health_bad'
    for unit in UNITS:
        if detail.startswith(unit + ' 未运行'): return 'unit_down:' + unit
    if detail.startswith('NapCat 容器未运行'): return 'unit_down:napcat'
    if detail.startswith('端口 3001'): return 'qq_api'
    if detail.startswith('端口 '):
        parts = detail.split()
        if len(parts) >= 2: return 'port:' + parts[1]
    if detail == QQ_LOGIN_ALERT: return 'qq_login'
    if detail == QQ_API_ALERT: return 'qq_api'
    if detail == QQ_SILENT_ALERT: return 'qq_silent'
    if detail.startswith('WAL 连续 '): return 'wal_stall'
    return detail


def describe(ident, detail=''):
    """返回这项检查的：级别、标题、影响、该做什么、不处理会怎样。"""
    meta = CHECK_META.get(ident)
    if meta:
        return meta
    if ident.startswith('unit_down:'):
        unit = ident.split(':', 1)[1]
        duty = {
            'ygo-collector': '负责观战采集和 8787 网页入口；停了以后新对局不会入库，网页可能打不开。',
            'ygo-web': '负责统计后台和查询；停了以后网页统计会报错。',
            'ygo-qq': '负责 QQ 机器人；停了以后群里不会回话，订阅推送也会停。',
            'napcat': 'QQ 容器；停了以后机器人收发全断。',
        }.get(unit, '这个服务负责的功能会停摆。')
        return dict(
            sev=SEV1, title='服务没有运行：' + unit,
            impact=duty + '系统已经尝试自动重启，但没有成功。',
            action=['ssh 后运行 systemctl status %s --no-pager -n 30 看退出原因。' % unit,
                    '再运行 systemctl restart %s。' % unit],
            consequence='功能一直停着，直到有人处理。',
            recovered='服务已恢复运行')
    if ident.startswith('port:'):
        port = ident.split(':', 1)[1]
        return dict(
            sev=SEV1, title='端口没有监听：' + port,
            impact='8787/8789 是网页入口，8788 是统计后台，3001 是 QQ 接口；对应功能会不可用。',
            action=['ssh 后运行 ss -lntp | grep %s，确认是哪个进程没起来。' % port,
                    '再看 systemctl status ygo-web ygo-collector --no-pager -n 30。'],
            consequence='对应功能一直不可用，直到进程恢复。',
            recovered='端口已恢复监听')
    return dict(
        sev=SEV2, title=(detail or ident)[:40],
        impact='这是自动巡检发现的一项异常，暂未归类。',
        action=['ssh 后运行 journalctl -u ygo-web -u ygo-collector -u ygo-qq -n 60 --no-pager 查看。'],
        consequence='若持续存在，可能影响网页或采集。',
        recovered='该项检查已恢复正常')


def per_ident(value, ident, default=0):
    """允许按 id / 前缀（unit_down:*）/ 通配符（*）配置不同阈值。"""
    if isinstance(value, dict):
        if ident in value:
            return value[ident]
        prefix = ident.split(':', 1)[0] + ':*'
        if prefix in value:
            return value[prefix]
        return value.get('*', default)
    return value


def fmt_time(ts):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))


def fmt_duration(seconds):
    minutes = max(0, int(seconds // 60))
    if minutes < 1: return '不到 1 分钟'
    if minutes < 60: return '%d 分钟' % minutes
    hours, rest = divmod(minutes, 60)
    return ('%d 小时 %d 分钟' % (hours, rest)) if rest else ('%d 小时' % hours)


def fmt_ms(value):
    """毫秒说成人话：163043 → 2 分 43 秒；用户关心的是“等了多久”，不是精确毫秒。"""
    if value is None:
        return '未知'
    seconds = max(0.0, float(value) / 1000.0)
    if seconds < 90:
        return '%.1f 秒' % seconds
    return '%d 分 %d 秒' % (int(seconds // 60), int(seconds % 60))


def human_bytes(value):
    if value is None: return '未知'
    if value >= 1024 ** 3: return '%.1fGB' % (value / 1024 ** 3)
    if value >= 1024 ** 2: return '%.0fMB' % (value / 1024 ** 2)
    return '%.0fKB' % (value / 1024)


def is_quiet(now):
    start, end = QUIET_HOURS
    hour = time.localtime(now).tm_hour
    return hour >= start or hour < end


def record_event(kind, ident, detail, now=None):
    """留一份可追溯的事件流水，日报和排查都靠它。"""
    row = {'ts': round(time.time() if now is None else now, 3),
           'kind': kind, 'id': ident, 'detail': detail}
    try:
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        pass


def migrate_state(state):
    """升级到 v2 时清掉旧事件的活跃标记，避免部署瞬间补发一堆“已恢复”。"""
    if state.get('_version') == STATE_VERSION:
        return state
    kept = {k: v for k, v in state.items() if k in ('_boot_id', '_last_digest')}
    kept['_version'] = STATE_VERSION
    if '_last_digest' not in kept:
        hour = time.localtime().tm_hour
        kept['_last_digest'] = time.strftime('%Y-%m-%d') if hour >= DIGEST_HOUR else ''
    return kept


def update_alerts(state, issues, now, hold=0, clear_hold=300, max_gap=None, repeat=None):
    """状态机：持续 hold 才报；恢复要稳定 clear_hold；同一件事 repeat 内不重复报。

    hold / clear_hold / repeat 可以传 int，也可以传 {id 或 '前缀:*' 或 '*': 秒}。
    """
    current = {issue_id(detail): detail for detail in issues}
    events = []
    for ident, detail in current.items():
        prior = state.get(ident) or {}
        gap = now - prior.get('last_seen', now)
        if prior.get('clear_since'):
            # 上一轮短暂恢复过：在 clear_hold 内又出现，算同一件事，不重复报。
            same = now - prior['clear_since'] <= max(clear_hold, max_gap or 0)
        else:
            same = bool(prior) and (max_gap is None or gap <= max_gap)
        if not same:
            prior = {}
        first = prior.get('first_seen', now) if prior else now
        active = bool(prior.get('active'))
        due = now - first >= per_ident(hold, ident)
        ts = prior.get('ts', 0)
        cooldown = per_ident(repeat, ident) if repeat is not None else ALERT_EVERY
        if due and (not active or now - ts >= cooldown):
            events.append(('alert', ident, detail, first))
            ts = now
            active = True
        state[ident] = dict(first_seen=first, last_seen=now, active=active,
                            ts=ts, detail=detail, clear_since=0)
    for ident in list(state):
        if ident.startswith('_') or ident in current:
            continue
        prior = state[ident]
        if not isinstance(prior, dict):
            state.pop(ident)
            continue
        if not prior.get('active'):
            state.pop(ident)
            continue
        clear_since = prior.get('clear_since') or now
        if now - clear_since >= per_ident(clear_hold, ident):
            events.append(('recovered', ident, prior.get('detail', ident),
                           prior.get('first_seen', clear_since)))
            state.pop(ident)
        else:
            prior['clear_since'] = clear_since
            prior['last_seen'] = now
    return events


def render_alert(ident, detail, first, now, host, observation=''):
    meta = describe(ident, detail)
    lines = ['【发生了什么】', detail, '', '【影响】', meta['impact'], '',
             '【需要你做什么】']
    for index, step in enumerate(meta['action'], 1):
        lines.append('%d. %s' % (index, step))
    lines += ['', '【不处理会怎样】', meta['consequence']]
    if ident in EXTRA:
        lines += ['', '【补充】', EXTRA[ident]]
    if observation:
        lines += ['', '【本次测量】', observation]
    lines += ['', '---',
              '检测时间：' + fmt_time(now),
              '已持续：' + fmt_duration(now - first) + '（首次发现 ' + fmt_time(first) + '）',
              '主机：' + host]
    return '\n'.join(lines)


def render_recovery(ident, detail, first, now, host):
    meta = describe(ident, detail)
    return '\n'.join([
        '【结论】',
        '此前的问题已经恢复，不需要你做任何事。',
        '',
        '【发生了什么】',
        '此前：' + detail,
        '恢复：' + (meta.get('recovered') or '检查已恢复正常'),
        '',
        '持续时间：' + fmt_duration(now - first) + '（' + fmt_time(first) + ' → ' + fmt_time(now) + '）',
        '主机：' + host,
        '',
        '若同一问题再次出现，会再发一封；否则不再打扰。',
    ])


def emit_events(events, host, now, observation=''):
    for kind, ident, detail, first in events:
        meta = describe(ident, detail)
        sev = meta['sev']
        if kind == 'recovered':
            # 恢复永远不打断人：夜间静默，P2 不进邮箱，只在日报里体现。
            if is_quiet(now) or sev == SEV2:
                record_event('self_healed', ident, detail, now)
                log('self_healed: ' + detail)
                continue
            subject = '[已恢复] ' + meta['title']
            body = render_recovery(ident, detail, first, now, host)
        else:
            if is_quiet(now) and sev == SEV2:
                record_event('quiet', ident, detail, now)
                log('quiet: ' + detail)
                continue
            subject = ('[需处理] ' if sev == SEV1 else '[留意] ') + meta['title']
            body = render_alert(ident, detail, first, now, host, observation)
        notify(subject, body)
        record_event(kind, ident, detail, now)
        log(kind + ': ' + detail)


def read_mem_available_mb():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_wal_state():
    """只读 WAL 文件大小与 -shm 头（不连库、不加锁，不影响任何进程）。

    -shm 头部：offset 14 是页大小（u16），offset 16 是 mxFrame（u32），
    offset 96 是 nBackfill（u32）。gap = 还没写回主库的帧数 × 页大小。
    """
    info = {'bytes': None, 'mx': None, 'nb': None, 'gap_bytes': None}
    try:
        info['bytes'] = (ROOT / 'data' / 'bot.sqlite3-wal').stat().st_size
    except OSError:
        return info
    try:
        with (ROOT / 'data' / 'bot.sqlite3-shm').open('rb') as handle:
            head = handle.read(20)
            handle.seek(96)
            backfill = handle.read(4)
    except OSError:
        return info
    if len(head) < 20 or len(backfill) < 4:
        return info
    page = int.from_bytes(head[14:16], 'little')
    mx = int.from_bytes(head[16:20], 'little')
    nb = int.from_bytes(backfill, 'little')
    if page in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536) and nb <= mx:
        info.update(mx=mx, nb=nb, gap_bytes=(mx - nb) * page)
    return info


def read_wal_bytes():
    return read_wal_state()['bytes']


def wal_stall_issue(state, now):
    """WAL 高水位连续 6 小时不回落 → checkpoint 被读者钉住了。

    正常一天会回落很多次（128MB）；只有长期不回落才报，且只发邮件（P2）。
    """
    info = read_wal_state()
    size = info['bytes']
    if size is None:
        return None
    track = state.get('_wal') or {}
    if size >= WAL_SOFT_BYTES:
        if not track.get('high_since'):
            track = dict(high_since=now)
        track['peak'] = max(track.get('peak') or 0, size)
        track['bytes'] = size
        track['gap_bytes'] = info['gap_bytes']
        track['checked_at'] = now
        state['_wal'] = track
        if now - track['high_since'] >= WAL_STALL_SECONDS:
            gap = info['gap_bytes']
            extra = ('；尚未写回主库 %s（典型的长读事务钉住）' % human_bytes(gap)) if gap else ''
            return ('WAL 连续 %s未回落（当前 %s，峰值 %s%s）'
                    % (fmt_duration(now - track['high_since']),
                       human_bytes(size), human_bytes(track['peak']), extra))
        return None
    if size < WAL_CLEAR_BYTES:
        state.pop('_wal', None)
    return None


def read_restarts():
    values = []
    for unit in UNITS:
        out = run(['systemctl', 'show', '-p', 'NRestarts', '--value', unit])
        values.append(out.stdout.strip() if out and out.returncode == 0 else '?')
    return values


WEB_CGROUP_PROCS = Path('/sys/fs/cgroup/system.slice/ygo-web.service/cgroup.procs')
DB_NAME = 'bot.sqlite3'


def read_web_db_fds():
    """ygo-web 进程里打开的 bot.sqlite3 连接数。

    正常 1–2 个；持续上涨说明某个读事务/游标没关掉——这正是 2026-11-10/11 WAL 被钉住
    （`nBackfill` 追不上 `mxFrame`、文件只涨不缩）的直接原因，所以放进观察行和日报。
    只读 `/proc/<pid>/fd`，不连库；pid 从 cgroup.procs 取，不额外起 systemctl 子进程。
    """
    try:
        pids = [int(value) for value in WEB_CGROUP_PROCS.read_text().split()]
    except (OSError, ValueError):
        return None
    if not pids:
        return None
    try:
        entries = list(Path('/proc/%d/fd' % pids[0]).iterdir())
    except OSError:
        return None
    total = 0
    for entry in entries:
        try:
            if entry.readlink().name == DB_NAME:
                total += 1
        except (OSError, RuntimeError):
            continue
    return total


def read_qq_state():
    try:
        data = json.loads((ROOT / 'data' / 'qq-status.json').read_text(encoding='utf-8'))
        return data.get('state') or '未知'
    except (OSError, ValueError):
        return '未知'


def read_psi(kind):
    """读 /proc/pressure/<kind>，返回 {'full_avg10': 43.4, ...}；内核没开 PSI 时返回 {}。"""
    result = {}
    try:
        text = (Path('/proc/pressure') / kind).read_text(encoding='utf-8')
    except OSError:
        return result
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        prefix = parts[0]          # some / full
        for token in parts[1:]:
            key, _, value = token.partition('=')
            try:
                result['%s_%s' % (prefix, key)] = float(value)
            except ValueError:
                continue
    return result


def web_issues(trouble):
    """把一次访问日志汇总转成告警文字列表；级别由 issue_id + describe 决定，这里只产事实。

    503 有很多含义（正在计算、暂无档案、真出错、等锁超时都可能是 503），
    所以按“次数 + 占比 + 耗时”分轻重：大面积报错、单请求卡死才是 P1。
    """
    if not trouble:
        return []
    found = []
    errors = trouble['errors']
    hits = sum(errors.values())
    share = hits / float(max(1, trouble['total']))
    if hits >= WEB_5XX_ALERT:
        names = '、'.join('%s %d 次' % (name, count) for name, count in
                          sorted(errors.items(), key=lambda kv: -kv[1])[:3])
        # 大面积的标准：次数够多，或者占比过半且样本不算太少。
        # 半夜低峰时"5 个请求错 4 个"占比很高但没什么人受影响，那种交给 P2 进日报，不半夜叫人。
        severe = hits >= WEB_5XX_SEVERE or (share >= 0.5 and hits >= 10 and trouble['total'] >= 30)
        detail = ('%s：最近 %d 分钟 %d 次 5xx（占这 %d 个请求的 %d%%；%s）'
                  % ('网页接口大面积报错' if severe else '网页接口报错',
                     WEB_WINDOW // 60, hits, trouble['total'], round(share * 100), names))
        if trouble['timeouts']:
            detail += '，其中 %d 次是等全局查询锁超时' % trouble['timeouts']
        found.append(detail)
    if trouble['slow']:
        top = trouble['slow'][0]
        worst = max(top['ms'], top.get('lock_hold') or 0)
        detail = ('%s：%s 单次请求用了 %s'
                  % ('网页接口卡死' if worst >= WEB_STALL_MS else '网页接口卡住',
                     top['path'], fmt_ms(top['ms'])))
        if top.get('lock_hold'):
            detail += '（同一请求持全局查询锁 %s，锁标记 %s）' % (
                fmt_ms(top['lock_hold']), top.get('lock_task') or 'unknown')
        if len(trouble['slow']) > 1:
            detail += '；期间另有 %d 个请求超过 %d 秒' % (len(trouble['slow']) - 1,
                                                      WEB_SLOW_MS // 1000)
        found.append(detail)
    return found


def recent_web_trouble(window=WEB_WINDOW, tail_bytes=2 * 1024 * 1024):
    """读 web-access.jsonl 尾部，汇总最近 window 秒的接口异常：5xx、慢请求、等锁超时。

    只读日志，不连库、不发 HTTP 探测，不给线上加任何负载。
    文件读不到时返回 None（当作“没有数据”，绝不据此判定恢复或故障）。
    """
    try:
        with WEB_ACCESS_LOG.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            raw = handle.read()
    except OSError:
        return None
    now = time.time()
    cutoff = now - window
    errors = {}
    slow = []
    timeouts = 0
    total = 0
    for line in raw.splitlines():
        if not line.startswith(b'{'):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = row.get('at')
        if not isinstance(stamp, str):
            continue
        try:
            when = datetime.datetime.strptime(stamp[:19], '%Y-%m-%dT%H:%M:%S').timestamp()
        except ValueError:
            continue
        # 只算“最近 window 秒”：早于窗口的不要，晚于当前时间 1 分钟以上的也不认（时钟跳变/回放）。
        if when < cutoff or when > now + 60:
            continue
        total += 1
        path = str(row.get('path') or '?')
        status = row.get('status')
        if isinstance(status, int) and status >= 500:
            errors[path] = errors.get(path, 0) + 1
        elapsed = row.get('elapsed_ms')
        if isinstance(elapsed, (int, float)) and elapsed >= WEB_SLOW_MS:
            slow.append(dict(path=path, ms=elapsed, lock_task=row.get('lock_task'),
                             lock_hold=row.get('lock_hold_ms'), at=stamp))
        if row.get('lock_timeout'):
            timeouts += 1
    slow.sort(key=lambda item: -item['ms'])
    return dict(total=total, errors=errors, slow=slow, timeouts=timeouts)


def recent_send_failures(window=QQ_SEND_WINDOW, tail_bytes=512 * 1024):
    """数最近 window 秒内 QQ 侧发送失败：api_error/send_* 的次数 + 判定未送达的指令数。

    只读 qq-diagnostics.jsonl 尾部，不碰数据库；文件读不到时返回 None（不误判为故障）。
    """
    try:
        with QQ_DIAG_LOG.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            raw = handle.read()
    except OSError:
        return None
    cutoff = time.time() - window
    errors = {}
    lifecycle = 0
    for line in raw.splitlines():
        if not line.startswith(b'{'):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = row.get('time')
        if not isinstance(stamp, (int, float)) or stamp < cutoff:
            continue
        if row.get('event') == 'api_error' and str(row.get('action') or '').startswith('send_'):
            action = row.get('action')
            errors[action] = errors.get(action, 0) + 1
        elif row.get('event') == 'command_lifecycle_complete' and row.get('outcome') == 'send_failure':
            lifecycle += 1
    return errors, lifecycle


def load_events(since):
    rows = []
    try:
        for line in EVENT_LOG.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get('ts', 0) >= since:
                rows.append(row)
    except OSError:
        pass
    return rows


def build_digest(state, now):
    """每天一封日报：结论先行，明细在后，明确写着“要不要我动手”。"""
    rows = load_events(now - 86400)
    alerts = [row for row in rows if row.get('kind') == 'alert']
    urgent = [row for row in alerts
              if describe(row.get('id', ''), row.get('detail', ''))['sev'] == SEV1]
    healed = [row for row in rows if row.get('kind') in ('recovered', 'self_healed')]
    quiet = [row for row in rows if row.get('kind') in ('quiet', 'quiet_recovered')]
    active = [key for key, value in state.items()
              if not key.startswith('_') and isinstance(value, dict) and value.get('active')]

    def count_by_title(items):
        counts = {}
        for row in items:
            meta = describe(row.get('id', ''), row.get('detail', ''))
            counts[meta['title']] = counts.get(meta['title'], 0) + 1
        return counts

    healed_counts = count_by_title(healed)
    alert_counts = count_by_title(alerts)

    mem = read_mem_available_mb()
    wal_info = read_wal_state()
    wal = wal_info['bytes']
    wal_note = ('（未写回主库 %s）' % human_bytes(wal_info['gap_bytes'])
                if wal_info.get('gap_bytes') else '')
    restarts = read_restarts()
    web_fds = read_web_db_fds()
    trouble = recent_web_trouble(30 * 60)
    if trouble:
        web_digest = ('%d 次' % sum(trouble['errors'].values()),
                      fmt_ms(trouble['slow'][0]['ms']) if trouble['slow'] else '正常')
    else:
        web_digest = ('读不到日志', '读不到日志')
    qq_state = read_qq_state()
    metrics_age = file_age(ROOT / 'data' / 'logs' / 'watch-metrics.jsonl')
    disk = shutil.disk_usage('/').free

    if active:
        subject = '[YGO 日报] 有 %d 件事需要留意' % len(active)
        verdict = '有 %d 件事还需要你留意（见最后一段）；其余正常。' % len(active)
    else:
        subject = '[YGO 日报] 一切正常，今天不需要你操作'
        verdict = '一切正常，今天不需要你操作。'

    lines = ['【结论】', verdict, '', '【过去 24 小时】']
    lines.append('· 发出的告警：%d 条（需处理 %d / 仅留意 %d）'
                 % (len(alerts), len(urgent), len(alerts) - len(urgent)))
    if healed_counts:
        detail = '、'.join('%s %d 次' % (title, count)
                           for title, count in sorted(healed_counts.items(), key=lambda kv: -kv[1]))
        lines.append('· 已自行恢复、无需处理：%s' % detail)
    else:
        lines.append('· 已自行恢复、无需处理：0 次')
    if quiet:
        lines.append('· 夜间静默（23:00–08:00）留到日报：%d 条' % len(quiet))
    lines += ['', '【当前状态】',
              '· 网页/接口：正常',
              '· 采集心跳：%s' % ('刚刚更新' if metrics_age < 120 else
                                   ('%d 分钟前更新' % (metrics_age // 60) if metrics_age < 3600 else '超过 1 小时未更新')),
              '· QQ：%s' % qq_state,
              '· 内存可用：%s' % ('%dMB（这台机常驻水位，系统会自动泄压）' % mem if mem is not None else '未知'),
              '· 磁盘可用：%s / WAL：%s%s' % (human_bytes(disk), human_bytes(wal), wal_note),
              '· 网页 DB 连接：%s' % (('%d 个（正常 1–2；持续上涨说明读事务没关，会把 WAL 钉大）' % web_fds)
                                 if web_fds is not None else '未知'),
              '· 网页接口：最近 30 分钟 5xx %s，最慢请求 %s' % (web_digest[0], web_digest[1]),
              '· 服务重启次数：web %s / collector %s / qq %s' % tuple(restarts)]
    if alert_counts:
        lines += ['', '【期间报过的事项】']
        for title, count in sorted(alert_counts.items(), key=lambda kv: -kv[1]):
            lines.append('· %s：%d 次' % (title, count))
    if active:
        lines += ['', '【仍需注意】']
        for key in active:
            meta = describe(key, state[key].get('detail', ''))
            duration = fmt_duration(now - state[key].get('first_seen', now))
            lines.append('· %s（已持续 %s）' % (meta['title'], duration))
            lines.append('  最近一次：' + state[key].get('detail', ''))
    lines += ['', '【说明】',
              '标题带【需处理】的邮件才需要你动手；【已恢复】和本报都不用处理。',
              '邮件按事件去重，同一问题 6 小时内不会反复发。']
    return subject, '\n'.join(lines)


def maybe_digest(state, now):
    if time.localtime(now).tm_hour < DIGEST_HOUR:
        return False
    today = time.strftime('%Y-%m-%d', time.localtime(now))
    if state.get('_last_digest') == today:
        return False
    subject, body = build_digest(state, now)
    notify(subject, body)
    state['_last_digest'] = today
    record_event('digest', 'digest', subject, now)
    log('digest: ' + subject)
    return True


def resources_main():
    global ALERT_EVERY
    cfg = load_env()
    minimum = int(cfg.get('HEALTH_MIN_AVAILABLE_MB', '80'))
    maximum = float(cfg.get('HEALTH_MAX_DISK_PERCENT', '85'))
    hold = max(0, int(cfg.get('HEALTH_RESOURCE_HOLD_SECONDS', '300')))
    ALERT_EVERY = max(60, int(cfg.get('HEALTH_REPEAT_SECONDS', '21600')))
    psi_limit_memory = float(cfg.get('HEALTH_PSI_MEMORY_FULL10', str(PSI_MEMORY_FULL10)))
    psi_limit_io = float(cfg.get('HEALTH_PSI_IO_FULL10', str(PSI_IO_FULL10)))
    # Read failures must not turn an existing resource alarm into a recovery.
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, _, value = line.partition(':')
        if key in ('MemAvailable', 'SwapTotal', 'SwapFree'):
            mem[key] = int(value.strip().split()[0])
    available = mem['MemAvailable']
    swap_used_mb = (mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)) // 1024
    usage = shutil.disk_usage('/')
    percent = usage.used / usage.total * 100
    path = ROOT / 'data' / 'resource-health-state.json'
    state = load_state(path)
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if state.get('_boot_id') != boot_id: state = {'_boot_id': boot_id}
    now = time.time()
    issues = []
    if available < minimum * 1024:
        issues.append('可用内存低于 %dMB（%d MB）' % (minimum, available//1024))
    if percent > maximum: issues.append('磁盘使用 %.0f%%' % percent)
    # PSI 量的是“全体进程因为等内存/磁盘停了多久”，比内存绝对值更接近用户感知的卡顿：
    # 11-17 13:32 那次 available 还有 300MB 以上，但 memory full avg10 已经到 46。
    psi_mem = read_psi('memory')
    psi_io = read_psi('io')
    mem_full10 = psi_mem.get('full_avg10')
    io_full10 = psi_io.get('full_avg10')
    if mem_full10 is not None and mem_full10 > psi_limit_memory:
        issues.append('内存压力过高：全体进程有 %.0f%% 的时间在等内存回收（swap 已用 %dMB）'
                      % (mem_full10, swap_used_mb))
    if io_full10 is not None and io_full10 > psi_limit_io:
        issues.append('磁盘压力过高：全体进程有 %.0f%% 的时间在等磁盘（swap 已用 %dMB）'
                      % (io_full10, swap_used_mb))
    wal_issue = wal_stall_issue(state, now)
    if wal_issue: issues.append(wal_issue)
    observation = '可用内存 %d MB，swap 已用 %dMB，磁盘使用 %.1f%%' % (available//1024, swap_used_mb, percent)
    if mem_full10 is not None:
        observation += '，PSI 内存 full %.1f（60s %.1f）' % (mem_full10, psi_mem.get('full_avg60', 0))
    if io_full10 is not None:
        observation += '，PSI 磁盘 full %.1f（60s %.1f）' % (io_full10, psi_io.get('full_avg60', 0))
    wal_info = read_wal_state()
    if wal_info['bytes']:
        observation += '，WAL %s' % human_bytes(wal_info['bytes'])
        if wal_info.get('gap_bytes'):
            observation += '（未写回 %s）' % human_bytes(wal_info['gap_bytes'])
    web_fds = read_web_db_fds()
    if web_fds is not None:
        observation += '，网页 DB 连接 %d 个' % web_fds
    if DRY:
        print(json.dumps(dict(issues=issues, observation=observation, hold_seconds=hold), ensure_ascii=False))
        return 0
    state = migrate_state(state)
    # 压力事件的窗口常常只有 2–3 分钟（本次 13:32–13:35，采样跨度 112 秒），
    # 所以 psi_memory 的 hold 压到 60 秒：定时器每 61 秒跑一次，等于"连续两次巡检都超标"才报，
    # 既不会把单次尖峰当故障，也不会像默认 300 秒那样整段漏掉。
    # psi_io 保留默认 hold，避免磁盘突发刷屏。
    events = update_alerts(state, issues, now,
                           hold={'memory_low': hold, 'disk_high': 0,
                                 'psi_memory': 60, 'psi_io': hold, '*': hold},
                           clear_hold={'memory_low': 600, 'disk_high': 3600, '*': 600},
                           max_gap=300,
                           repeat={'memory_low': 86400, 'disk_high': ALERT_EVERY,
                                   'psi_memory': 3600, 'psi_io': 3600, '*': ALERT_EVERY})
    emit_events(events, socket.gethostname(), now, observation)
    save_state(state, path)
    return 0


def main():
    global ALERT_EVERY
    ALERT_EVERY = max(60, int(load_env().get('HEALTH_REPEAT_SECONDS', '1800')))
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
        EXTRA[issue_id(qq_issue)] = detail
        issues.append(qq_issue)

    age = file_age(ROOT / 'data' / 'logs' / 'watch-metrics.jsonl')
    if age > 900:
        issues.append('watch-metrics 已 %.0f 分钟未更新' % (age / 60))

    # 发送失败是“用户已经感觉到坏了”的直接信号：11-17 13:32 那次群回复连续 8 秒超时，
    # 但 /health、端口、登录状态全绿，只有 qq-diagnostics 里留着记录。
    failures = recent_send_failures()
    if failures:
        errors, lifecycle = failures
        total = sum(errors.values())
        if total >= QQ_SEND_FAIL_ALERT or lifecycle >= 2:
            detail = 'QQ 发送失败：最近 %d 分钟 %d 次发送超时' % (QQ_SEND_WINDOW // 60, total)
            if errors:
                detail += '（' + '、'.join('%s %d 次' % (name, count)
                                          for name, count in sorted(errors.items())) + '）'
            if lifecycle:
                detail += '，另有 %d 条指令判定未送达' % lifecycle
            issues.append(detail)

    # 网页接口级：进程活着、端口通、/health 200，不代表用户能用。
    # 14:10–14:49 有 47 个 503、一条 matrix-cell 持全局查询锁 163 秒，以上判据全部是绿的，
    # 只有访问日志里留着 status / elapsed_ms / lock_hold_ms，所以这里直接读日志。
    issues.extend(web_issues(recent_web_trouble()))

    host = socket.gethostname()
    if DRY:
        print(json.dumps({'issues': issues, 'detail': EXTRA}, ensure_ascii=False, indent=2))
        return 0
    state = migrate_state(state)
    # Resource alarms now have their own timer/state; silently retire legacy keys.
    state = {key: value for key, value in state.items()
             if issue_id(key) not in ('memory_low', 'disk_high')}
    events = update_alerts(
        state, issues, now,
        hold={'unit_down:*': 0, 'qq_silent': 0, 'qq_login': 120, 'qq_api': 180,
              'metrics_stale': 120, 'port:*': 180, 'health_bad': 600,
              'qq_send_fail': 0, 'web_5xx': 0, 'web_stall': 0,
              'web_slow': 60, 'web_5xx_mild': 300, '*': 60},
        clear_hold=300,
        max_gap=300,
        repeat={'*': ALERT_EVERY, 'qq_send_fail': max(ALERT_EVERY, 3600),
                'web_5xx': 7200, 'web_stall': 3600,
                'web_slow': 3600, 'web_5xx_mild': 7200})
    emit_events(events, host, now)
    maybe_digest(state, now)

    save_state(state)
    return 0


if __name__ == '__main__':
    if '--resources' in sys.argv:
        DRY = '--dry-run' in sys.argv
        raise SystemExit(resources_main())
    if len(sys.argv) > 1 and sys.argv[1] in ('test-mail', 'test-notify'):
        ok = notify('[ygo] 测试通知', '通知通道可用，之后异常会推送到这里。')
        print('sent' if ok else 'failed')
        raise SystemExit(0 if ok else 1)
    if len(sys.argv) > 1 and sys.argv[1] in ('check', 'dry-run'):
        # 只体检不动手：打印当前问题，不重启服务、不发通知。
        DRY = True
        raise SystemExit(main())
    raise SystemExit(main())
