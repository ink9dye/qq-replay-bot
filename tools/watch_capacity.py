# -*- coding: utf-8 -*-
"""Transport-only watch capacity benchmark. Never calls /watch-api or stores replays.

Commands: estimate (offline), serve (dedicated receiver), run, demo (loopback), merge.
No production config, real device token, MyCard connection or database is used.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
import uuid

PROTOCOL = 'qq-watch-capacity/1'
PREFIX = '/bench/v1/'
MIB = 1024 * 1024
MAX_BODY = 4 * MIB
MAX_RESPONSE = MIB
STOP_REASONS = frozenset(('receiver_duration_limit','receiver_byte_budget','system_cpu_pressure',
    'system_memory_pressure','disk_reserve','client_byte_budget','rate_limited_stage_deadline','benchmark_stopped'))


def safe_error(error):
    value=str(error)
    if value in STOP_REASONS:return value
    if value.startswith('benchmark_http_') and value[15:].isdigit():return value
    return type(error).__name__


def checked_bench_key(token):
    if (not isinstance(token,str) or not token.startswith('bench_') or len(token)<22
            or not token.isascii() or any(c.isspace()for c in token)):
        raise ValueError('set a separate ASCII WATCH_CAPACITY_TOKEN starting bench_ (>=22 chars); never use device credentials')
    return token


@dataclass(frozen=True)
class Profile:
    rooms_per_ip: int = 6
    heartbeat_seconds: float = 20.
    upload_seconds: float = 180.
    match_seconds: float = 900.
    replay_bytes: int = 16 * 1024
    evidence_bytes: int = 64 * 1024
    claim_reply_bytes: int = 4096

    def validate(self):
        if not all(math.isfinite(value) for value in (
                self.heartbeat_seconds,self.upload_seconds,self.match_seconds)):
            raise ValueError('finite intervals required')
        if not 1 <= self.rooms_per_ip <= 32:
            raise ValueError('rooms_per_ip must be 1..32')
        if min(self.heartbeat_seconds, self.upload_seconds, self.match_seconds) <= 0:
            raise ValueError('intervals must be positive')
        if not 0 <= self.evidence_bytes <= MAX_BODY or not 1 <= self.replay_bytes <= MAX_BODY:
            raise ValueError('each upload must be <=4MiB')
        if not 128 <= self.claim_reply_bytes <= 64*1024:
            raise ValueError('claim reply must be 128..65536 bytes')
        return self


def estimate(profile, ingress_mbps, egress_mbps, reserve=.4, baseline_in=0., baseline_out=0.,
             overhead=1.2, burst_seconds=10.):
    """Bandwidth planning only. Rates include an explicit estimated HTTP/TLS factor."""
    profile.validate()
    if (not all(math.isfinite(x) for x in (ingress_mbps,egress_mbps,reserve,baseline_in,baseline_out,overhead,burst_seconds))
            or min(ingress_mbps, egress_mbps, burst_seconds) <= 0 or not 0 <= reserve < 1
            or overhead < 1 or min(baseline_in,baseline_out)<0):
        raise ValueError('invalid bandwidth/reserve/overhead')
    rooms = profile.rooms_per_ip
    # One worker per modeled IP, holding the configured number of rooms.
    hb_request = 350 + 24*rooms
    hb_reply = 250
    ingress = overhead * (hb_request/profile.heartbeat_seconds + rooms*(
        (profile.replay_bytes+profile.evidence_bytes+700)/profile.upload_seconds + 700/profile.match_seconds))
    egress = overhead * (hb_reply/profile.heartbeat_seconds + rooms*(
        500/profile.upload_seconds+(profile.claim_reply_bytes+300)/profile.match_seconds))
    available_in = max(0., ingress_mbps*(1-reserve)-baseline_in)*1e6/8
    available_out = max(0., egress_mbps*(1-reserve)-baseline_out)*1e6/8
    burst = overhead*rooms*(profile.replay_bytes+profile.evidence_bytes+700)/burst_seconds
    limits = dict(average_ingress=math.floor(available_in/ingress),
                  average_egress=math.floor(available_out/egress),
                  synchronized_upload_burst=math.floor(available_in/burst))
    return dict(kind='bandwidth_model_only', profile=asdict(profile), reserve_fraction=reserve,
                assumed_ingress_mbps=ingress_mbps, assumed_egress_mbps=egress_mbps,
                per_ip_ingress_bytes_per_second=round(ingress,2),
                per_ip_egress_bytes_per_second=round(egress,2), limits_ips=limits,
                bandwidth_only_model_limit_ips=min(limits.values()),
                burst_completion_seconds=burst_seconds, estimated_wire_overhead_factor=overhead,
                actual_public_ip_limit_verified=False,
                caveat='Not a MyCard/IP-risk, scheduler, database or production capacity certification.')


def resource_sample():
    """Cheap Linux resource reads. Missing counters stay null, never assumed healthy."""
    result = dict(available_mib=None, process_rss_mib=None, system_cpu_ticks=None,
                  system_idle_ticks=None, free_disk_mib=shutil.disk_usage(Path.cwd()).free/MIB)
    try:
        mem = dict((line.split(':')[0],int(line.split()[1])) for line in Path('/proc/meminfo').read_text().splitlines())
        result['available_mib'] = mem['MemAvailable']/1024
        status = Path('/proc/self/status').read_text().splitlines()
        result['process_rss_mib'] = next(int(s.split()[1])/1024 for s in status if s.startswith('VmRSS:'))
        ticks = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        result.update(system_cpu_ticks=sum(ticks),system_idle_ticks=ticks[3]+ticks[4])
    except (OSError,ValueError,KeyError,StopIteration):
        pass
    return result


class ReceiverState:
    def __init__(self, token, duration=300, max_bytes=128*MIB, max_qps=25,
                 min_available=400, min_disk=2048, max_cpu=85, claim_bytes=4096):
        self.token = token
        self.server_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.duration = duration
        self.max_bytes = max_bytes
        self.max_qps = max_qps
        self.min_available, self.min_disk, self.max_cpu = min_available,min_disk,max_cpu
        self.claim_bytes = claim_bytes
        self.received = self.sent = 0
        self.requests = Counter()
        self.sources = set()
        self.request_times = deque()
        self.stopped = ''
        self.sample = resource_sample()
        self.last_sample = time.monotonic()
        self.high_cpu_samples = 0
        self.stopped_at = None

    def admit(self):
        with self.lock:
            now = time.monotonic()
            if now-self.started >= self.duration:
                self.stopped = self.stopped or 'receiver_duration_limit'
            if self.stopped:return False
            while self.request_times and now-self.request_times[0] >= 1:
                self.request_times.popleft()
            if len(self.request_times) >= self.max_qps:return False
            self.request_times.append(now)
            return True

    def account(self, incoming=0, outgoing=0, endpoint='', source=''):
        with self.lock:
            if self.received+self.sent+incoming+outgoing > self.max_bytes:
                self.stopped = 'receiver_byte_budget'
                return False
            self.received += incoming;self.sent += outgoing
            if endpoint:self.requests[endpoint] += 1
            if source and len(self.sources)<512:self.sources.add(source)
            return True

    def source_id(self, address):
        return hashlib.sha256((self.server_id+'\0'+address).encode()).hexdigest()[:20]

    def monitor(self):
        sample = resource_sample();now=time.monotonic()
        with self.lock:
            before = self.sample
            if sample['system_cpu_ticks'] is not None and before['system_cpu_ticks'] is not None:
                total=sample['system_cpu_ticks']-before['system_cpu_ticks']
                idle=sample['system_idle_ticks']-before['system_idle_ticks']
                sample['system_cpu_percent']=100*(1-idle/total) if total>0 else None
            cpu=sample.get('system_cpu_percent')
            self.high_cpu_samples = self.high_cpu_samples+1 if cpu is not None and cpu>self.max_cpu else 0
            if self.high_cpu_samples>=3:self.stopped=self.stopped or 'system_cpu_pressure'
            if sample['available_mib'] is not None and sample['available_mib']<self.min_available:
                self.stopped=self.stopped or 'system_memory_pressure'
            if sample['free_disk_mib']<self.min_disk:self.stopped=self.stopped or 'disk_reserve'
            self.sample=sample;self.last_sample=now
            if now-self.started>=self.duration:self.stopped=self.stopped or 'receiver_duration_limit'

    def snapshot(self):
        with self.lock:
            return dict(protocol=PROTOCOL,server_id=self.server_id,
                        elapsed_seconds=round(time.monotonic()-self.started,3),
                        body_bytes_received=self.received,body_bytes_sent=self.sent,
                        requests=dict(self.requests),distinct_source_ips=len(self.sources),
                        stopped_reason=self.stopped,resource_sample=self.sample,
                        configured_limits=dict(max_body_bytes=MAX_BODY,max_total_body_bytes=self.max_bytes,
                            max_requests_per_second=self.max_qps,max_connections=getattr(self,'max_connections',None),
                            max_duration_seconds=self.duration,min_available_mib=self.min_available,
                            min_free_disk_mib=self.min_disk,max_system_cpu_percent=self.max_cpu),
                        claim_reply_padding_bytes=self.claim_bytes,
                        real_watch_api_exercised=False,uploads_saved=False)


class BenchHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='WatchCapacity/1'

    def log_message(self,*args):pass

    def setup(self):
        super().setup();self.connection.settimeout(5)

    def reply(self, status, payload):
        body=json.dumps(payload,separators=(',',':')).encode()
        if not self.server.state.account(outgoing=len(body)):
            status=503;body=b'{"error":"receiver_byte_budget"}'
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        if status>=400:self.send_header('Connection','close');self.close_connection=True
        self.end_headers()
        try:self.wfile.write(body)
        except (OSError,TimeoutError):pass

    def allowed(self):
        value=self.headers.get('Authorization','')
        if not hmac.compare_digest(value.encode('utf8'),('Bearer '+self.server.state.token).encode('utf8')):
            self.reply(401,dict(error='benchmark_unauthorized'));return False
        if not self.server.state.admit():
            self.reply(503 if self.server.state.stopped else 429,
                       dict(error=self.server.state.stopped or 'benchmark_qps_limit'));return False
        return True

    def do_GET(self):
        if self.path not in (PREFIX+'hello',PREFIX+'status'):
            self.reply(404,dict(error='benchmark_route_required'));return
        if not self.allowed():return
        state=self.server.state;state.account(endpoint=self.path,source=state.source_id(self.client_address[0]))
        result=state.snapshot();result['observed_source_id']=state.source_id(self.client_address[0])
        result['observed_source_public']=ipaddress.ip_address(self.client_address[0]).is_global
        self.reply(200,result)

    def do_POST(self):
        if self.path not in [PREFIX+n for n in ('claim','heartbeat','upload','report')]:
            self.reply(404,dict(error='benchmark_route_required'));return
        if not self.allowed():return
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:length=-1
        if not 0<length<=MAX_BODY:
            self.reply(413,dict(error='benchmark_body_limit'));return
        is_upload=self.path.endswith('/upload')
        if not is_upload and length>65536:
            self.reply(413,dict(error='benchmark_json_limit'));return
        state=self.server.state
        if not state.account(incoming=length,endpoint=self.path,
                             source=state.source_id(self.client_address[0])):
            self.reply(503,dict(error='receiver_byte_budget'));return
        remaining=length;parts=[]
        try:
            while remaining:
                chunk=self.rfile.read(min(65536,remaining))
                if not chunk:raise ValueError('short_body')
                remaining-=len(chunk)
                if not is_upload:parts.append(chunk)
            if not is_upload:
                payload=json.loads(b''.join(parts))
                if not isinstance(payload,dict):raise ValueError('json_object_required')
        except (OSError,TimeoutError,ValueError):
            self.reply(400,dict(error='invalid_or_timed_out_body'));return
        if self.path.endswith('/claim'):
            self.reply(200,dict(job=dict(id=uuid.uuid4().hex[:12],lease=120,
                                        padding='x'*state.claim_bytes)))
        else:self.reply(200,dict(ok=True,cancel=[]))


class BenchServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    block_on_close=False

    def __init__(self,address,state,max_connections=32):
        self.state=state;self.slots=threading.BoundedSemaphore(max_connections)
        self.state.max_connections=max_connections
        self.finished=threading.Event()
        super().__init__(address,BenchHandler)

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:pass
            self.shutdown_request(request);return
        try:super().process_request(request,address)
        except Exception:self.slots.release();raise

    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


class ByteBudget:
    def __init__(self,max_bytes,rate_mbps,stop,deadline=None):
        self.max_bytes=max_bytes;self.rate=rate_mbps*1e6/8
        self.stop=stop;self.lock=threading.Lock();self.total=0
        self.started=time.monotonic();self.throttled=False
        self.deadline=deadline

    def reserve(self,count):
        with self.lock:
            if self.total+count>self.max_bytes:
                self.stop.set();raise RuntimeError('client_byte_budget')
            target=self.started+self.total/self.rate if self.rate>0 else time.monotonic()
            self.total+=count
        wait=target-time.monotonic()
        if wait>0:
            self.throttled=True
            if self.deadline is not None and target>=self.deadline:
                self.stop.set();raise RuntimeError('rate_limited_stage_deadline')
            if self.stop.wait(wait):raise RuntimeError('benchmark_stopped')


class Results:
    def __init__(self):
        self.lock=threading.Lock();self.rows=defaultdict(lambda:dict(attempts=0,ok=0,errors=Counter(),
            durations=deque(maxlen=4096),sent=0,received=0))

    def add(self,name,status,seconds,sent,received,error=''):
        with self.lock:
            row=self.rows[name];row['attempts']+=1;row['ok']+=int(200<=status<300)
            row['durations'].append(seconds);row['sent']+=sent;row['received']+=received
            if status>=300 or status==0:row['errors'][str(status)if status else error]+=1

    def snapshot(self):
        with self.lock:
            out={}
            for name,row in self.rows.items():
                samples=sorted(row['durations']);p95=samples[max(0,math.ceil(.95*len(samples))-1)]
                out[name]=dict(attempts=row['attempts'],ok=row['ok'],errors=dict(row['errors']),
                               latency_p95_ms=round(p95*1000,3),sampled_latencies=len(samples),
                               estimated_http_bytes_sent=row['sent'],estimated_http_bytes_received=row['received'])
            return out


def checked_url(value,allow_network=False):
    parts=urlsplit(value)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('use a benchmark http(s) origin without credentials')
    if parts.path not in ('','/') or parts.query or parts.fragment:
        raise ValueError('URL must be an origin; /watch-api and token links are not accepted')
    try:loopback=ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:loopback=parts.hostname.lower()=='localhost'
    if not loopback and not allow_network:
        raise ValueError('non-loopback target requires --allow-network')
    return parts


class BenchClient:
    def __init__(self,url,token,budget,results,stop,allow_network=False,timeout=5):
        self.parts=checked_url(url,allow_network);self.token=token;self.budget=budget
        self.results=results;self.stop=stop;self.timeout=timeout;self.conn=None

    def close(self):
        if self.conn:self.conn.close();self.conn=None

    def call(self,name,payload=None,body=None):
        if self.stop.is_set():raise RuntimeError('benchmark_stopped')
        if self.conn is None:
            kind=http.client.HTTPSConnection if self.parts.scheme=='https' else http.client.HTTPConnection
            self.conn=kind(self.parts.hostname,self.parts.port,timeout=self.timeout)
        data=body if body is not None else (json.dumps(payload).encode()if payload is not None else None)
        method='POST'if data is not None else'GET';path=PREFIX+name
        headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/octet-stream'if body is not None else'application/json'}
        sent=(len(data)if data is not None else 0)+len(method)+len(path)+80+sum(len(k)+len(v)+4 for k,v in headers.items())
        self.budget.reserve(sent);start=time.monotonic();received=0
        try:
            self.conn.request(method,path,body=data,headers=headers)
            response=self.conn.getresponse();raw=response.read(MAX_RESPONSE+1)
            if len(raw)>MAX_RESPONSE:raise ValueError('oversized_benchmark_response')
            received=len(raw)+32+sum(len(k)+len(v)+4 for k,v in response.getheaders())
            result=json.loads(raw)if raw else{}
            if not isinstance(result,dict):raise ValueError('benchmark_json_object_required')
            self.results.add(name,response.status,time.monotonic()-start,sent,received)
            self.budget.reserve(received)
            if response.status!=200:
                if response.status in (401,404,503):self.stop.set()
                if result.get('error') in STOP_REASONS:raise RuntimeError(result['error'])
                raise RuntimeError('benchmark_http_'+str(response.status))
            return result
        except (OSError,ValueError,http.client.HTTPException) as error:
            self.results.add(name,0,time.monotonic()-start,sent,received,type(error).__name__)
            self.close();raise


def run_stage(url,token,logical_ips,profile,seconds,max_bytes,rate_mbps,allow_network=False,
              synchronized=False,p95_limit_ms=1500,error_limit=.01):
    profile.validate();checked_url(url,allow_network)
    if not 1<=logical_ips<=128 or not 0<seconds<=1800:raise ValueError('stage bounds exceeded')
    stop=threading.Event();results=Results()
    budget=ByteBudget(max_bytes,rate_mbps,stop,deadline=time.monotonic()+seconds+5)
    control=BenchClient(url,token,budget,Results(),stop,allow_network)
    try:hello=control.call('hello')
    except Exception:control.close();raise
    if hello.get('protocol')!=PROTOCOL:
        control.close();raise ValueError('not a dedicated capacity receiver; no load sent')
    if hello.get('claim_reply_padding_bytes')!=profile.claim_reply_bytes:
        control.close();raise ValueError('receiver/client claim payload sizes differ')
    server_before=hello;started=time.monotonic();deadline=started+seconds
    receiver_qps=max(1,int(hello.get('configured_limits',{}).get('max_requests_per_second',25)))
    startup_spread=0. if synchronized else min(seconds/4,logical_ips*profile.rooms_per_ip/receiver_qps*3)
    # Reuse immutable buffers across threads. No replay files are opened or generated.
    replay=b'x'*profile.replay_bytes;evidence=b'e'*profile.evidence_bytes
    failures=[];failure_reasons=[];failure_lock=threading.Lock()
    def worker(number):
        client=BenchClient(url,token,budget,results,stop,allow_network)
        jobs=[]
        try:
            if stop.wait(number/logical_ips*startup_spread):return
            worker_id='bench-'+uuid.uuid4().hex[:12]
            for _ in range(profile.rooms_per_ip):
                jobs.append(client.call('claim',dict(worker=worker_id,capacity=profile.rooms_per_ip))['job']['id'])
            next_heartbeat=time.monotonic()
            phase=0 if synchronized else (number+.5)/logical_ips*min(profile.upload_seconds,seconds*.5)
            next_upload=started+phase
            next_match=started+profile.match_seconds
            while not stop.is_set()and time.monotonic()<deadline:
                now=time.monotonic()
                if now>=next_match:
                    for job in jobs:client.call('report',dict(worker=worker_id,job=job,outcome='saved'))
                    jobs=[client.call('claim',dict(worker=worker_id,capacity=profile.rooms_per_ip))['job']['id']
                          for _ in range(profile.rooms_per_ip)]
                    next_match=now+profile.match_seconds
                if now>=next_heartbeat:
                    client.call('heartbeat',dict(worker=worker_id,active=jobs));next_heartbeat=now+profile.heartbeat_seconds
                if now>=next_upload:
                    for job in jobs:
                        client.call('upload',body=replay)
                        if evidence:client.call('upload',body=evidence)
                    next_upload=now+profile.upload_seconds
                stop.wait(min(.1,max(0.,deadline-time.monotonic())))
            if not stop.is_set():
                for job in jobs:client.call('report',dict(worker=worker_id,job=job,outcome='saved'))
        except Exception as error:
            with failure_lock:
                failures.append(type(error).__name__);failure_reasons.append(safe_error(error))
        finally:client.close()
    try:
        with ThreadPoolExecutor(max_workers=logical_ips)as pool:
            list(pool.map(worker,range(logical_ips)))
        elapsed=max(.001,time.monotonic()-started)
        rows=results.snapshot();attempts=sum(r['attempts']for r in rows.values());ok=sum(r['ok']for r in rows.values())
        # A server stop is captured as an explicit failure, rather than a missing successful sample.
        server_after=None
        if not stop.is_set():
            try:server_after=control.call('status')
            except Exception:pass
        minimum=dict(claim=logical_ips*profile.rooms_per_ip,heartbeat=logical_ips,
                     upload=logical_ips*profile.rooms_per_ip*(2 if evidence else 1),
                     report=logical_ips*profile.rooms_per_ip)
        incomplete=[name for name,count in minimum.items()if rows.get(name,{}).get('ok',0)<count]
        valid=(not failures and not incomplete and attempts>0 and 1-ok/attempts<=error_limit
               and all(r['latency_p95_ms']<=p95_limit_ms for r in rows.values()))
        signals=[]
        if budget.throttled:signals.append('load_generator_rate_limit')
        if any('429'in r['errors']for r in rows.values()):signals.append('configured_receiver_qps_limit')
        if any('503'in r['errors']for r in rows.values()):signals.append('receiver_resource_or_connection_limit_check_receiver_report')
        if any(r['latency_p95_ms']>p95_limit_ms for r in rows.values()):signals.append('latency_threshold')
        if incomplete:signals.append('short_stage_or_incomplete_workload')
        return dict(protocol=PROTOCOL,kind='dedicated_receiver_transport_only',
            started_at=time.time()-elapsed,ended_at=time.time(),elapsed_seconds=round(elapsed,3),
            server_id=hello['server_id'],observed_source_id=hello['observed_source_id'],
            observed_source_public=hello.get('observed_source_public',False),
            modeled_ip_groups=logical_ips,source_ip_probe_count=1,
            profile=asdict(profile),synchronized_uploads=synchronized,requests=rows,
            startup_spread_seconds=startup_spread,
            startup_uploads_forced=True,steady_upload_intervals_covered=elapsed/profile.upload_seconds,
            short_stage_not_steady_state=elapsed<2*profile.upload_seconds,
            estimated_http_ingress_mbps=sum(r['estimated_http_bytes_sent']for r in rows.values())*8/elapsed/1e6,
            estimated_http_egress_mbps=sum(r['estimated_http_bytes_received']for r in rows.values())*8/elapsed/1e6,
            failed_workers=len(failures),failure_types=dict(Counter(failures)),error_rate=1-ok/attempts if attempts else 1,
            failure_reasons=dict(Counter(failure_reasons)),
            limiting_signals=signals,
            minimum_workload_covered=not incomplete,incomplete_endpoints=incomplete,
            stage_passed=valid,client_rate_limiter_engaged=budget.throttled,
            generator_budget_bytes=budget.total,server_before=server_before,server_after=server_after,
            actual_public_ip_limit_verified=False,
            interpretation='Passed offered transport workload only; does not test MyCard or real watch API/DB.')
    finally:stop.set();control.close()


def merge_reports(reports):
    stages=[]
    for report in reports:
        stages.extend(report.get('stages',[report]))
    if not stages:raise ValueError('no results')
    if len({s['server_id']for s in stages})!=1:raise ValueError('different receiver sessions')
    if len({json.dumps(s['profile'],sort_keys=True)for s in stages})!=1:raise ValueError('different workload profiles')
    start=max(s['started_at']for s in stages);end=min(s['ended_at']for s in stages)
    if end<=start:raise ValueError('runs did not overlap; sequential ramp stages cannot be merged as simultaneous IPs')
    return dict(kind='multi_agent_transport_summary',agents=len(stages),overlap_seconds=end-start,
                observed_distinct_source_ips=len({s['observed_source_id']for s in stages}),
                observed_distinct_public_ips=len({s['observed_source_id']for s in stages if s.get('observed_source_public')is True}),
                offered_modeled_ip_groups=sum(s['modeled_ip_groups']for s in stages),
                all_stages_passed=all(s['stage_passed']for s in stages),
                client_limiter_engaged=any(s['client_rate_limiter_engaged']for s in stages),
                actual_public_ip_limit_verified=False,
                caveat='Distinct NAT egresses verified by receiver; total server maximum and platform limits are not proven.')


def write_report(path,result):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')


def start_receiver(server):
    def monitor():
        while not server.finished.wait(1):
            server.state.monitor()
            if server.state.stopped:
                # Keep control requests available briefly so callers can see the stop reason.
                if server.state.stopped_at is None:server.state.stopped_at=time.monotonic()
                if time.monotonic()-server.state.stopped_at>2:server.shutdown();break
    thread=threading.Thread(target=monitor,name='capacity-guard',daemon=True);thread.start()
    return thread


def add_profile(parser):
    parser.add_argument('--rooms-per-ip',type=int,default=6)
    parser.add_argument('--heartbeat-seconds',type=float,default=20)
    parser.add_argument('--upload-seconds',type=float,default=180)
    parser.add_argument('--match-seconds',type=float,default=900)
    parser.add_argument('--replay-kib',type=float,default=16)
    parser.add_argument('--evidence-kib',type=float,default=64)
    parser.add_argument('--claim-reply-bytes',type=int,default=4096)


def profile_from(args):
    return Profile(args.rooms_per_ip,args.heartbeat_seconds,args.upload_seconds,args.match_seconds,
                   int(args.replay_kib*1024),int(args.evidence_kib*1024),args.claim_reply_bytes).validate()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='mode',required=True)
    model=subs.add_parser('estimate',help='offline bandwidth model; sends no traffic');add_profile(model)
    model.add_argument('--ingress-mbps',type=float,required=True);model.add_argument('--egress-mbps',type=float,required=True)
    model.add_argument('--reserve',type=float,default=.4);model.add_argument('--baseline-in-mbps',type=float,default=0)
    model.add_argument('--baseline-out-mbps',type=float,default=0);model.add_argument('--burst-seconds',type=float,default=10)
    model.add_argument('--output',default='watch-capacity-estimate.json')
    serve=subs.add_parser('serve',help='dedicated discard-only receiver, never a production watch API')
    serve.add_argument('--bind',default='127.0.0.1');serve.add_argument('--port',type=int,default=8896)
    serve.add_argument('--duration',type=float,default=300);serve.add_argument('--max-total-mib',type=float,default=128)
    serve.add_argument('--max-qps',type=int,default=25);serve.add_argument('--max-connections',type=int,default=32)
    serve.add_argument('--min-available-mib',type=float,default=400);serve.add_argument('--min-disk-mib',type=float,default=2048)
    serve.add_argument('--max-cpu-percent',type=float,default=85);serve.add_argument('--claim-reply-bytes',type=int,default=4096)
    serve.add_argument('--output',default='watch-capacity-receiver.json')
    for name in ('run','demo'):
        command=subs.add_parser(name);add_profile(command)
        command.add_argument('--stages',default='1,2,4,8');command.add_argument('--stage-seconds',type=float,default=30)
        command.add_argument('--max-total-mib',type=float,default=64);command.add_argument('--max-rate-mbps',type=float,default=1.5)
        command.add_argument('--synchronized',action='store_true');command.add_argument('--p95-ms',type=float,default=1500)
        command.add_argument('--error-fraction',type=float,default=.01)
        command.add_argument('--output',default='watch-capacity-'+name+'.json')
        if name=='run':command.add_argument('--url',required=True);command.add_argument('--allow-network',action='store_true')
    merge=subs.add_parser('merge');merge.add_argument('files',nargs='+');merge.add_argument('--output',default='watch-capacity-merged.json')
    args=parser.parse_args(argv)
    try:
        exit_code=0
        if args.mode=='estimate':
            result=estimate(profile_from(args),args.ingress_mbps,args.egress_mbps,args.reserve,
                            args.baseline_in_mbps,args.baseline_out_mbps,burst_seconds=args.burst_seconds)
        elif args.mode=='merge':
            result=merge_reports([json.loads(Path(p).read_text(encoding='utf8'))for p in args.files])
        elif args.mode=='serve':
            token=checked_bench_key(os.environ.get('WATCH_CAPACITY_TOKEN',''))
            if not 1<=args.duration<=3600 or not 1<=args.max_qps<=200 or not 1<=args.max_connections<=128:
                raise ValueError('receiver bounds exceeded')
            if (not math.isfinite(args.max_total_mib) or args.max_total_mib<=0
                    or not 128<=args.claim_reply_bytes<=65536
                    or not 0<args.max_cpu_percent<=100
                    or not math.isfinite(args.min_available_mib) or args.min_available_mib<0
                    or not math.isfinite(args.min_disk_mib) or args.min_disk_mib<0):
                raise ValueError('invalid receiver budget')
            state=ReceiverState(token,args.duration,int(args.max_total_mib*MIB),args.max_qps,
                                args.min_available_mib,args.min_disk_mib,args.max_cpu_percent,args.claim_reply_bytes)
            server=BenchServer((args.bind,args.port),state,args.max_connections);start_receiver(server)
            print('Dedicated receiver started; token hidden, uploads discarded. Ctrl+C stops it.',flush=True)
            try:server.serve_forever(poll_interval=.2)
            except KeyboardInterrupt:pass
            finally:server.finished.set();server.server_close()
            result=state.snapshot()
        else:
            profile=profile_from(args);stages=[int(x)for x in args.stages.split(',')]
            if not stages or any(not 1<=n<=128 for n in stages)or len(stages)>12:raise ValueError('1..128 groups, <=12 stages')
            if (not math.isfinite(args.max_rate_mbps) or not math.isfinite(args.max_total_mib)
                    or args.max_rate_mbps<=0 or args.max_total_mib<=0):raise ValueError('positive finite traffic caps required')
            if not 0<args.stage_seconds<=1800 or not 0<=args.error_fraction<1 or args.p95_ms<=0:raise ValueError('invalid stage limits')
            server=None;thread=None
            if args.mode=='demo':
                token='bench_'+secrets.token_urlsafe(24)
                state=ReceiverState(token,duration=min(3600,len(stages)*(args.stage_seconds+8)),
                                    max_bytes=int(args.max_total_mib*MIB),max_qps=200,claim_bytes=profile.claim_reply_bytes)
                server=BenchServer(('127.0.0.1',0),state,128)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();start_receiver(server)
                url='http://127.0.0.1:'+str(server.server_port);allow=False
            else:
                token=checked_bench_key(os.environ.get('WATCH_CAPACITY_TOKEN',''));url=args.url;allow=args.allow_network
            result=dict(protocol=PROTOCOL,stages=[],actual_public_ip_limit_verified=False)
            try:
                remaining=int(args.max_total_mib*MIB)
                for n in stages:
                    print('Testing modeled IP groups:',n,flush=True)
                    try:
                        stage=run_stage(url,token,n,profile,args.stage_seconds,remaining,args.max_rate_mbps,
                                        allow,args.synchronized,args.p95_ms,args.error_fraction)
                    except (OSError,RuntimeError,ValueError) as error:
                        result['stopped_error_type']=type(error).__name__;result['stopped_reason']=safe_error(error);exit_code=2;break
                    result['stages'].append(stage);remaining-=stage['generator_budget_bytes']
                    print('Stage:',n,'passed:',stage['stage_passed'],'client limited:',stage['client_rate_limiter_engaged'],flush=True)
                    if not stage['stage_passed']or remaining<=0:
                        exit_code=2;break
                result['highest_passed_modeled_groups']=max((s['modeled_ip_groups']for s in result['stages']if s['stage_passed']),default=0)
                result['measured_actual_source_ips']=len({s['observed_source_id']for s in result['stages']})
                result['measured_public_source_ips']=len({s['observed_source_id']for s in result['stages']if s.get('observed_source_public')is True})
                result['interpretation']='A ramp stage count is logical load, not distinct public IPs or a verified deployment maximum.'
            finally:
                if server:server.finished.set();server.shutdown();server.server_close();thread.join(timeout=2)
        write_report(args.output,result)
        print('Report saved:',str(Path(args.output).resolve()),flush=True)
        return exit_code
    except (ValueError,OSError,RuntimeError) as error:
        # Do not print HTTP response bodies, credentials, or full URLs.
        print('Capacity test stopped:',type(error).__name__,str(error)if isinstance(error,ValueError)else'',flush=True)
        return 2


if __name__=='__main__':raise SystemExit(main())
