# -*- coding: utf-8 -*-
"""统计页只读代理：/stats/<token>/statistics/... → 8787 进程内的 StatisticsPortal。

token 是 bearer，2 小时有效；只放行 GET，不代理 deck-naming/access 等写入口。
portal 只认本机 + owner cookie，这里以本机 Host/客户端身份调用，并注入 owner 会话。
不经过 8788 的 HTTP，避免多一层网络和端口依赖。
"""
from __future__ import annotations

from urllib.parse import urlsplit

OWNER_COOKIE = "qqstats_owner"
BLOCKED = ("/statistics/access", "/statistics/deck-naming", "/statistics/api/deck-naming")
TEXT_TYPES = ("text/html", "text/javascript", "application/javascript", "text/css", "application/json")


def _rewrite(body, token):
    if not isinstance(body, (bytes, bytearray)):
        return body
    prefix = f"/stats/{token}/statistics/".encode()
    result = body.replace(b"/statistics/", prefix)
    result = result.replace(b'/statistics"', f'/stats/{token}/statistics"'.encode())
    result = result.replace(b"'/statistics'", f"'/stats/{token}/statistics'".encode())
    return result


def route(app, path, parts):
    """parts=['stats', token, ...tail]；返回 (status, content_type, body) 或 None。"""
    if len(parts) < 2 or parts[0] != "stats":
        return None
    token = parts[1]
    link = app.store.get_stats_link(token)
    now = float(app.clock())
    if link is None:
        return 404, "text/plain; charset=utf-8", "统计链接不存在。".encode()
    if float(link["expires_at"]) <= now:
        return 410, "text/html; charset=utf-8", (
            "<meta charset='utf-8'><h1>统计链接已过期</h1>"
            "<p>请在 QQ 里重新发送“统计”获取新链接。</p>").encode()
    tail = parts[2:]
    if tail and tail[0] != "statistics":
        return 404, "text/plain; charset=utf-8", "找不到这个统计页面。".encode()
    upstream = "/" + "/".join(tail) if tail else "/statistics"
    if any(upstream.startswith(prefix) for prefix in BLOCKED):
        return 403, "text/plain; charset=utf-8", "这个页面不通过分享链接开放。".encode()
    query = urlsplit(path or "").query
    if query:
        upstream += "?" + query
    portal = getattr(app, "statistics_portal", None)
    if portal is None:
        return 503, "text/plain; charset=utf-8", "统计页尚未初始化。".encode()
    try:
        cookie = f"{OWNER_COOKIE}={portal._session(int(now) + 600)}"
        status, content_type, body, headers = portal.route(
            upstream, {"Host": "127.0.0.1:8788", "Cookie": cookie}, "127.0.0.1")
    except Exception:
        return 503, "text/html; charset=utf-8", (
            "<meta charset='utf-8'><h1>统计服务暂不可用</h1>"
            "<p>请确认统计网页仍在运行，稍后重新发送“统计”。</p>").encode()
    if any(kind in content_type for kind in TEXT_TYPES):
        body = _rewrite(body, token)
    location = headers.get("Location") if isinstance(headers, dict) else None
    if location and location.startswith("/statistics"):
        return status, content_type, b"", {"Location": f"/stats/{token}" + location}
    return status, content_type, body
