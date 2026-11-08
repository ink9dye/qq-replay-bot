#!/usr/bin/env python3
"""Controlled query-API stress test for the Android 1.9.1 request mix.

Runs ON the server (target https://127.0.0.1:8443) so the test token never
leaves the box. Creates/disables a dedicated test account through the local
admin API, so no real user credential is involved.

Stages (default 1,2,4 concurrent users, 90 s each) walk the same sequence the
Android client issues: top100 -> pie -> player decks -> player replays -> card.

Hard aborts: MemAvailable < 250 MB, web cgroup > 520 MB, new OOM kill, load1 > 8.
Writes a JSON report and prints a per-stage summary.

Usage:
  python3 query_stress.py --create-account 90000000001 --stages 1,2,4 --seconds 90
  python3 query_stress.py --token-file /root/.qtest-token --stages 1 --seconds 30
"""
import argparse
import gzip
import json
import os
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

BASE = 'https://127.0.0.1:8443'
ADMIN_BASE = 'http://127.0.0.1:8791'
CTX = ssl._create_unverified_context()
WEB_CGROUP = Path('/sys/fs/cgroup/system.slice/ygo-web.service')
DEFAULT_CARD = 14558127


def http(method, url, token=None, body=None, timeout=60.0):
    req = urllib.request.Request(url, method=method)
    req.add_header('Accept-Encoding', 'gzip')
    req.add_header('User-Agent', 'MoeCardQuery/1.3')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf8')
        req.add_header('Content-Type', 'application/json')
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout, context=CTX) as resp:
            raw = resp.read()
            wire = len(raw)
            if resp.headers.get('Content-Encoding') == 'gzip':
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return dict(status=resp.status, wire=wire, body=raw,
                        ms=(time.monotonic() - started) * 1000.0)
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        return dict(status=exc.code, wire=0, body=b'', ms=(time.monotonic() - started) * 1000.0)
    except Exception:
        return dict(status=-1, wire=0, body=b'', ms=(time.monotonic() - started) * 1000.0)


def as_json(resp):
    try:
        return json.loads(resp['body'].decode('utf8'))
    except (ValueError, UnicodeDecodeError):
        return None


def find_people(payload):
    if not isinstance(payload, dict):
        return []
    data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    people = data.get('people') if isinstance(data, dict) else None
    if isinstance(people, list):
        return [p.get('player') for p in people if isinstance(p, dict) and p.get('player')]
    return []


def find_card_code(payload):
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            code = node.get('code')
            if isinstance(code, int) and 0 < code <= 4294967295:
                return code
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return DEFAULT_CARD


def admin(action, identity, admin_token):
    body = json.dumps({'identity': str(identity)}).encode('utf8')
    req = urllib.request.Request(f'{ADMIN_BASE}/query-api/admin/{action}', data=body, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'X-Admin-Token': admin_token})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf8'))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode('utf8'))
        except ValueError:
            return dict(ok=False, error=f'http_{exc.code}', status=exc.code)


def admin_token_from_config(root):
    cfg = json.loads((Path(root) / 'config.json').read_text(encoding='utf8'))
    return str((cfg.get('query') or {}).get('admin_token') or '')


def login(account, secret):
    return http('POST', BASE + '/query-api/login', body={'account': account, 'secret': secret})


class Monitor(threading.Thread):
    def __init__(self, interval=2.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.stop = threading.Event()
        self.samples = []
        self.abort_reason = ''
        self._oom0 = self._oom_kills()

    @staticmethod
    def _mem_available_mb():
        for line in open('/proc/meminfo', encoding='ascii'):
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024.0
        return 0.0

    @staticmethod
    def _psi_full():
        try:
            for line in open('/proc/pressure/memory', encoding='ascii'):
                if line.startswith('full'):
                    for part in line.split():
                        if part.startswith('avg10='):
                            return float(part.split('=')[1])
        except OSError:
            pass
        return 0.0

    @staticmethod
    def _web_mb():
        try:
            return int((WEB_CGROUP / 'memory.current').read_text()) / 1048576.0
        except OSError:
            return 0.0

    @staticmethod
    def _web_swap_mb():
        try:
            return int((WEB_CGROUP / 'memory.swap.current').read_text()) / 1048576.0
        except OSError:
            return 0.0

    def _oom_kills(self):
        try:
            for line in (WEB_CGROUP / 'memory.events').read_text().splitlines():
                if line.startswith('oom_kill '):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    def run(self):
        while not self.stop.is_set():
            try:
                load1 = float(open('/proc/loadavg', encoding='ascii').read().split()[0])
            except OSError:
                load1 = 0.0
            sample = dict(t=time.time(), avail_mb=self._mem_available_mb(), psi_full=self._psi_full(),
                          web_mb=self._web_mb(), web_swap_mb=self._web_swap_mb(),
                          load1=load1, oom_kill=self._oom_kills())
            self.samples.append(sample)
            if sample['avail_mb'] < 250:
                self.abort_reason = f"available {sample['avail_mb']:.0f} MB < 250 MB"
            elif sample['web_mb'] > 520:
                self.abort_reason = f"web {sample['web_mb']:.0f} MB > 520 MB"
            elif sample['oom_kill'] > self._oom0:
                self.abort_reason = 'web cgroup OOM kill'
            elif sample['load1'] > 8:
                self.abort_reason = f"load1 {sample['load1']:.1f} > 8"
            if self.abort_reason:
                self.stop.set()
                return
            time.sleep(self.interval)


def pick_player(token):
    resp = http('GET', BASE + '/query-api/top100', token=token)
    names = find_people(as_json(resp))
    return (names[0] if names else '久 远'), resp


def one_user(token, stage, seconds, think, stop, results, lock):
    player = None
    code = DEFAULT_CARD
    deadline = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < deadline:
        if player is None:
            player, resp = pick_player(token)
            results.append((stage, 'top100', resp['status'], resp['wire'], resp['ms']))
            if resp['status'] != 200:
                time.sleep(1.0)
                continue
        steps = []
        steps.append(('pie', 'GET', '/query-api/charts/pie', None))
        steps.append(('decks', 'GET', f'/query-api/player/{urllib.parse.quote(player, safe="")}/decks', None))
        steps.append(('replays', 'GET', f'/query-api/player/{urllib.parse.quote(player, safe="")}/replays', None))
        steps.append(('card', 'GET', f'/query-api/card/{code}', None))
        for name, method, path, body in steps:
            if stop.is_set() or time.monotonic() >= deadline:
                break
            resp = http(method, BASE + path, token=token, body=body)
            results.append((stage, name, resp['status'], resp['wire'], resp['ms']))
            if name == 'decks' and resp['status'] == 200:
                code = find_card_code(as_json(resp)) or DEFAULT_CARD
            time.sleep(think)


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def summarize(results, stages):
    out = {}
    for stage in stages:
        rows = [r for r in results if r[0] == stage]
        by = defaultdict(list)
        for _, name, status, wire, ms in rows:
            by[name].append((status, wire, ms))
        stage_summary = {}
        for name, items in by.items():
            ok = [ms for status, _, ms in items if status == 200]
            stage_summary[name] = dict(
                n=len(items), ok=len(ok),
                p50=round(pct(ok, 0.50), 1) if ok else None,
                p95=round(pct(ok, 0.95), 1) if ok else None,
                max=round(max(ok), 1) if ok else None,
                wire_avg=round(statistics.mean([w for _, w, _ in items]), 0) if items else 0,
                status=dict(Counter(status for status, _, _ in items)),
            )
        out[str(stage)] = stage_summary
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='/opt/ygobot')
    ap.add_argument('--stages', default='1,2,4')
    ap.add_argument('--seconds', type=int, default=90)
    ap.add_argument('--think', type=float, default=2.0)
    ap.add_argument('--create-account', default='')
    ap.add_argument('--token-file', default='')
    ap.add_argument('--keep-account', action='store_true')
    ap.add_argument('--report', default='/tmp/query-stress-report.json')
    args = ap.parse_args(argv)

    stages = [int(x) for x in args.stages.split(',') if x.strip()]
    token = ''
    identity = args.create_account
    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    elif identity:
        admin_secret = admin_token_from_config(args.root)
        if not admin_secret:
            print('no query.admin_token in config', file=sys.stderr)
            return 2
        issued = admin('issue', identity, admin_secret)
        if not issued.get('ok'):
            issued = admin('reissue', identity, admin_secret)
        if not issued.get('ok'):
            print('admin issue failed:', issued.get('error'), file=sys.stderr)
            return 2
        resp = login(identity, issued['code'])
        payload = as_json(resp)
        if resp['status'] != 200 or not payload or not payload.get('token'):
            print('login failed', resp['status'], file=sys.stderr)
            return 2
        token = payload['token']
        print('test account ready, identity=%s' % identity)
    else:
        print('need --create-account or --token-file', file=sys.stderr)
        return 2

    monitor = Monitor()
    monitor.start()
    results = []
    lock = threading.Lock()
    print('start stages=%s seconds=%d think=%.1fs' % (stages, args.seconds, args.think))
    for stage in stages:
        if monitor.abort_reason:
            break
        threads = [threading.Thread(target=one_user, args=(token, stage, args.seconds, args.think,
                                                           monitor.stop, results, lock), daemon=True)
                   for _ in range(stage)]
        started = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=args.seconds + 120)
        elapsed = time.time() - started
        rows = [r for r in results if r[0] == stage]
        bad = sum(1 for r in rows if r[2] != 200)
        print('stage %d done: %.0fs, %d requests, %d non-200 (%.0f%%), abort=%s' % (
            stage, elapsed, len(rows), bad, 100.0 * bad / max(1, len(rows)), monitor.abort_reason or '-'))

    monitor.stop.set()
    monitor.join(timeout=3)
    summary = summarize(results, stages)
    report = dict(at=time.time(), stages=stages, seconds=args.seconds, think=args.think,
                  identity=identity, abort=monitor.abort_reason,
                  summary=summary, samples=monitor.samples,
                  requests=[dict(stage=s, name=n, status=st, wire=w, ms=round(ms, 1))
                            for s, n, st, w, ms in results])
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False), encoding='utf8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('report:', args.report)

    if identity and not args.keep_account:
        try:
            admin('disable', identity, admin_token_from_config(args.root))
            print('test account disabled')
        except Exception as exc:
            print('disable failed:', exc, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
