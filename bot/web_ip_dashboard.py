# -*- coding: utf-8 -*-
"""本机访问统计看板：独立 8790 端口，不挂到统计网站 8788。"""
from __future__ import annotations

import argparse
import ipaddress
import json
import time
import urllib.request
import webbrowser
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE_PREFIXES = ('/statistics', '/s/<redacted>', '/stats/<redacted>', '/')
GEO_TTL = 86400


def _is_public(ip: str) -> bool:
    try:
        value = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if value.is_private or value.is_loopback or value.is_link_local or value.is_reserved:
        return False
    if value.version == 4 and (value in ipaddress.ip_network('100.64.0.0/10')):
        return False
    return True


def _page_path(path: str) -> bool:
    if not path or path == '/health' or path == '/favicon.ico':
        return False
    if path.startswith('/statistics/assets/') or path.startswith('/statistics/card-image/'):
        return False
    if path.startswith('/statistics/api/'):
        return False
    return path == '/' or path.startswith('/statistics') or path.startswith('/s/') or path.startswith('/stats/')


def _when(value: str):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _read(records_path: Path):
    records = []
    bad = 0
    try:
        handle = records_path.open(encoding='utf-8')
    except OSError:
        return records, bad
    with handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                bad += 1
                continue
            path = str(item.get('path') or '')
            at = _when(item.get('at'))
            if at is None:
                bad += 1
                continue
            item['_at'] = at
            records.append(item)
    return records, bad


def _geo_lookup(ips):
    path = None
    return {}


def _load_geo(path: Path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_geo(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def _fetch_geo(ips, cache_path: Path):
    cache = _load_geo(cache_path)
    now = time.time()
    missing = [ip for ip in ips if now - float((cache.get(ip) or {}).get('_ts') or 0) > GEO_TTL]
    error = ''
    if missing:
        for start in range(0, len(missing), 100):
            chunk = missing[start:start + 100]
            try:
                body = json.dumps(chunk, ensure_ascii=False).encode('utf-8')
                request = urllib.request.Request(
                    'http://ip-api.com/batch?fields=status,message,country,regionName,city,isp,query',
                    data=body, headers={'Content-Type': 'application/json; charset=utf-8'})
                with urllib.request.urlopen(request, timeout=20) as response:
                    result = json.loads(response.read().decode('utf-8'))
                for item in result if isinstance(result, list) else []:
                    if item.get('status') != 'success' or not item.get('query'):
                        continue
                    cache[str(item['query'])] = dict(country=item.get('country') or '',
                                                     province=item.get('regionName') or '',
                                                     city=item.get('city') or '',
                                                     isp=item.get('isp') or '', _ts=now)
            except Exception as exc:
                error = str(exc)
                break
        _save_geo(cache_path, cache)
    return {ip: cache.get(ip, {}) for ip in ips}, error


SESSION_GAP = 1800


def _sessions(times):
    ordered = sorted(times)
    result = []
    first = last = None
    for value in ordered:
        if first is None:
            first = last = value
        elif (value - last).total_seconds() > SESSION_GAP:
            result.append((first, last))
            first = last = value
        else:
            last = value
    if first is not None:
        result.append((first, last))
    return result


def _summary(records_path: Path, geo_cache_path: Path, days=0, query='', geo=False):
    records, bad = _read(records_path)
    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=days) if days > 0 else None
    if cutoff:
        records = [item for item in records if item['_at'] >= cutoff]
    query = (query or '').strip().lower()
    page_records = [item for item in records if _page_path(str(item.get('path') or ''))]
    if query:
        page_records = [item for item in page_records
                        if query in str(item.get('ip') or '').lower()
                        or query in str(item.get('path') or '').lower()
                        or query in str(item.get('ua') or '').lower()
                        or query in str(item.get('country') or '').lower()]
    selected = {str(item.get('ip') or 'unknown') for item in page_records}
    times = {}
    for item in records:
        ip = str(item.get('ip') or 'unknown')
        if ip in selected:
            times.setdefault(ip, []).append(item['_at'])
    entries = {}
    for item in page_records:
        ip = str(item.get('ip') or 'unknown')
        entry = entries.get(ip)
        if entry is None:
            entry = entries[ip] = dict(ip=ip, requests=0, first=item['_at'], last=item['_at'],
                                       paths=Counter(), statuses=Counter(), country='',
                                       ua=str(item.get('ua') or ''))
        entry['requests'] += 1
        if item['_at'] < entry['first']:
            entry['first'] = item['_at']
        if item['_at'] > entry['last']:
            entry['last'] = item['_at']
        entry['paths'][str(item.get('path') or '')] += 1
        entry['statuses'][str(item.get('status') or '')] += 1
        if not entry['country'] and item.get('country'):
            entry['country'] = str(item['country'])
    public = [ip for ip in entries if _is_public(ip)]
    geo_data, geo_error = ({}, '')
    if geo and public:
        geo_data, geo_error = _fetch_geo(public, geo_cache_path)
    rows = []
    total_sessions = 0
    total_seconds = 0.0
    for ip, entry in entries.items():
        sessions = _sessions(times.get(ip, []))
        seconds = sum((right - left).total_seconds() for left, right in sessions)
        total_sessions += len(sessions)
        total_seconds += seconds
        place = geo_data.get(ip, {})
        rows.append(dict(ip=ip, requests=entry['requests'], page_views=entry['requests'],
                         sessions=len(sessions),
                         duration_seconds=round(seconds, 1),
                         avg_seconds=round(seconds / len(sessions), 1) if sessions else 0,
                         first=entry['first'].strftime('%Y-%m-%d %H:%M:%S'),
                         last=entry['last'].strftime('%Y-%m-%d %H:%M:%S'),
                         country=place.get('country') or entry['country'],
                         province=place.get('province') or '',
                         city=place.get('city') or '',
                         isp=place.get('isp') or '',
                         public=_is_public(ip),
                         statuses=dict(entry['statuses']),
                         paths=[dict(path=path, count=count) for path, count in entry['paths'].most_common(8)],
                         ua=entry['ua']))
    rows.sort(key=lambda row: (-row['sessions'], -row['page_views'], row['ip']))
    statuses = Counter()
    timeline = Counter()
    paths = Counter()
    for item in page_records:
        statuses[str(item.get('status') or '')] += 1
        timeline[item['_at'].strftime('%Y-%m-%d')] += 1
        paths[str(item.get('path') or '')] += 1
    return dict(generated_at=now.strftime('%Y-%m-%d %H:%M:%S'), bad=bad, total=len(page_records),
                sessions=total_sessions, duration_seconds=round(total_seconds, 1),
                avg_seconds=round(total_seconds / total_sessions, 1) if total_sessions else 0,
                unique_ips=len(entries), public_ips=len(public), local_ips=len(entries) - len(public),
                rows=rows, statuses=dict(statuses),
                timeline=[dict(date=date, count=count) for date, count in sorted(timeline.items())],
                paths=[dict(path=path, count=count) for path, count in paths.most_common(15)],
                geo_error=geo_error, geo=bool(geo),
                duration_note='访问次数按 30 分钟无请求切分会话；时长是该会话首末请求间隔，不是精确阅读时长。')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    root: Path
    log_path: Path
    geo_path: Path

    def _send(self, status, body, kind='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/':
            html = Path(__file__).with_name('web_ip_dashboard.html').read_text(encoding='utf-8')
            return self._send(200, html, 'text/html; charset=utf-8')
        if parsed.path == '/api/summary':
            args = parse_qs(parsed.query)
            try:
                days = max(0, min(3650, int(args.get('days', ['0'])[0])))
            except ValueError:
                days = 0
            query = args.get('q', [''])[0][:128]
            geo = args.get('geo', ['0'])[0] == '1'
            data = _summary(self.log_path, self.geo_path, days, query, geo)
            return self._send(200, json.dumps(data, ensure_ascii=False))
        return self._send(404, json.dumps({'error': 'not found'}, ensure_ascii=False))

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    data = root / 'data'
    Handler.root = root
    Handler.log_path = data / 'logs' / 'web-access.jsonl'
    Handler.geo_path = data / 'logs' / 'web-ip-geo-cache.json'
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    except OSError as exc:
        print(f'无法启动访问看板：{exc}')
        return 1
    url = f'http://127.0.0.1:{args.port}/'
    print(f'访问看板：{url}')
    print('只监听 127.0.0.1；关闭此窗口即停止。')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
