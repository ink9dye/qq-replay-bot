# -*- coding: utf-8 -*-
"""统计页代理：/stats/<token>/statistics/... → 8787 进程内的 StatisticsPortal。

token 是 bearer，2 小时有效。
- GET：只读查询与页面；访问入口 /statistics/access 仍然不开放。
- POST：仅放行 /statistics/api/deck-naming（人工命名）与 /statistics/api/deck-naming/groups（组合编辑），
  供拿到统计链接的人维护命名规则。portal 只认本机 + owner cookie，这里以本机 Host/客户端身份调用，
  并注入 owner 会话。不经过 8788 的 HTTP，避免多一层网络和端口依赖。
"""
from __future__ import annotations

from urllib.parse import urlsplit

OWNER_COOKIE = "qqstats_owner"
BLOCKED = ("/statistics/access",)
TEXT_TYPES = ("text/html", "text/javascript", "application/javascript", "text/css", "application/json")


def _rewrite(body, token):
    if not isinstance(body, (bytes, bytearray)):
        return body
    prefix = f"/stats/{token}/statistics/".encode()
    result = body.replace(b"/statistics/", prefix)
    result = result.replace(b'/statistics"', f'/stats/{token}/statistics"'.encode())
    result = result.replace(b"'/statistics'", f"'/stats/{token}/statistics'".encode())
    return result


def _check_link(app, token):
    link = app.store.get_stats_link(token)
    now = float(app.clock())
    if link is None:
        return None, (404, "text/plain; charset=utf-8", "统计链接不存在。".encode())
    if link.get("revoked_at"):
        return None, (410, "text/html; charset=utf-8",
                      "<meta charset='utf-8'><h1>统计链接已撤销</h1>"
                      "<p>请向分享者重新要一条。</p>".encode())
    if float(link["expires_at"]) <= now:
        return None, (410, "text/html; charset=utf-8",
                      "<meta charset='utf-8'><h1>统计链接已过期</h1>"
                      "<p>请在 QQ 里重新发送“统计”获取新链接。</p>".encode())
    return link, None


def route(app, path, parts):
    """parts=['stats', token, ...tail]；返回 (status, content_type, body) 或 None。"""
    if len(parts) < 2 or parts[0] != "stats":
        return None
    token = parts[1]
    link, error = _check_link(app, token)
    if error is not None:
        return error
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
        cookie = f"{OWNER_COOKIE}={portal._session(int(app.clock()) + 600)}"
        status, content_type, body, headers = portal.route(
            upstream, {"Host": "127.0.0.1:8788", "Cookie": cookie}, "127.0.0.1")
    except Exception:
        return 503, "text/html; charset=utf-8", (
            "<meta charset='utf-8'><h1>统计服务暂不可用</h1>"
            "<p>请确认统计网页仍在运行，稍后重新发送“统计”。</p>".encode())
    if any(kind in content_type for kind in TEXT_TYPES):
        body = _rewrite(body, token)
    location = headers.get("Location") if isinstance(headers, dict) else None
    if location and location.startswith("/statistics"):
        return status, content_type, b"", {"Location": f"/stats/{token}" + location}
    disposition = headers.get("Content-Disposition") if isinstance(headers, dict) else None
    if disposition:
        return status, content_type, body, {"Content-Disposition": disposition}
    return status, content_type, body


def post(app, path, parts, body):
    """公开统计链接的写接口：卡组命名 + 组合编辑。"""
    if len(parts) < 2 or parts[0] != "stats":
        return 403, "text/plain; charset=utf-8", "不支持的操作。".encode()
    token = parts[1]
    link, error = _check_link(app, token)
    if error is not None:
        return error
    tail = parts[2:]
    upstream = "/" + "/".join(tail)
    if upstream not in ("/statistics/api/deck-naming", "/statistics/api/deck-naming/groups"):
        return 403, "text/plain; charset=utf-8", "这个接口不通过分享链接开放。".encode()
    limit = 16384 if upstream.endswith("/groups") else 4096
    if len(body) > limit:
        return 400, "text/plain; charset=utf-8", "请求内容过大。".encode()
    portal = getattr(app, "statistics_portal", None)
    if portal is None:
        return 503, "text/plain; charset=utf-8", "统计页尚未初始化。".encode()
    try:
        cookie = f"{OWNER_COOKIE}={portal._session(int(app.clock()) + 600)}"
        headers = {
            "Host": "127.0.0.1:8788",
            "Cookie": cookie,
            "X-Deck-Naming": "1",
            "Origin": "http://127.0.0.1:8788",
            "Content-Type": "application/json",
        }
        status, content_type, payload, _extra = portal.post(upstream, headers, "127.0.0.1", body)
    except Exception:
        return 503, "text/plain; charset=utf-8", "统计服务暂不可用，请稍后重试。".encode()
    return status, content_type, payload
