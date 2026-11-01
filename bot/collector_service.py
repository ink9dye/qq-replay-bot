# -*- coding: utf-8 -*-
"""M3 collector 骨架（D26 候选，尚未接入 watch/QQ/8788）。

作用：SQLite 的唯一写者。只监听 127.0.0.1，Bearer token 鉴权，写请求按 request_id 幂等，
其余进程改为 `POST /m3/write`，读路径继续用只读连接。

本文件今晚只到"骨架 + B0/B1/B2 少量候选 op"：
- B0：建表/迁移/`PRAGMA user_version`（唯一执行者）。
- B1：insert_stats_link / purge_stats_links / insert_link / upsert_subscription /
      delete_subscription / set_notify / enqueue_notification / mark_sent / mark_skipped / mark_retry。
- B2：add_replay（采集登记）。
- B3（stat_* 批量）**不做**，按 D26 补的契约另行评审。

调用方（watch/QQ/8788）接入改造不在本轮；接口评审文档见 决策与阻塞 D26/D26 补。
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8790
TOKEN_FILE = 'm3-token'
MAX_BODY = 512 * 1024
SCHEMA_VERSION = 1
EPOCH_DEFAULT = 1


def token_path(data_dir: Path) -> Path:
    return Path(data_dir) / TOKEN_FILE


def read_token(data_dir: Path) -> str:
    try:
        return token_path(data_dir).read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def _params(payload: dict) -> dict:
    value = payload.get('params')
    return value if isinstance(value, dict) else {}


def op_insert_stats_link(store, p: dict):
    store.insert_stats_link(str(p['token']), str(p['user_id']), float(p['created_at']), float(p['expires_at']))
    return {'ok': True}


def op_purge_stats_links(store, p: dict):
    store.purge_stats_links(float(p['now']))
    return {'ok': True}


def op_insert_link(store, p: dict):
    store.insert_link(str(p['token']), str(p['user_id']), str(p['player']), float(p['created_at']),
                      float(p['expires_at']), [int(x) for x in p['replay_ids']], float(p['range_start']),
                      [int(x) for x in p['deck_replay_ids']] if p.get('deck_replay_ids') is not None else None)
    return {'ok': True}


def op_add_replay(store, p: dict):
    replay, created = store.add_replay(str(p['digest']), str(p['focus_player']), str(p['rel_path']),
                                       list(p['players']), dict(p.get('ranks') or {}),
                                       float(p['recorded_at']), p.get('saved_at'))
    return {'id': replay.get('id'), 'created': bool(created)}


def op_upsert_subscription(store, p: dict):
    row = store.upsert_subscription(str(p['user_id']), str(p['player']), p.get('rank_top'),
                                    bool(p.get('notify', True)), float(p['created_at']))
    return {'id': (row or {}).get('id')}


def op_delete_subscription(store, p: dict):
    return {'deleted': bool(store.delete_subscription(str(p['user_id']), str(p['player'])))}


def op_set_notify(store, p: dict):
    return {'updated': bool(store.set_notify(str(p['user_id']), str(p['player']), bool(p['notify'])))}


def op_enqueue_notification(store, p: dict):
    return {'queued': bool(store.enqueue_notification(str(p['user_id']), int(p['replay_id']), str(p['player'])))}


def op_mark_sent(store, p: dict):
    store.mark_sent(str(p['user_id']), int(p['replay_id']))
    return {'ok': True}


def op_mark_skipped(store, p: dict):
    store.mark_skipped(str(p['user_id']), int(p['replay_id']))
    return {'ok': True}


def op_mark_retry(store, p: dict):
    return {'status': store.mark_retry(str(p['user_id']), int(p['replay_id']), int(p.get('limit', 3)))}


OPS = {
    'insert_stats_link': op_insert_stats_link,
    'purge_stats_links': op_purge_stats_links,
    'insert_link': op_insert_link,
    'add_replay': op_add_replay,
    'upsert_subscription': op_upsert_subscription,
    'delete_subscription': op_delete_subscription,
    'set_notify': op_set_notify,
    'enqueue_notification': op_enqueue_notification,
    'mark_sent': op_mark_sent,
    'mark_skipped': op_mark_skipped,
    'mark_retry': op_mark_retry,
}
REQUIRED = {
    'insert_stats_link': ('token', 'user_id', 'created_at', 'expires_at'),
    'purge_stats_links': ('now',),
    'insert_link': ('token', 'user_id', 'player', 'created_at', 'expires_at', 'replay_ids', 'range_start'),
    'add_replay': ('digest', 'focus_player', 'rel_path', 'players', 'recorded_at'),
    'upsert_subscription': ('user_id', 'player', 'created_at'),
    'delete_subscription': ('user_id', 'player'),
    'set_notify': ('user_id', 'player', 'notify'),
    'enqueue_notification': ('user_id', 'replay_id', 'player'),
    'mark_sent': ('user_id', 'replay_id'),
    'mark_skipped': ('user_id', 'replay_id'),
    'mark_retry': ('user_id', 'replay_id'),
}


class Collector:
    """唯一写者：持有写连接、m3_requests 幂等表与 mode(epoch)。"""

    def __init__(self, config: dict, enabled: bool = True, epoch: int = EPOCH_DEFAULT):
        from .store import Store
        self.config = config
        self.data_dir = Path(str(config.get('data_dir') or (ROOT / 'data')))
        self.database = self.data_dir / 'bot.sqlite3'
        self.enabled = bool(enabled)
        self.epoch = int(epoch)
        self.store = Store(self.database)          # B0：建表/迁移/索引（唯一执行者）
        self._ensure_requests_table()
        self.store.set_schema_version(SCHEMA_VERSION)
        self.lock = threading.Lock()

    def _ensure_requests_table(self) -> None:
        self.store._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS m3_requests (
                request_id TEXT PRIMARY KEY,
                op TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS m3_requests_created ON m3_requests(created_at);
            """
        )
        self.store._db.commit()

    def health(self) -> dict:
        return {'service': 'collector', 'status': 'running', 'enabled': self.enabled,
                'epoch': self.epoch, 'schema_version': self.store.schema_version(),
                'ops': sorted(OPS), 'database': str(self.database)}

    def mode(self) -> dict:
        return {'enabled': self.enabled, 'epoch': self.epoch}

    def _known(self, request_id: str):
        row = self.store._db.execute(
            'SELECT result_json FROM m3_requests WHERE request_id=? AND state=?',
            (request_id, 'done')).fetchone()
        return json.loads(row['result_json']) if row and row['result_json'] else None

    def write(self, payload: dict) -> tuple[int, dict]:
        if not self.enabled:
            return 503, {'ok': False, 'error': 'disabled'}
        request_id = str(payload.get('request_id') or '').strip()
        op = str(payload.get('op') or '').strip()
        if not request_id or len(request_id) > 80:
            return 400, {'ok': False, 'error': 'bad_request_id'}
        if op not in OPS:
            return 400, {'ok': False, 'error': 'unknown_op', 'op': op}
        missing = [key for key in REQUIRED[op] if key not in _params(payload)]
        if missing:
            return 400, {'ok': False, 'error': 'missing_params', 'missing': missing}
        with self.lock:
            cached = self._known(request_id)
            if cached is not None:
                return 200, {'ok': True, 'result': cached, 'replayed': True}
            try:
                # 真原子：同一连接 BEGIN…COMMIT 包住"业务写 + m3_requests 幂等记录"；
                # 任一失败整体回滚，m3_requests 不会留下 done（调用方可用同一 request_id 重试）。
                with self.store.atomic():
                    result = OPS[op](self.store, _params(payload))
                    self.store._db.execute(
                        'INSERT INTO m3_requests(request_id, op, epoch, state, result_json, created_at) '
                        'VALUES(?,?,?,?,?,?)',
                        (request_id, op, self.epoch, 'done', json.dumps(result, ensure_ascii=False), time.time()))
            except (KeyError, TypeError, ValueError) as exc:
                return 400, {'ok': False, 'error': 'bad_params', 'detail': type(exc).__name__}
            except Exception as exc:  # 数据库忙/锁等：让调用方带同一 request_id 重试
                return 503, {'ok': False, 'error': 'store_busy', 'detail': type(exc).__name__}
            # 同 request_id 但第一次因业务冲突失败时，重试会再撞一次：此时 m3_requests 里没有 done 记录，
            # 仍返回失败；只有业务写入成功的那一次才留下 done 并返回首次结果。
            return 200, {'ok': True, 'result': result}

    def prune_requests(self, keep_seconds: float = 7 * 86400) -> int:
        cur = self.store._db.execute('DELETE FROM m3_requests WHERE created_at < ?',
                                     (time.time() - keep_seconds,))
        self.store._db.commit()
        return cur.rowcount

    def close(self) -> None:
        try:
            self.store._db.close()
        except Exception:
            pass


def make_handler(collector: Collector, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            return

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _local_only(self) -> bool:
            return self.client_address[0] in ('127.0.0.1', '::1')

        def _authorized(self) -> bool:
            header = self.headers.get('Authorization') or ''
            supplied = header[7:].strip() if header.lower().startswith('bearer ') else ''
            return bool(token) and bool(supplied) and hmac.compare_digest(supplied, token)

        def do_GET(self) -> None:
            path = self.path.split('?', 1)[0]
            if path == '/health':
                return self._send(200, collector.health())
            if path == '/m3/mode':
                return self._send(200, collector.mode())
            return self._send(404, {'ok': False, 'error': 'not_found'})

        def do_POST(self) -> None:
            if self.path.split('?', 1)[0] != '/m3/write':
                return self._send(404, {'ok': False, 'error': 'not_found'})
            if not self._local_only():
                return self._send(403, {'ok': False, 'error': 'local_only'})
            if not self._authorized():
                return self._send(403, {'ok': False, 'error': 'bad_token'})
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                return self._send(413 if length > MAX_BODY else 400, {'ok': False, 'error': 'bad_length'})
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {'ok': False, 'error': 'bad_json'})
            status, body = collector.write(payload if isinstance(payload, dict) else {})
            return self._send(status, body)

    return Handler


class CollectorServer:
    def __init__(self, collector: Collector, token: str, host: str = '127.0.0.1', port: int = DEFAULT_PORT):
        self.collector = collector
        self.token = token
        self.httpd = ThreadingHTTPServer((host, port), make_handler(collector, token))
        self.port = int(self.httpd.server_address[1])
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, name='m3-collector', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        close = getattr(self.collector, 'close', None)
        if callable(close):
            close()


def main() -> int:
    from .setup import read_config
    config_path = ROOT / 'config.json'
    if not config_path.is_file():
        print('缺少 config.json。', flush=True)
        return 2
    config = read_config(config_path)
    data_dir = Path(str(config.get('data_dir') or (ROOT / 'data')))
    token = read_token(data_dir)
    if not token:
        print(f'缺少 {token_path(data_dir)}（M3 Bearer token，由 owner 生成，仅本机可读）。', flush=True)
        return 2
    m3 = config.get('m3') or {}
    collector = Collector(config, enabled=bool(m3.get('enabled', False)), epoch=int(m3.get('epoch') or EPOCH_DEFAULT))
    server = CollectorServer(collector, token, port=int(m3.get('bind_port') or DEFAULT_PORT)).start()
    print('M3 collector 已启动：', json.dumps(collector.health(), ensure_ascii=False), flush=True)
    print(f'监听 http://127.0.0.1:{server.port}/（/health、/m3/mode、POST /m3/write）', flush=True)
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
