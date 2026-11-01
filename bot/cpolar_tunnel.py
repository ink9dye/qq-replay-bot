"""cpolar 免费隧道的纯逻辑：命令、地址解析、模式选择与发布前自检。

只做"能独立测试"的部分，不启动进程、不读配置、不碰 service.py——
集成（谁在什么时候启动它）留到 M2 之后再接线，避免与此并行改动冲突。
"""
from __future__ import annotations

import json
import re
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

URL_PATTERN = re.compile(r'https://[a-z0-9][a-z0-9.-]*\.cpolar\.(?:cn|top|io)\b', re.I)
MODES = ('ssh', 'cpolar')


def command(executable, port, log_stdout=True):
    """cpolar 的启动命令；本地端口必须落在 1..65535。"""
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('invalid_port')
    args = [str(executable), 'http', str(port)]
    if log_stdout:
        args.append('-log=stdout')
    return args


def hostname_from_line(line):
    """从 cpolar 的 stdout 里取公开地址；没有就返回空串。"""
    match = URL_PATTERN.search(str(line or ''))
    return match.group(0).rstrip('/') if match else ''


def hostname_from_output(text):
    """取输出里最后一个地址：cpolar 重连后会打印新地址，旧的不能再用。"""
    found = URL_PATTERN.findall(str(text or ''))
    return found[-1].rstrip('/') if found else ''


def resolve_mode(config):
    """未配置时保持现在的 ssh 隧道，切换必须显式。"""
    value = str((config or {}).get('tunnel_mode') or 'ssh').strip().lower()
    if value not in MODES:
        raise ValueError('invalid_tunnel_mode')
    return value


def public_url_ok(url, timeout=8):
    """发布判定：地址必须能真的返回 /health 200，否则不写进分享链接。

    只用于本机自检，所以走直连、不读系统代理；链接会经过公网回环，
    这正是要验证的路径。
    """
    parts = urlsplit(str(url or ''))
    if parts.scheme != 'https' or not parts.hostname or parts.query or parts.fragment:
        return False
    target = url.rstrip('/') + '/health'
    request = Request(target, headers={'User-Agent': 'qq-replay-bot/tunnel-check'})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = response.read(4096)
    except (URLError, OSError, ValueError):
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get('status') == 'running'
