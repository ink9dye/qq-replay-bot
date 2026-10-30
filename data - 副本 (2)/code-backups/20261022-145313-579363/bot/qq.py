# -*- coding: utf-8 -*-
"""连接本机 NapCat 的 OneBot 11 正向 WebSocket。只处理私聊。"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Optional

from .watch_core import WebSocketClient


class NapCatTransport:
    def __init__(self, url: str, token: str):
        if any(char in (token or "") for char in "\r\n"):
            raise RuntimeError("接口 Token 无效")
        self.url = url
        self.token = token
        self.client: Optional[WebSocketClient] = None

    async def connect(self) -> None:
        self.client = WebSocketClient(self.url)
        await self.client.connect({"Authorization": "Bearer " + self.token})

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


class OneBotClient:
    def __init__(self, url: str, token: str, on_event, transport=None, pause=None, timeout: float = 8):
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
        while not self._stopped:
            try:
                await self.transport.connect()
                self.connected = True
                delay = 1.0
                await self._read_loop()
            except Exception:
                self.connected = False
                self.disconnects += 1
                self._fail_pending(ConnectionError("NapCat 连接已断开"))
                if self._stopped:
                    break
                await self.pause(delay)
                delay = min(delay * 2, 30.0)

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
            asyncio.create_task(self._dispatch(data))

    async def _dispatch(self, event: dict) -> None:
        if event.get("post_type") != "message" or event.get("message_type") != "private":
            return
        if self._remember(event.get("message_id")):
            return
        try:
            await self.on_event(event)
        except Exception as exc:
            print(f"处理私聊失败：{exc}", flush=True)

    def _fail_pending(self, exc: Exception) -> None:
        pending = list(self._pending.items())
        self._pending.clear()
        for _echo, future in pending:
            if not future.done():
                future.set_exception(exc)

    async def call(self, action: str, params: dict) -> dict:
        self._echo += 1
        echo = str(self._echo)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[echo] = future
        payload = json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
        try:
            await self.transport.send_text(payload)
            return await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            self._pending.pop(echo, None)

    async def send_private(self, user_id, text: str, retries: int = 3) -> bool:
        params = {
            "user_id": int(user_id) if str(user_id).isdigit() else str(user_id),
            "message": text,
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
