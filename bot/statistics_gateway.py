"""Collector's small HTTP bridge. Never executes web/statistics business code."""
import hashlib
import hmac
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class StatisticsGateway:
    remote_only = True

    def __init__(self, secret_path, base='http://127.0.0.1:8788'):
        self.secret = Path(secret_path).read_text(encoding='ascii').strip()
        if len(self.secret) != 43 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in self.secret):
            raise ValueError('statistics owner credential invalid')
        self.base = base

    def _session(self, expires):
        value = str(expires)
        return value+'.'+hmac.new(self.secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def unavailable():
        return 503, 'text/plain; charset=utf-8', '网页服务正在更新，请稍后刷新；观战分发与上传仍独立运行。'.encode(), {'Retry-After': '3'}

    def _request(self, path, headers, body=None):
        request = Request(self.base+quote(path, safe='/?&=%:'), data=body, headers=headers,
                          method='POST' if body is not None else 'GET')
        try:
            try:
                response = build_opener(_NoRedirect()).open(request, timeout=60)
            except HTTPError as exc:
                response = exc
            with response:
                extra = {k: v for k, v in response.headers.items() if k.lower() in (
                    'location', 'set-cookie', 'content-disposition', 'cache-control', 'retry-after')}
                return response.code, response.headers.get('Content-Type', 'application/octet-stream'), response.read(), extra
        except (OSError, URLError):
            return self.unavailable()

    @staticmethod
    def _local(headers, client):
        try:
            return (ip_address(client).is_loopback and urlsplit('//'+headers.get('Host', '')).hostname in (
                '127.0.0.1', 'localhost', '::1') and not any(headers.get(k) for k in (
                    'Forwarded', 'X-Forwarded-For', 'X-Forwarded-Host')))
        except ValueError:
            return False

    def route(self, path, headers, client):
        if not self._local(headers, client):
            return 403, 'text/plain; charset=utf-8', '仅允许从本机入口访问。'.encode(), {}
        return self._request(path, {k: headers[k] for k in ('Host', 'Cookie') if headers.get(k)})

    def post(self, path, headers, client, body):
        if not self._local(headers, client):
            return 403, 'text/plain; charset=utf-8', b'Forbidden', {}
        return self._request(path, {k: headers[k] for k in (
            'Host', 'Cookie', 'Origin', 'Content-Type', 'X-Deck-Naming') if headers.get(k)}, body)

    def public(self, path):
        return self._request(path, {'Host': '127.0.0.1:8788'})
