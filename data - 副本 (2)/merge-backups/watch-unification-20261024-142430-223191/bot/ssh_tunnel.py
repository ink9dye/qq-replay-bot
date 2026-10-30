"""后台维护一个仅转发录像端口的 localhost.run SSH 连接。"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .tunnel import valid_hostname, valid_origin


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

    def status(self) -> dict:
        with self.lock:
            ready = bool(self.hostname and self.process is not None and self.process.poll() is None)
            return {'service': 'qq-replay-tunnel', 'instance': self.instance, 'origin': self.origin,
                    'hostname': self.hostname if ready else '', 'ready': ready}

    def close(self):
        self.stop.set()
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
            try:
                process = subprocess.Popen(ssh_command(self.executable, self.origin, self.data_dir),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
                with self.lock:
                    self.process = process
                for line in process.stdout:
                    hostname = hostname_from_event(line)
                    if hostname:
                        with self.lock:
                            self.hostname = hostname
                        delay = 3
                        print(time.strftime('%Y-%m-%d %H:%M:%S'), 'localhost.run connected:', hostname, flush=True)
                    if self.stop.is_set():
                        break
                process.wait(timeout=5)
                print(time.strftime('%Y-%m-%d %H:%M:%S'), 'SSH disconnected:', process.returncode, flush=True)
            except (OSError, subprocess.SubprocessError) as exc:
                print(time.strftime('%Y-%m-%d %H:%M:%S'), 'Tunnel error:', type(exc).__name__, flush=True)
            finally:
                with self.lock:
                    self.hostname = ''
                    self.process = None
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


if __name__ == '__main__':
    main()
