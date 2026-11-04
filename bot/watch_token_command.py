# -*- coding: utf-8 -*-
"""QQ 观战接口令牌命令。

群里/私聊发“观战令牌”就自动签发一个绑定客户端名称的令牌；管理员可以私聊
“观战令牌 列表/吊销”。明文只在回复里出现一次，数据库只存哈希。
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .watch_tokens import DEFAULT_HOURS, WatchTokenStore

DEFAULT_PUBLIC_URL = "http://101.201.108.188:8787"
COOLDOWN_SECONDS = 30.0
_LOCK = threading.RLock()
_SAFE_WORKER = re.compile(r"[^0-9A-Za-z_-]+")


def usage() -> str:
    return ("令牌分发：给保密名单内的设备发一个观战接口令牌（群内/私聊都可）。\n"
            "令牌分发 2：给第二个设备领一个。\n"
            "管理员私聊：令牌分发 列表；令牌分发 吊销 <客户端名称>；"
            "令牌分发 吊销 QQ <QQ号>")


def _is_admin(service, user) -> bool:
    return str(user) in {str(item) for item in (getattr(service, "admins", set()) or set())}


def _in_group(conversation) -> bool:
    return str(conversation or "").startswith("group:")


def _allowed(service, user, conversation) -> bool:
    """令牌分发只对保密名单开放。

    优先用 remote.token_groups / token_users；没配置时沿用统计的
    statistics.allowed_groups / private_users。群聊不因管理员身份绕过群名单。
    """
    remote = service.config.get("remote") or {}
    stats = service.config.get("statistics") or {}
    groups = remote["token_groups"] if "token_groups" in remote else (stats.get("allowed_groups") or [])
    users = remote["token_users"] if "token_users" in remote else (stats.get("private_users") or [])
    if _in_group(conversation):
        group = str(conversation).split(":", 1)[1]
        return group in {str(item) for item in (groups or [])}
    return str(user) in {str(item) for item in (users or [])} or _is_admin(service, user)


def _public_url(service) -> str:
    remote = service.config.get("remote") or {}
    for value in (remote.get("public_url"), remote.get("server_url")):
        text = str(value or "").strip().rstrip("/")
        if text.startswith(("http://", "https://")):
            return text
    return DEFAULT_PUBLIC_URL


def _store(service) -> WatchTokenStore:
    with _LOCK:
        store = getattr(service, "_watch_token_store", None)
        if store is None:
            base = getattr(getattr(service, "store", None), "path", None)
            path = Path(base) if base else (Path(service.data_dir) / "bot.sqlite3")
            store = WatchTokenStore(path, migrate=True)
            service._watch_token_store = store
        return store


def _worker_for(user, slot: int) -> str:
    base = _SAFE_WORKER.sub("", str(user or ""))[:24] or "user"
    worker = f"android-{base}"
    if slot > 1:
        worker = f"{worker}-{slot}"
    return worker[:64]


def _format_expiry(stamp: float) -> str:
    return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M")


def _issue(service, user, slot: int, conversation) -> str:
    worker = _worker_for(user, slot)
    key = (str(user), int(slot))
    now = time.time()
    with _LOCK:
        issued = getattr(service, "_watch_token_issued_at", None)
        if issued is None:
            issued = {}
            service._watch_token_issued_at = issued
        last = float(issued.get(key) or 0.0)
        if now - last < COOLDOWN_SECONDS:
            return "令牌刚发过，请先使用上一条；30 秒后再试。"

    remote = service.config.get("remote") or {}
    hours = remote.get("device_token_hours", DEFAULT_HOURS)
    label = conversation or "private"
    try:
        row = _store(service).issue(worker, user, hours=hours, label=label)
    except Exception as exc:  # noqa: BLE001 - 用户侧只看到失败，不暴露数据库细节
        writer = getattr(service, "write_log", None)
        if callable(writer):
            writer(f"观战令牌签发失败：{type(exc).__name__}")
        return "令牌签发失败，请稍后再试。"

    with _LOCK:
        issued[key] = now
    return ("观战接口令牌（已绑定客户端）\n"
            f"服务器地址：{_public_url(service)}\n"
            f"接口令牌：{row['token']}\n"
            f"客户端名称：{row['worker']}\n"
            f"有效期至：{_format_expiry(row['expires_at'])}\n"
            "只复制“接口令牌：”后以 wt_ 开头的完整一串，不要复制标签、Bearer 或列表中的省略号。\n"
            "客户端名称必须原样填；保存后先停止再启动观战。\n"
            "同一设备重发会使旧令牌失效；到期或泄露后重新发送“令牌分发”换新。")


def _list(service, user, conversation) -> str:
    if not _is_admin(service, user):
        return "仅管理员可以查看令牌列表。"
    if _in_group(conversation):
        return "管理员命令请私聊我使用。"
    try:
        rows = _store(service).list_active(limit=30)
    except Exception:  # noqa: BLE001
        return "令牌库暂时不可用，请稍后再试。"
    if not rows:
        return "当前没有有效的观战令牌。"
    lines = [f"观战令牌（有效 {len(rows)} 个）：", "以下是辨认用提示，含省略号，不能填入 APP；完整令牌只在签发回复中显示。"]
    for row in rows:
        owner = row.get("owner") or "-"
        lines.append(f"{row['worker']}  QQ {owner}  {row['token_hint']}  "
                     f"到期 {_format_expiry(row['expires_at'])}")
    if len(rows) >= 30:
        lines.append("（只显示最近 30 个）")
    return "\n".join(lines)


def _revoke(service, user, args, conversation) -> str:
    if not _is_admin(service, user):
        return "仅管理员可以吊销令牌。"
    if _in_group(conversation):
        return "管理员命令请私聊我使用。"
    if not args:
        return "格式：观战令牌 吊销 <客户端名称>；或 观战令牌 吊销 QQ <QQ号>"
    try:
        store = _store(service)
        if str(args[0]).casefold() == "qq":
            if len(args) != 2:
                return "格式：观战令牌 吊销 QQ <QQ号>"
            count = store.revoke_owner(args[1], by=user)
            return f"已吊销 QQ {args[1]} 的 {count} 个观战令牌。"
        if len(args) != 1:
            return "格式：观战令牌 吊销 <客户端名称>"
        worker = str(args[0]).strip()
        count = store.revoke_worker(worker, by=user)
    except Exception:  # noqa: BLE001
        return "令牌库暂时不可用，请稍后再试。"
    if count:
        return f"已吊销 {worker} 的 {count} 个观战令牌。"
    return f"没有找到有效的观战令牌：{worker}"


def handle(service, user, args, conversation="") -> str:
    """commands.parse_command 的 watch_token op 入口。"""
    tokens = [str(item).strip() for item in (args or []) if str(item).strip()]
    if tokens and tokens[0].casefold() in ("help", "帮助"):
        return usage()
    if tokens and tokens[0] in ("列表", "list"):
        return _list(service, user, conversation)
    if tokens and tokens[0] in ("吊销", "revoke"):
        return _revoke(service, user, tokens[1:], conversation)
    if not _allowed(service, user, conversation):
        return "令牌分发只对保密名单内的群/用户开放。"
    if len(tokens) > 1:
        return usage()
    slot = 1
    if tokens:
        text = tokens[0]
        if not (text.isdigit() and 1 <= int(text) <= 9):
            return "直接发送“令牌分发”，或“令牌分发 2”领第二个设备。"
        slot = int(text)
    return _issue(service, user, slot, conversation)
