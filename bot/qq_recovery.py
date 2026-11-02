"""Only reconnect OneBot; never starts QQ, logs in, or handles authentication."""
import json
import math
import os
import subprocess
import time
from pathlib import Path
from .reliability import write_state

# 重连退避上限：NapCat 恢复后最多等 10 秒就能连上。
RETRY_DELAY_CAP = 10
# 重启后旧冷却最多保留 10 秒，不让上一个进程的长冷却拖住新进程。
RESTART_COOLDOWN_CAP = 10
# 连续失败上限，约 25 分钟（1+2+4+8 秒后每 10 秒一次）；到点停止自动重连，等人工确认。
GIVE_UP_AFTER_FAILURES = 150


def desktop_notice(kind):
    """Best-effort local balloon, fixed text only; does not block the event loop."""
    if os.name != 'nt' or os.environ.get('QQ_BOT_SUPPRESS_LOCAL_NOTICES') == '1':
        return
    messages = {
        'offline': 'QQ连接已中断，网页和观战继续。接口将有限重试，不会自动登录。',
        'attention': 'QQ账号离线或需要人工处理，请查看官方QQ或NapCat提示。可能自行恢复，机器人不会反复登录。',
        'online': 'QQ账号与接口已恢复在线。',
        'test': '这是本机连接提醒自检，不代表QQ离线，也不改变QQ登录状态。',
    }
    message = messages[kind]
    script = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$n=New-Object System.Windows.Forms.NotifyIcon;"
              "$n.Icon=[System.Drawing.SystemIcons]::Information;"
              "$n.Visible=$true;$n.BalloonTipTitle='QQ机器人连接状态';"
              f"$n.BalloonTipText='{message}';"
              "$n.ShowBalloonTip(8000);Start-Sleep -Seconds 9;$n.Dispose()")
    import base64
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                      '-EncodedCommand', encoded], creationflags=subprocess.CREATE_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RecoveryPolicy:
    def __init__(self, directory, clock=None, notify=None):
        self.path = Path(directory)/'qq-recovery.json' if directory else None
        self.clock, self.notify = clock or time.time, notify or desktop_notice
        self.failures, self.next_attempt_at, self.alert = 0, 0, ''
        self.offline_since, self.invalid = 0, False
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf8'))
                self.failures = data['failures']
                self.next_attempt_at = data['next_attempt_at']
                self.alert = data['alert']
                self.offline_since = data.get('offline_since', 0)
                now = self.clock()
                if self.next_attempt_at - now > RESTART_COOLDOWN_CAP:
                    self.next_attempt_at = now + RESTART_COOLDOWN_CAP
                if (type(self.failures) is not int or self.failures < 0
                        or self.alert not in ('', 'offline', 'attention')
                        or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0
                               for v in (self.next_attempt_at,self.offline_since))):
                    raise ValueError('invalid recovery state')
            except (OSError,ValueError,KeyError,TypeError):
                self.invalid = True

    def save(self):
        try:
            write_state(self.path, 'recovery', failures=self.failures,
                        next_attempt_at=self.next_attempt_at, alert=self.alert,
                        offline_since=self.offline_since)
        except OSError:
            self.invalid = True  # Without persistent cooldown, stop automatic attempts.

    def announce(self, kind):
        if kind == 'online':
            if not self.alert:
                return
            self.alert = ''
        else:
            if self.alert == kind or self.alert == 'attention':
                return
            self.alert = kind
        self.save()  # Persist before notification: restart must not repeat it.
        try:
            self.notify(kind)
        except OSError:
            pass

    def remaining(self):
        return max(0, self.next_attempt_at-self.clock())

    def failed(self):
        self.offline_since = self.offline_since or self.clock()
        self.failures += 1
        # 退避 1/2/4/8/10/10… 秒，上限 10 秒；仍只重连接口，不登录。
        delay = min(2**(self.failures-1), RETRY_DELAY_CAP)
        self.next_attempt_at = self.clock()+delay
        self.save()
        if self.clock()-self.offline_since >= 30 or self.failures >= 6:
            self.announce('offline')
        return delay

    def reset_retry(self):
        """人工确认后清掉“重试次数与冷却”，让下一轮循环立刻重连；不登录、不扫码。

        只重置重试预算，保留 alert/offline_since 这类故障事件与提醒去重状态：
        - 之后连接稳定时，stable() 仍能补发一次恢复提醒；
        - 之后仍然失败时，同一故障不会再提醒一次。
        """
        self.failures, self.next_attempt_at = 0, 0
        self.save()

    # 兼容既有调用名；语义同 reset_retry（不再清除 alert）。
    reset = reset_retry

    def stable(self):
        if not (self.failures or self.next_attempt_at or self.offline_since or self.alert):
            return
        self.failures, self.next_attempt_at, self.offline_since = 0, 0, 0
        self.announce('online')
        self.save()


async def verify_online(transport):
    echo = 'replay-bot-online-check'
    await transport.send_text(json.dumps({'action':'get_status','params':{},'echo':echo}))
    while True:
        raw = await transport.recv_text()
        if raw is None:
            raise ConnectionError('online check disconnected')
        data = json.loads(raw)
        if not isinstance(data,dict) or data.get('echo') != echo:
            continue
        return data.get('retcode') == 0 and (data.get('data') or {}).get('online') is True
