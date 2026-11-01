# -*- coding: utf-8 -*-
"""连接本机 NapCat：私聊指令，以及可选的群内 @ 指令。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from typing import Optional

from .watch_core import WebSocketClient
from .account_guard import AccountMismatch, verify_transport_account
from .reliability import OutboundGate, RequestGuard, AccountAttention, restriction_hint, write_state
from pathlib import Path
from .qq_diagnostics import Diagnostics, trace_id, exception_fields


def mark_stopped(state_dir, reason='QQ窗口已关闭'):
    """QQ 进程退出时把状态写成 stopped；保留其它字段，原子替换。

    只用于"进程要退出了"的收尾：不发消息、不碰网络；写失败就静默放弃。
    """
    if not state_dir:
        return
    path = Path(state_dir) / 'qq-status.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('invalid_status')
    except (OSError, ValueError):
        value = {}
    value.update(state='stopped', reason=reason, updated_at=time.time())
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class InterfaceReconnectRequested(ConnectionError):
    """Explicit local request, not a transport failure or login attempt."""


def connection_hint(exc: Exception) -> str:
    """只返回固定提示，不把响应原文、URL 参数或密钥写进日志。"""
    if isinstance(exc, AccountMismatch):
        return str(exc)
    if isinstance(exc, AccountAttention):
        return str(exc)
    if "401" in str(exc) or "403" in str(exc):
        return "Token 验证失败，请使用 Websocket Server 的 Token，而非网页登录密码。"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "连接或响应超时，请检查 NapCat 是否运行、工具号是否在线。"
    if isinstance(exc, ConnectionRefusedError):
        return "连接被拒绝，请启动 NapCat 并启用 127.0.0.1:3001 的 WebSocket 服务端。"
    return "连接断开或无法建立，请检查 NapCat 和接口设置。"


class NapCatTransport:
    def __init__(self, url: str, token: str):
        if any(char in (token or "") for char in "\r\n"):
            raise RuntimeError("接口 Token 无效")
        self.url = url
        self.token = token
        self.client: Optional[WebSocketClient] = None

    async def connect(self) -> None:
        await self.close()
        self.client = WebSocketClient(self.url)
        try:
            await self.client.connect({"Authorization": "Bearer " + self.token})
        except BaseException:
            await self.close()
            raise

    async def recv_text(self) -> Optional[str]:
        if self.client is None:
            return None
        return await self.client.recv_text()

    async def send_text(self, text: str) -> None:
        if self.client is None:
            raise ConnectionError("尚未连接")
        await self.client.send_text(text)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None


def event_text(event: dict) -> str:
    message = event.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for segment in message:
            if isinstance(segment, dict) and segment.get("type") == "text":
                data = segment.get("data") or {}
                parts.append(str(data.get("text") or ""))
        return "".join(parts)
    raw = event.get("raw_message")
    return raw if isinstance(raw, str) else ""


def group_command_text(event: dict, allow_commands: bool = False, extra_accept=None) -> Optional[str]:
    """支持明确指令免@；普通聊天、仅@别人或@全体不触发。"""
    self_id = str(event.get("self_id") or "")
    group_id = str(event.get("group_id") or "")
    if not self_id.isdigit() or not group_id.isdigit() or int(group_id) <= 0:
        return None
    message = event.get("message")
    if isinstance(message, list):
        mentioned = any(
            isinstance(segment, dict) and segment.get("type") == "at"
            and str((segment.get("data") or {}).get("qq")) == self_id
            for segment in message
        )
        if mentioned:
            return event_text(event).strip() or "帮助"
        if any(isinstance(segment, dict) and segment.get('type') == 'at' for segment in message):
            return None
    if isinstance(message, str):
        at = re.compile(r"\[CQ:at,qq=([0-9]+|all)(?:,[^\]]*)?\]")
        if any(match.group(1) == self_id for match in at.finditer(message)):
            return at.sub("", message).strip() or "帮助"
        if at.search(message):
            return None
    if allow_commands:
        from .commands import parse_command
        text = event_text(event).strip()
        head = text.split()[0] if text.split() else ''
        # 卡价预定只在被@时出现，不参与免@指令，也不在帮助里列出。
        if head in ('预定', '我的预定', '取消预定'):
            return None
        if parse_command(text).get('op') not in ('empty', 'unknown'):
            return text
        if extra_accept and extra_accept(event, text):
            return text
    return None


class OneBotClient:
    def __init__(self, url: str, token: str, on_event, transport=None, pause=None, timeout: float = 8, on_status=None, accept_group_messages: bool = False, expected_self_id=None, account_probe=None, allow_unmentioned_commands: bool = False, group_extra_accept=None, send_interval=0, destination_interval=0, duplicate_reply_seconds=0, request_cooldown=0, duplicate_command_seconds=0, state_dir=None, read_timeout=75):
        self.url = url
        self.token = token
        self.on_event = on_event
        self.transport = transport if transport is not None else NapCatTransport(url, token)
        self.pause = pause or asyncio.sleep
        self.timeout = timeout
        self.disconnects = 0
        self.connected = False
        self._stopped = False
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._pending: dict[str, asyncio.Future] = {}
        self._echo = 0
        self.on_status = on_status
        self.accept_group_messages = accept_group_messages
        self.allow_unmentioned_commands = allow_unmentioned_commands
        self.group_extra_accept = group_extra_accept
        self.expected_self_id = str(expected_self_id or '')
        self.account_probe = account_probe
        self._dispatch_tasks: set[asyncio.Task] = set()
        self.request_guard = RequestGuard(request_cooldown, duplicate_command_seconds)
        self.outbound = OutboundGate(send_interval, destination_interval, duplicate_reply_seconds, pause=self.pause)
        self.state_dir = Path(state_dir) if state_dir else None
        self.suspended = bool(self.state_dir and (self.state_dir / 'qq-paused.json').exists())
        self.pause_reason = '已暂停收发，请处理QQ限制后运行 resume-qq.bat。' if self.suspended else ''
        self.read_timeout = read_timeout
        self._published_state = None
        self._published_at = 0
        self.last_heartbeat_at = None
        self.last_message_at = None
        self.last_resume_request_id = None
        from .qq_recovery import RecoveryPolicy
        self.recovery = RecoveryPolicy(self.state_dir)
        self._online_since = None
        self._connection_clock = time.monotonic
        self.diagnostics = Diagnostics(self.state_dir)
        self._diagnostic_heartbeat_at = 0
        self.received_messages = 0
        self.handled_commands = 0
        self.diagnostics.record('runtime_start', group_enabled=accept_group_messages,
                                unmentioned_enabled=allow_unmentioned_commands)
        if self.recovery.invalid:
            self.suspended = True
            self.pause_reason = 'QQ恢复状态损坏，已停止自动尝试，请检查本机qq-recovery.json。' 

    def publish_state(self, state, reason=''):
        signature = (state, reason, self.outbound.waiting, self.recovery.failures, self.recovery.next_attempt_at, self.last_resume_request_id)
        now = time.time()
        previous_state = self._published_state[0] if self._published_state else None
        if state != previous_state:
            self.diagnostics.record('connection_state', state=state, failures=self.recovery.failures,
                                    retry_seconds=max(0, self.recovery.next_attempt_at - now))
        if signature == self._published_state and now - self._published_at < 15:
            return
        try:
            write_state(self.state_dir / 'qq-status.json' if self.state_dir else None, state, reason,
                        expected_self_id=self.expected_self_id, queued=self.outbound.waiting,account_handoff_version=1,
                        recovery_version=1, reconnect_failures=self.recovery.failures,
                        connection_control_version=2, process_id=os.getpid(),
                        last_heartbeat_at=self.last_heartbeat_at, last_message_at=self.last_message_at,
                        last_resume_request_id=self.last_resume_request_id,
                        diagnostics_version=1, diagnostics_session=self.diagnostics.session,
                        next_attempt_at=self.recovery.next_attempt_at)
            self._published_state = signature
            self._published_at = now
        except OSError:
            pass

    async def pause_for_attention(self, reason):
        self.suspended, self.connected, self.pause_reason = True, False, reason
        try:
            write_state(self.state_dir / 'qq-paused.json' if self.state_dir else None, 'manual_attention', reason)
        except OSError:
            pass
        self.recovery.announce('attention')
        self.publish_state('manual_attention', reason)
        if self.on_status:
            self.on_status(reason)
        self._fail_pending(AccountAttention(reason))
        await self._close_transport()

    def consume_resume_request(self):
        if not self.state_dir or not (self.state_dir / 'qq-resume.request').exists():
            return False
        if self.recovery.invalid:
            # 持久状态损坏时，普通恢复请求不能绕过“停止自动尝试”的保护。
            return False
        request=self.state_dir/'qq-resume.request'
        try:
            text=request.read_text(encoding='utf8').strip()
            payload=json.loads(text) if text else {}
            expected=payload.get('expected_self_id') if isinstance(payload,dict) else None
            request_id = payload.get('request_id') if isinstance(payload, dict) else None
            if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{32}', request_id)):
                return False
            if expected is not None:
                if not isinstance(expected,str) or not expected.isdigit() or not 5<=len(expected)<=20:return False
                self.expected_self_id=expected
        except (OSError,ValueError):return False
        (self.state_dir / 'qq-resume.request').unlink(missing_ok=True)
        (self.state_dir / 'qq-paused.json').unlink(missing_ok=True)
        self.suspended, self.pause_reason = False, ''
        self.last_resume_request_id = request_id
        self.diagnostics.record('manual_resume')
        # 只清重试冷却；故障事件与提醒去重保留，恢复后仍只提醒一次。
        self.recovery.reset_retry()
        self.publish_state('reconnecting', '已收到手动恢复请求，只重连接口，不自动登录QQ。')
        return True

    async def queued_send(self, kind, receiver, body, callback):
        if self.suspended or not self.connected:
            self.diagnostics.record('reply_skipped', reason='not_connected', kind=kind)
            return False
        async def guarded():
            return False if self.suspended or not self.connected else await callback()
        self.diagnostics.record('reply_queued', kind=kind, queued=self.outbound.waiting)
        result = await self.outbound.submit((kind, str(receiver)), body, guarded)
        self.diagnostics.record('reply_complete', kind=kind, success=bool(result))
        return result

    def stop(self) -> None:
        self._stopped = True
        closer = getattr(self.transport, "close", None)
        if closer is not None:
            result = closer()
            if asyncio.iscoroutine(result):
                asyncio.get_running_loop().create_task(result)

    def _remember(self, message_id) -> bool:
        if message_id is None:
            return False
        key = str(message_id)
        if key in self._seen:
            return True
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > 4000:
            self._seen.discard(self._seen_order.popleft())
        return False

    async def run(self) -> None:
        delay = 1.0
        if self.state_dir:
            if self.recovery.invalid:
                # 状态损坏时既不消费请求也不删除它：保护优先，文件留给人工排查。
                pass
            elif self.suspended:
                # 重启不能自动解除“需要人工处理”的暂停，丢弃启动前的旧请求。
                (self.state_dir / 'qq-resume.request').unlink(missing_ok=True)
            else:
                # 进程启动前写入的请求来自用户明确的恢复操作：只清重试冷却，保留提醒去重状态。
                self.consume_resume_request()
        try:
            while not self._stopped:
                if self.recovery.invalid:
                    self.suspended = True
                    self.pause_reason = 'QQ恢复状态无法安全读写，已停止自动尝试，请检查qq-recovery.json。'
                    self.recovery.announce('attention')
                    self.publish_state('manual_attention', self.pause_reason)
                    await self.pause(5)
                    continue
                if self.state_dir and self.consume_resume_request():
                    # 手动恢复：立刻重连；暂停与冷却都由请求解除。
                    continue
                if self.suspended:
                    self.recovery.announce('attention')
                    self.publish_state('manual_attention', self.pause_reason)
                    await self.pause(1)
                    continue
                if self.state_dir and self.recovery.remaining():
                    self.publish_state('cooldown', 'QQ接口重试冷却中；不会重新登录QQ。')
                    await self.pause(min(self.recovery.remaining(), 5))
                    continue
                failure = None
                connected_at = None
                opened = False
                manual_reconnect = False
                try:
                    try:
                        self.diagnostics.record('connect_attempt', failures=self.recovery.failures)
                        await self.transport.connect()
                        opened = True
                    except Exception:
                        if self.expected_self_id and self.account_probe and not self.state_dir:
                            actual = await asyncio.to_thread(self.account_probe)
                            if actual and str(actual) != self.expected_self_id:
                                raise AccountMismatch(self.expected_self_id, actual)
                        raise
                    try:
                        if self.expected_self_id:
                            await asyncio.wait_for(verify_transport_account(self.transport, self.expected_self_id), timeout=self.timeout)
                    except BaseException:
                        raise
                    if self.state_dir:
                        from .qq_recovery import verify_online
                        if not await asyncio.wait_for(verify_online(self.transport), timeout=self.timeout):
                            self.recovery.announce('attention')
                            raise ConnectionError('QQ账号离线，等待NapCat或人工恢复，不自动登录。')
                    self.connected = True
                    connected_at = self._connection_clock()
                    self._online_since = connected_at
                    self.publish_state('online')
                    if self.on_status:
                        self.on_status("NapCat 接口已连接，正在接收 QQ 事件。")
                    await self._read_loop()
                except InterfaceReconnectRequested:
                    self.diagnostics.record('manual_reconnect')
                    manual_reconnect = True
                except Exception as exc:
                    self.diagnostics.record('connection_error', **exception_fields(exc),
                                            pending=len(self._pending), dispatching=len(self._dispatch_tasks))
                    failure = exc
                    self.disconnects += 1
                    if isinstance(exc, (AccountAttention, AccountMismatch)) or '401' in str(exc) or '403' in str(exc):
                        await self.pause_for_attention(connection_hint(exc))
                finally:
                    self.connected = False
                    self._fail_pending(ConnectionError("NapCat 连接已断开"))
                    if opened:
                        await self._close_transport()
                if self._stopped:
                    break
                if manual_reconnect:
                    delay = 1.0
                    continue
                if self.suspended:
                    continue
                if connected_at is not None and self._connection_clock() - connected_at >= 30:
                    delay = 1.0
                if self.state_dir:
                    delay = self.recovery.failed()
                self.publish_state('cooldown' if self.state_dir and delay >= 900 else 'reconnecting', connection_hint(failure) if failure else '接口断开')
                if self.on_status and failure is not None:
                    self.on_status(connection_hint(failure) + f" {delay:g} 秒后重试。")
                await self.pause(min(delay, 5) if self.state_dir else delay)
                delay = min(delay * 2, 30.0)
        finally:
            self.connected = False
            self._fail_pending(ConnectionError("NapCat 连接已停止"))
            tasks = list(self._dispatch_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.publish_state('stopped', self.pause_reason)

    async def _close_transport(self) -> None:
        try:
            result = self.transport.close()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=2)
        except Exception:
            pass

    async def _receive_or_resume(self):
        if not self.state_dir:
            return await asyncio.wait_for(self.transport.recv_text(), timeout=self.read_timeout)
        # Keep one pending read alive across polls: cancelling partial frame reads
        # every second would corrupt the websocket stream.
        read = asyncio.create_task(self.transport.recv_text())
        deadline = asyncio.get_running_loop().time() + self.read_timeout
        try:
            while True:
                if self.consume_resume_request():
                    raise InterfaceReconnectRequested()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                done, _ = await asyncio.wait({read}, timeout=min(1, remaining))
                if read in done:
                    return read.result()
        finally:
            if not read.done():
                read.cancel()
            await asyncio.gather(read, return_exceptions=True)

    async def _read_loop(self) -> None:
        while not self._stopped:
            raw = await self._receive_or_resume()
            if raw is None:
                raise ConnectionError("NapCat 连接已断开")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self.diagnostics.record('invalid_event_json')
                continue
            if not isinstance(data, dict):
                continue
            if self.expected_self_id and data.get('self_id') is not None and str(data['self_id']) != self.expected_self_id:
                raise AccountMismatch(self.expected_self_id, data['self_id'])
            if data.get('meta_event_type') == 'heartbeat':
                self.last_heartbeat_at = time.time()
                online = (data.get('status') or {}).get('online')
                if self.last_heartbeat_at - self._diagnostic_heartbeat_at >= 60:
                    self.diagnostics.record('heartbeat', success=online is True, pending=len(self._pending),
                                            dispatching=len(self._dispatch_tasks), queued=self.outbound.waiting,
                                            message_count=self.received_messages, command_count=self.handled_commands)
                    self._diagnostic_heartbeat_at = self.last_heartbeat_at
                if online is False:
                    self.connected = False
                    self._online_since = None
                    self._fail_pending(ConnectionError('QQ账号离线'))
                    self.recovery.announce('attention')
                    self.publish_state('qq_offline', 'QQ账号离线，等待NapCat或人工恢复；不会自动登录。')
                    continue
                if online is True and self.state_dir:
                    if not self.connected:
                        self.connected = True
                        self._online_since = self._connection_clock()
                        self.publish_state('online', 'QQ账号与接口恢复在线。')
                    if self._online_since is not None and self._connection_clock()-self._online_since >= 120:
                        self.recovery.stable()
                        self.publish_state('online')
                if online is True and self.connected:
                    self.publish_state('online')
            if data.get('post_type') == 'message':
                self.received_messages += 1
                data['_local_diagnostic_trace'] = uuid.uuid4().hex[:12]
                token = trace_id.set(data['_local_diagnostic_trace'])
                try:
                    self.diagnostics.record('message_received', kind=data.get('message_type', 'unknown'),
                                            dispatching=len(self._dispatch_tasks), pending=len(self._pending))
                finally:
                    trace_id.reset(token)
                self.last_message_at = time.time()
                if self.connected:
                    self.publish_state('online')
            if self.state_dir and not self.connected:
                continue
            echo = data.get("echo")
            if echo is not None and str(echo) in self._pending:
                future = self._pending.pop(str(echo))
                if not future.done():
                    future.set_result(data)
                continue
            if len(self._dispatch_tasks) >= 64:
                self.diagnostics.record('event_dropped', reason='dispatch_capacity', dispatching=len(self._dispatch_tasks))
                continue
            task = asyncio.create_task(self._dispatch(data))
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)

    async def _dispatch(self, event: dict) -> None:
        token = trace_id.set(event.get('_local_diagnostic_trace') or uuid.uuid4().hex[:12])
        try:
            await self._dispatch_command(event)
        finally:
            trace_id.reset(token)

    async def _dispatch_command(self, event: dict) -> None:
        if event.get("post_type") != "message":
            return
        kind = event.get("message_type")
        if kind == "group":
            if not self.accept_group_messages:
                self.diagnostics.record('message_ignored', reason='group_disabled', kind=kind)
                return
            if group_command_text(event, self.allow_unmentioned_commands, self.group_extra_accept) is None:
                self.diagnostics.record('message_ignored', reason='not_command', kind=kind)
                return
        elif kind != "private":
            self.diagnostics.record('message_ignored', reason='unsupported_kind')
            return
        if event.get("self_id") is not None and str(event.get("self_id")) == str(event.get("user_id")):
            self.diagnostics.record('message_ignored', reason='self_message', kind=kind)
            return
        message_id = event.get("message_id")
        key = None if message_id is None else f"{kind}:{event.get('group_id') or event.get('user_id')}:{message_id}"
        if self._remember(key):
            self.diagnostics.record('message_ignored', reason='duplicate_event', kind=kind)
            return
        text = group_command_text(event, self.allow_unmentioned_commands, self.group_extra_accept) if kind == 'group' else event_text(event)
        if not self.request_guard.accept(f'{kind}:{event.get("group_id") or event.get("user_id")}', event.get('user_id'), text or ''):
            self.diagnostics.record('message_ignored', reason='command_rate_limit', kind=kind)
            return
        from .commands import parse_command
        self.handled_commands += 1
        started = time.monotonic()
        self.diagnostics.record('command_start', operation=parse_command(text or '').get('op', 'unknown'), kind=kind)
        try:
            await self.on_event(event)
            self.diagnostics.record('command_complete', elapsed_ms=round((time.monotonic()-started)*1000))
        except Exception as exc:
            self.diagnostics.record('command_error', **exception_fields(exc), elapsed_ms=round((time.monotonic()-started)*1000))
            if self.on_status:
                self.on_status("处理 QQ 指令失败，请检查配置或稍后重试。")

    def _fail_pending(self, exc: Exception) -> None:
        pending = list(self._pending.items())
        self._pending.clear()
        for _echo, future in pending:
            if not future.done():
                future.set_exception(exc)

    async def call(self, action: str, params: dict, *, timeout: Optional[float] = None) -> dict:
        if not self.connected:
            raise ConnectionError("NapCat 尚未连接")
        self._echo += 1
        echo = str(self._echo)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[echo] = future
        payload = json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
        started = time.monotonic()
        self.diagnostics.record('api_begin', action=action, pending=len(self._pending))
        try:
            await self.transport.send_text(payload)
            response = await asyncio.wait_for(future, timeout=self.timeout if timeout is None else timeout)
            self.diagnostics.record('api_response', action=action, retcode=response.get('retcode'),
                                    elapsed_ms=round((time.monotonic()-started)*1000))
            hint = restriction_hint(response)
            if hint:
                await self.pause_for_attention(hint)
                raise AccountAttention(hint)
            return response
        except Exception as exc:
            self.diagnostics.record('api_error', action=action, **exception_fields(exc),
                                    elapsed_ms=round((time.monotonic()-started)*1000),
                                    delivery_uncertain=action.startswith('send_'))
            raise
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()

    async def send_private(self, user_id, text: str, retries: int = 3) -> bool:
        return await self.queued_send('private', user_id, [str(text), getattr(text, 'image_url', None)],
            lambda: self._send_message("send_private_msg", "user_id", user_id, text, retries))

    async def send_group(self, group_id, text: str, retries: int = 3) -> bool:
        return await self.queued_send('group', group_id, [str(text), getattr(text, 'image_url', None)],
            lambda: self._send_message("send_group_msg", "group_id", group_id, text, retries))

    async def _send_message(self, action, destination, receiver, text, retries):
        params = {
            destination: int(receiver) if str(receiver).isdigit() else str(receiver),
            "message": text,
            "auto_escape": True,
        }
        image_url = getattr(text, "image_url", None)
        if image_url:
            try:
                response = await self.call(action, {
                    destination: params[destination],
                    "message": [
                        {"type": "text", "data": {"text": str(text)}},
                        {"type": "image", "data": {"file": image_url}},
                    ],
                })
                if int(response.get("retcode", -1)) == 0:
                    return True
            except AccountAttention:
                return False
            except (asyncio.TimeoutError, ConnectionError):
                return False
            except Exception:
                pass
            # 卡图下载或发送失败时仍提供文字和原图链接。
            fallback = getattr(text, "image_fallback", None)
            if fallback is None:
                fallback = ('\n图片发送失败，请稍后重试。' if image_url.startswith('base64://')
                            else "\n卡图：" + image_url)
            params["message"] = str(text) + fallback
            await self.pause(max(self.outbound.interval, self.outbound.destination_interval))
        for attempt in range(retries):
            if self.suspended:
                return False
            try:
                result = await self.call(action, params)
                if int(result.get("retcode", -1)) == 0:
                    return True
            except (AccountAttention, asyncio.TimeoutError, ConnectionError):
                return False
            except Exception:
                pass
            if attempt + 1 < retries:
                await self.pause(max(0.2 * (attempt + 1), self.outbound.interval, self.outbound.destination_interval))
        return False

    async def send_private_forward(self, user_id, cards: list[str], self_id=None) -> bool:
        return await self.queued_send('private', user_id, cards,
            lambda: self._send_forward("send_private_forward_msg", "user_id", user_id, cards, self_id))

    async def send_group_forward(self, group_id, cards: list[str], self_id=None) -> bool:
        return await self.queued_send('group', group_id, cards,
            lambda: self._send_forward("send_group_forward_msg", "group_id", group_id, cards, self_id))

    async def _send_forward(self, action, destination, receiver, cards, self_id):
        try:
            if not str(self_id or '').isdigit():
                info = await self.call('get_login_info', {})
                if int(info.get('retcode', -1)) != 0:
                    return False
                self_id = (info.get('data') or {}).get('user_id')
            if not str(self_id or '').isdigit():
                return False
            result = await self.call(action, {
                destination: int(receiver),
                'messages': [{'type': 'node', 'data': {
                    'name': '录像订阅助手', 'uin': str(self_id),
                    'content': [{'type': 'text', 'data': {'text': card}}],
                }} for card in cards],
                'source': '游戏王机器人',
                'summary': str(cards[0])[:80] if cards else '查询结果',
                'prompt': '[聊天记录] 游戏王机器人',
                'news': [{'text': str(cards[0])[:80]}] if cards else [],
                'timeout': 25000,
            }, timeout=30)
            return int(result.get('retcode', -1)) == 0
        except Exception:
            return False

    async def send_group_notice(self, group_id, user_id, text: str) -> bool:
        return await self.queued_send('group', group_id, [str(user_id), text],
            lambda: self._send_group_notice(group_id, user_id, text))

    async def _send_group_notice(self, group_id, user_id, text: str) -> bool:
        """发送带有用户提及的群内提示。"""
        try:
            response = await self.call("send_group_msg", {
                "group_id": int(group_id),
                "message": [
                    {"type": "at", "data": {"qq": str(user_id)}},
                    {"type": "text", "data": {"text": " " + text}},
                ],
            })
            return int(response.get("retcode", -1)) == 0
        except Exception:
            return False
