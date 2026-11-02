# -*- coding: utf-8 -*-
"""W88：记录网页访问来源（优先 Cloudflare/反代真实 IP），供本机报表统计。"""
import pathlib

p = pathlib.Path(r'D:\pythons\qq-replay-bot\bot\statistics_server.py')
t = p.read_text(encoding='utf-8')
t = t.replace('import argparse,json,os,sys,time\n', 'import argparse,json,os,sys,threading,time\n', 1)
helper = '''


_access_lock=threading.Lock()


def _visitor_ip(handler):
    for name in ('CF-Connecting-IP','True-Client-IP','X-Real-IP'):
        value=(handler.headers.get(name) or '').strip()
        if value:return value[:64]
    forwarded=(handler.headers.get('X-Forwarded-For') or '').strip()
    if forwarded:return forwarded.split(',',1)[0].strip()[:64]
    return (handler.client_address[0] if handler.client_address else '')[:64]


def _safe_path(path):
    raw=str(path or '').split('?',1)[0]
    if raw.startswith('/s/'):return '/s/<redacted>'
    if raw.startswith('/stats/'):return '/stats/<redacted>'
    return raw[:300]


def _log_access(handler,portal,status,kind):
    raw=str(handler.path or '').split('?',1)[0]
    if raw=='/health' or raw.startswith('/statistics/assets/') or raw.startswith('/statistics/card-image/'):
        return
    try:
        record=dict(at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),ip=_visitor_ip(handler),
                    method=handler.command,path=_safe_path(handler.path),status=int(status),
                    kind=str(kind or '')[:80],country=(handler.headers.get('CF-IPCountry') or '')[:8],
                    ua=(handler.headers.get('User-Agent') or '')[:300],
                    ref=(handler.headers.get('Referer') or '')[:300])
        log=portal.database.parent/'logs'/'web-access.jsonl'
        line=json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\\n'
        with _access_lock:
            log.parent.mkdir(parents=True,exist_ok=True)
            with log.open('a',encoding='utf-8') as handle:
                handle.write(line)
    except OSError:
        pass
'''
needle = '\n\ndef make_handler(portal):'
assert t.count(needle) == 1
t = t.replace(needle, helper + needle, 1)
# Add log calls after body write in GET and POST
old = "            self.end_headers();self.wfile.write(body)\n        def log_message"
new = "            self.end_headers();self.wfile.write(body)\n            _log_access(self,portal,status,kind)\n        def log_message"
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = "            self.end_headers();self.wfile.write(body)\n    return Handler"
new = "            self.end_headers();self.wfile.write(body)\n            _log_access(self,portal,status,kind)\n    return Handler"
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t, encoding='utf-8', newline='')
print('patched')
