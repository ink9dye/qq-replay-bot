"""后台维护一个仅转发录像端口的 localhost.run SSH 连接。"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .tunnel import valid_hostname, valid_origin
from .tunnel_health import Journal,probe,ssh_error_category


def hostname_from_event(line: str) -> str:
    try:
        event = json.loads(line)
        if event.get('type') != 'v1.tcpip_forward.register.accepted':
            return ''
        identifier = event.get('id', '')
        hostname = identifier[len('localhost/'):]
        return hostname if identifier.startswith('localhost/') and valid_hostname(hostname) else ''
    except (ValueError, AttributeError, TypeError):
        return ''


def ssh_command(executable: Path, origin: str, data_dir: Path) -> list:
    if not valid_origin(origin):
        raise ValueError('无效的本机录像服务地址')
    known_hosts = (data_dir / 'tunnel-known-hosts').as_posix()
    return [str(executable), '-F', os.devnull, '-T', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile="{known_hosts}"',
            '-o', 'IdentityFile=none', '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes',
            '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-o', 'ExitOnForwardFailure=yes', '-R', f'80:127.0.0.1:{urlsplit(origin).port}',
            'nokey@localhost.run', '--', '--output', 'json']


class LocalTunnelWorker:
    def __init__(self, origin: str, data_dir: Path, instance: str, executable: Path):
        self.origin, self.data_dir = origin, Path(data_dir)
        self.instance, self.executable = instance, executable
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.process = None
        self.hostname = ''
        self.started_at=0.;self.verified_at=0.;self.last_health_at=0.
        self.health_failures=0;self.connect_failures=0;self.last_error=None
        self.offline_since=time.time();self.last_recovery_at=None;self.last_exit_code=None
        self.journal=Journal(self.data_dir/'logs');self.probe=probe
        self.health_wake=threading.Event()

    def status(self) -> dict:
        with self.lock:
            ready = bool(self.hostname and self.process is not None and self.process.poll() is None
                         and self.verified_at and 0<=time.time()-self.verified_at<=45 and self.last_error is None)
            return {'service': 'qq-replay-tunnel', 'instance': self.instance, 'origin': self.origin,
                    'hostname': self.hostname if ready else '', 'ready': ready,'diagnostics_version':2,
                    'public_verified_at':self.verified_at or None,'last_health_at':self.last_health_at or None,
                    'last_error':self.last_error,'health_failures':self.health_failures,
                    'connect_failures':self.connect_failures,'offline_since':self.offline_since,
                    'last_recovery_at':self.last_recovery_at,'last_exit_code':self.last_exit_code}

    def check_health(self):
        with self.lock:process,hostname,started=self.process,self.hostname,self.started_at
        if process is None or process.poll() is not None:return
        now=time.time()
        if not hostname:
            if now-started>30:
                with self.lock:
                    if self.process is not process:return
                    self.last_error='registration_timeout';self.verified_at=0
                self.journal.emit('registration_timeout',category='registration_timeout')
                process.terminate()
            return
        # Probe no token-bearing paths. An unavailable origin never causes SSH churn.
        local_error=self.probe(self.origin+'/health')
        public_error=None if local_error else self.probe('https://'+hostname+'/health')
        category=('origin_'+local_error) if local_error else ('public_'+public_error) if public_error else None
        restart=False;recovered=False;outage=0
        with self.lock:
            if self.process is not process or self.hostname!=hostname:return
            previous=self.last_error;self.last_health_at=now
            if category:
                self.verified_at=0;self.offline_since=self.offline_since or now
                self.health_failures=self.health_failures+1 if not local_error else 0
                self.last_error=category
                restart=not local_error and self.health_failures>=2 and now-started>=30
            else:
                recovered=self.offline_since is not None;outage=max(0,now-self.offline_since) if recovered else 0
                self.verified_at=now;self.health_failures=0;self.connect_failures=0;self.last_error=None
                if recovered:self.last_recovery_at=now;self.offline_since=None
        if category and (previous!=category or restart):
            self.journal.emit('health_failed',category=category,failures=self.health_failures)
        elif recovered:self.journal.emit('public_recovered',outage_seconds=round(outage,1),available=True)
        if restart:
            self.journal.emit('unhealthy_ssh_reconnect',category=category,failures=self.health_failures)
            with self.lock:
                if self.process is process and process.poll() is None:process.terminate()

    def health_loop(self):
        while not self.stop.is_set():
            try:self.check_health()
            except Exception:self.journal.emit('health_check_error',category='internal_probe_error')
            self.health_wake.wait(15);self.health_wake.clear()

    def close(self):
        self.stop.set()
        self.health_wake.set()
        with self.lock:
            process, self.hostname = self.process, ''
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def connect_loop(self):
        delay = 3
        while not self.stop.is_set():
            process = None
            failure_kind='ssh_exit'
            try:
                process = subprocess.Popen(ssh_command(self.executable, self.origin, self.data_dir),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
                with self.lock:
                    self.process = process
                    self.started_at=time.time();self.verified_at=0;self.health_failures=0
                self.journal.emit('ssh_attempt')
                for line in process.stdout:
                    category=ssh_error_category(line)
                    if category and (failure_kind=='ssh_exit' or category!='remote_closed'):failure_kind=category
                    hostname = hostname_from_event(line)
                    if hostname:
                        with self.lock:
                            self.hostname = hostname
                            self.verified_at=0
                        self.health_wake.set()
                        delay = 3
                        self.journal.emit('ssh_registered',available=False)
                        print(time.strftime('%Y-%m-%d %H:%M:%S'), 'SSH registered; public health verification pending', flush=True)
                    if self.stop.is_set():
                        break
                process.wait(timeout=5)
                with self.lock:
                    self.last_exit_code=process.returncode;self.connect_failures+=1
                    self.offline_since=self.offline_since or time.time()
                    if not self.last_error:self.last_error=failure_kind
                self.journal.emit('ssh_disconnected',category=failure_kind,exit_code=process.returncode,
                                  failures=self.connect_failures,retry_seconds=delay)
                print(time.strftime('%Y-%m-%d %H:%M:%S'), 'SSH disconnected:', process.returncode, flush=True)
            except (OSError, subprocess.SubprocessError) as exc:
                with self.lock:
                    self.last_error='ssh_spawn_error';self.connect_failures+=1
                    self.offline_since=self.offline_since or time.time()
                self.journal.emit('ssh_start_failed',category='ssh_spawn_error',failures=self.connect_failures,retry_seconds=delay)
                print(time.strftime('%Y-%m-%d %H:%M:%S'), 'Tunnel error:', type(exc).__name__, flush=True)
            finally:
                with self.lock:
                    self.hostname = ''
                    self.process = None
                    self.verified_at=0
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    if process.stdout:
                        process.stdout.close()
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 60)


class StatusServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_server(worker: LocalTunnelWorker, port: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/status':
                self.send_error(404)
                return
            body = json.dumps(worker.status()).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return StatusServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin', required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--ssh', type=Path, required=True)
    parser.add_argument('--metrics-port', type=int, required=True)
    args = parser.parse_args()
    if not valid_origin(args.origin):
        parser.error('只允许本机录像 HTTP 服务')
    args.data_dir.mkdir(parents=True, exist_ok=True)
    worker = LocalTunnelWorker(args.origin, args.data_dir, args.instance, args.ssh)
    server = make_server(worker, args.metrics_port)
    reader = threading.Thread(target=worker.connect_loop, daemon=True)
    reader.start()
    health_reader=threading.Thread(target=worker.health_loop,daemon=True)
    health_reader.start()

    def idle_shutdown():
        missing_since = time.monotonic()
        while not worker.stop.wait(10):
            try:
                with socket.create_connection(('127.0.0.1', urlsplit(args.origin).port), timeout=1):
                    missing_since = time.monotonic()
            except OSError:
                if time.monotonic() - missing_since > 120:
                    worker.close()
                    server.shutdown()
                    return
    threading.Thread(target=idle_shutdown, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        worker.close()
        server.server_close()
        reader.join(timeout=10)
        health_reader.join(timeout=10)


if __name__ == '__main__':
    main()
