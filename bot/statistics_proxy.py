# -*- coding: utf-8 -*-
"""统计页代理：/stats/<token>/statistics/... → ygo-web 127.0.0.1:8788。

token 是 bearer，2 小时有效。
- GET：只读查询与页面；访问入口 /statistics/access 仍然不开放。
- POST：仅放行 /statistics/api/deck-naming（人工命名）与 /statistics/api/deck-naming/groups（组合编辑），
  供拿到统计链接的人维护命名规则。portal 只认本机 + owner cookie，这里以本机 Host/客户端身份调用，
  并注入 owner 会话。不经过 8788 的 HTTP，避免多一层网络和端口依赖。
"""
from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

OWNER_COOKIE = "qqstats_owner"
UPSTREAM = "http://127.0.0.1:8788"
BLOCKED = ("/statistics/access",)
TEXT_TYPES = ("text/html", "text/javascript", "application/javascript", "text/css", "application/json")
IMAGE_SOURCES = ("https://cdn.233.momobako.com/ygopro/pics/",
                 "https://cdntx.moecube.com/ygopro-super-pre/data/pics/")
IMAGE_PATHS = ("/statistics/card-thumb/", "/statistics/card-image/")


class _NoImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


def _fetch(upstream, headers, body=None, method="GET"):
    """请求 ygo-web；失败抛 OSError/HTTPError 由调用方回退或报错。"""
    # 查询串已在浏览器侧编码；'+' 是表单编码的空格，不能再转成 %2B，
    # 否则带空格的玩家名（如“久 远”）到上游会变成带加号的另一个名字。
    request = urllib.request.Request(UPSTREAM + quote(upstream, safe='/?&=%:+'), data=body, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    request.add_header("Accept-Encoding", "identity")
    try:
        if urlsplit(upstream).path.startswith(IMAGE_PATHS):
            # Let the browser fetch a cache miss from CDN; never forward the owner cookie.
            response = urllib.request.build_opener(_NoImageRedirect()).open(request, timeout=10)
        else:
            response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()
    return response.status, dict(response.headers.items()), response.read()


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
    cookie = f"{OWNER_COOKIE}={portal._session(int(app.clock()) + 600)}"
    headers = {"Host": "127.0.0.1:8788", "Cookie": cookie}
    try:
        status, remote_headers, body = _fetch(upstream, headers)
        content_type = next((value for key, value in remote_headers.items()
                             if key.lower() == "content-type"), "application/octet-stream")
        headers = {key: value for key, value in remote_headers.items()
                   if key.lower() in ("location", "content-disposition")}
    except (OSError, urllib.error.URLError):
        if getattr(portal, 'remote_only', False):
            return portal.unavailable()
        # ygo-web 不可用时回退进程内 portal，至少还能看统计。
        try:
            status, content_type, body, headers = portal.route(
                upstream, {"Host": "127.0.0.1:8788", "Cookie": cookie}, "127.0.0.1")
        except Exception:
            return 503, "text/html; charset=utf-8", (
                "<meta charset='utf-8'><h1>统计服务暂不可用</h1>"
                "<p>请确认统计网页仍在运行，稍后重新发送“统计”。</p>".encode())
    if any(kind in content_type for kind in TEXT_TYPES):
        body = _rewrite(body, token)
    if urlsplit(upstream).path.startswith(IMAGE_PATHS):
        location = next((value for key,value in headers.items() if key.lower()=='location'),None)
        if status==302 and location and location.startswith(IMAGE_SOURCES):
            return status,content_type,b'',{'Location':location}
        if status==200 and content_type.startswith('image/'):
            return status,content_type,body,{'Cache-Control':'private, max-age=86400'}
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
    cookie = f"{OWNER_COOKIE}={portal._session(int(app.clock()) + 600)}"
    headers = {
        "Host": "127.0.0.1:8788",
        "Cookie": cookie,
        "X-Deck-Naming": "1",
        "Origin": "http://127.0.0.1:8788",
        "Content-Type": "application/json",
    }
    try:
        status, remote_headers, payload = _fetch(upstream, headers, body=body, method="POST")
        content_type = next((value for key, value in remote_headers.items()
                             if key.lower() == "content-type"), "application/octet-stream")
    except (OSError, urllib.error.URLError):
        if getattr(portal, 'remote_only', False):
            return portal.unavailable()[:3]
        try:
            status, content_type, payload, _extra = portal.post(upstream, headers, "127.0.0.1", body)
        except Exception:
            return 503, "text/plain; charset=utf-8", "统计服务暂不可用，请稍后重试。".encode()
    return status, content_type, payload
