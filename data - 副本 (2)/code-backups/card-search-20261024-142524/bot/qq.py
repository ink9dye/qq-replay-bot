# -*- coding: utf-8 -*-
"""连接本机 NapCat：私聊指令，以及可选的群内 @ 指令。"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from typing import Optional

from .watch_core import WebSocketClient


def connection_hint(exc: Exception) -> str:
    """只返回固定提示，不把响应原文、URL 参数或密钥写进日志。"""
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


def group_command_text(event: dict) -> Optional[str]:
    """只有真正 @ 本工具号的群消息才触发；普通聊天和 @全体 不触发。"""
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
        return (event_text(event).strip() or "帮助") if mentioned else None
    if isinstance(message, str):
        at = re.compile(r"\[CQ:at,qq=([0-9]+|all)(?:,[^\]]*)?\]")
        if any(match.group(1) == self_id for match in at.finditer(message)):
            return at.sub("", message).strip() or "帮助"
    return None


class OneBotClient:
    def __init__(self, url: str, token: str, on_event, transport=None, pause=None, timeout: float = 8, on_status=None, accept_group_messages: bool = False):
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
        self._dispatch_tasks: set[asyncio.Task] = set()

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
        try:
            while not self._stopped:
                failure = None
                try:
                    await self.transport.connect()
                    self.connected = True
                    delay = 1.0
                    if self.on_status:
                        self.on_status("NapCat 接口已连接，正在接收 QQ 事件。")
                    await self._read_loop()
                except Exception as exc:
                    failure = exc
                    self.disconnects += 1
                finally:
                    was_connected = self.connected
                    self.connected = False
                    self._fail_pending(ConnectionError("NapCat 连接已断开"))
                    if was_connected:
                        await self._close_transport()
                if self._stopped:
                    break
                if self.on_status and failure is not None:
                    self.on_status(connection_hint(failure) + f" {delay:g} 秒后重试。")
                await self.pause(delay)
                delay = min(delay * 2, 30.0)
        finally:
            self.connected = False
            self._fail_pending(ConnectionError("NapCat 连接已停止"))
            tasks = list(self._dispatch_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _close_transport(self) -> None:
        try:
            result = self.transport.close()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=2)
        except Exception:
            pass

    async def _read_loop(self) -> None:
        while not self._stopped:
            raw = await self.transport.recv_text()
            if raw is None:
                raise ConnectionError("NapCat 连接已断开")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            echo = data.get("echo")
            if echo is not None and str(echo) in self._pending:
                future = self._pending.pop(str(echo))
                if not future.done():
                    future.set_result(data)
                continue
            task = asyncio.create_task(self._dispatch(data))
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)

    async def _dispatch(self, event: dict) -> None:
        if event.get("post_type") != "message":
            return
        kind = event.get("message_type")
        if kind == "group":
            if not self.accept_group_messages or group_command_text(event) is None:
                return
        elif kind != "private":
            return
        if event.get("self_id") is not None and str(event.get("self_id")) == str(event.get("user_id")):
            return
        message_id = event.get("message_id")
        key = None if message_id is None else f"{kind}:{event.get('group_id') or event.get('user_id')}:{message_id}"
        if self._remember(key):
            return
        try:
            await self.on_event(event)
        except Exception:
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
        try:
            await self.transport.send_text(payload)
            return await asyncio.wait_for(future, timeout=self.timeout if timeout is None else timeout)
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()

    async def send_private(self, user_id, text: str, retries: int = 3) -> bool:
        params = {
            "user_id": int(user_id) if str(user_id).isdigit() else str(user_id),
            "message": text,
            "auto_escape": True,
        }
        for attempt in range(retries):
            try:
                result = await self.call("send_private_msg", params)
                if int(result.get("retcode", -1)) == 0:
                    return True
            except Exception:
                pass
            if attempt + 1 < retries:
                await self.pause(0.2 * (attempt + 1))
        return False

    async def send_private_forward(self, user_id, cards: list[str], self_id=None) -> bool:
        """用工具号自己的身份构造帮助记录，只发给发指令的人。"""
        try:
            if not str(self_id or '').isdigit():
                info = await self.call('get_login_info', {})
                if int(info.get('retcode', -1)) != 0:
                    return False
                self_id = (info.get('data') or {}).get('user_id')
            if not str(self_id or '').isdigit():
                return False
            result = await self.call('send_private_forward_msg', {
                'user_id': int(user_id),
                'messages': [{'type': 'node', 'data': {
                    'name': '录像订阅助手', 'uin': str(self_id),
                    'content': [{'type': 'text', 'data': {'text': card}}],
                }} for card in cards],
                'source': '录像订阅机器人使用指南',
                'summary': '指令模板 · 作用 · 例子',
                'prompt': '[聊天记录] 录像订阅机器人使用指南',
                'news': [{'text': '订阅、推送、静默、录像查询'}, {'text': '每条指令都有模板、作用和例子'}],
                'timeout': 25000,
            }, timeout=30)
            return int(result.get('retcode', -1)) == 0
        except Exception:
            return False

    async def send_group_notice(self, group_id, user_id, text: str) -> bool:
        """群内只发固定处理提示；完整回复和录像链接仍使用 send_private。"""
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
