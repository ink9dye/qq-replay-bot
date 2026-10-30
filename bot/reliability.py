"""减少重复请求与集中发送；不模拟真人，不自动处理账号验证。"""
import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from pathlib import Path


class AccountAttention(RuntimeError):
    pass


def restriction_hint(response):
    if not isinstance(response, dict) or response.get('retcode') in (None, 0):
        return ''
    text = ' '.join(str(response.get(k) or '') for k in ('message', 'wording', 'msg')).lower()
    words = ('账号冻结', '帐号冻结', '账号被封', '帐号被封', '发送频率过高', '发送消息受限',
             '禁止发送', '安全验证', '登录保护', '验证码', '人脸验证', '风控',
             'captcha', 'account banned', 'account frozen', 'rate limit')
    return 'QQ明确提示账号验证或发送受限，已暂停收发。请先在官方QQ处理，再运行 resume-qq.bat。' if any(w in text for w in words) else ''


class RequestGuard:
    def __init__(self, cooldown=0, duplicate_seconds=0, clock=None):
        self.cooldown, self.duplicate = max(0, cooldown), max(0, duplicate_seconds)
        self.clock = clock or time.monotonic
        self.last = OrderedDict()

    def accept(self, conversation, user, text):
        now, key = self.clock(), (str(conversation), str(user))
        signature = ' '.join(text.split())
        previous = self.last.get(key)
        if previous:
            when, old = previous
            if now - when < self.cooldown or (signature == old and now - when < self.duplicate):
                return False
        self.last[key] = (now, signature)
        self.last.move_to_end(key)
        while len(self.last) > 4000:
            self.last.popitem(last=False)
        return True


class OutboundGate:
    def __init__(self, interval=0, destination_interval=0, duplicate_seconds=0, limit=100, clock=None, pause=None):
        self.interval, self.destination_interval = max(0, interval), max(0, destination_interval)
        self.duplicate = max(0, duplicate_seconds)
        self.limit = max(1, int(limit))
        self.clock, self.pause = clock or time.monotonic, pause or asyncio.sleep
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.last_sent = None
        self.destinations, self.successes = OrderedDict(), OrderedDict()

    async def submit(self, destination, payload, send):
        if self.waiting >= self.limit:
            return False
        self.waiting += 1
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        key = (destination, digest)
        try:
            async with self.lock:
                now = self.clock()
                if key in self.successes and now - self.successes[key] < self.duplicate:
                    return True  # 同一结果已成功发送，不再发第二遍。
                wait = max((self.last_sent + self.interval - now) if self.last_sent is not None else 0,
                           self.destinations.get(destination, -1e12) + self.destination_interval - now, 0)
                if wait:
                    await self.pause(wait)
                # 无论此次是否成功都留下间隔，失败不会绕过节流。
                stamp = self.clock()
                self.last_sent = stamp
                self.destinations[destination] = stamp
                self.destinations.move_to_end(destination)
                result = await send()
                if result:
                    self.successes[key] = self.clock()
                    self.successes.move_to_end(key)
                while len(self.destinations) > 2000:
                    self.destinations.popitem(last=False)
                while len(self.successes) > 2000:
                    self.successes.popitem(last=False)
                return result
        finally:
            self.waiting -= 1


def write_state(path, state, reason='', **extra):
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'state': state, 'reason': reason, 'updated_at': time.time(), **extra},
                                    ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(target)
