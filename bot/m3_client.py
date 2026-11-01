# -*- coding: utf-8 -*-
"""M3 公共客户端（D26 拍板第 3/4 项）：outbox + 幂等 + epoch。

调用方（watch / QQ / 8788）统一用它提交写请求，不再直连写库：
- 写前落盘 `data/m3-outbox/<request_id>.json`，**收到 200 才删**；400 视为永久失败也删（不重试）。
- collector 不可用（连接失败 / 503）→ 保留 outbox，按 **1→60 秒指数退避**重试；
- outbox 上限 **1000**，满了返回 `outbox_full`（不丢已入队项，也不回退直写）；
- 启动时扫 outbox 重放（`flush()` 或后台 `start_worker()`）；
- **epoch 从 `/m3/mode` 取**（缓存 30 秒）；epoch 变化时清空旧 epoch 的 outbox，禁止跨 epoch 重放；
- 同一个 request_id 重发：已发送成功的直接复用首次结果；仍在队列里的不重复入队。

本模块只做"排队 + 发送"，不碰 SQLite；`m3.enabled=false` 时也不直写（按拍板：进 outbox 排队）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE_URL = 'http://127.0.0.1:8790'
MAX_OUTBOX = 1000
MODE_TTL = 30.0
BACKOFF_MAX = 60.0


def _no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class M3Client:
    def __init__(self, data_dir, *, base_url=DEFAULT_BASE_URL, token='', timeout=5.0,
                 outbox_dir=None, max_outbox=MAX_OUTBOX, now=time.time, post=None, get_mode=None,
                 auto_worker=False):
        self.data_dir = Path(data_dir)
        self.outbox_dir = Path(outbox_dir) if outbox_dir else self.data_dir / 'm3-outbox'
        self.base_url = str(base_url).rstrip('/')
        self.token = str(token or '')
        self.timeout = float(timeout)
        self.max_outbox = int(max_outbox)
        self.now = now
        self._post = post or self._post_http
        self._get_mode = get_mode or self._get_mode_http
        self._mode_cache = None
        self._mode_at = 0.0
        self.epoch = None
        self._results = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self._load_epoch()
        if auto_worker:
            self.start_worker()

    # ---- 网络 ----
    def _post_http(self, url, payload, headers):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with _no_proxy_opener().open(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read(8192))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read(8192))
            except (ValueError, OSError):
                return exc.code, {}
        except OSError:
            return 0, {}

    def _get_mode_http(self, url):
        try:
            with _no_proxy_opener().open(url, timeout=self.timeout) as response:
                return json.loads(response.read(4096))
        except (OSError, ValueError, urllib.error.HTTPError):
            return None

    # ---- epoch ----
    def _fetch_mode(self):
        return self._get_mode(f'{self.base_url}/m3/mode')

    def mode(self, refresh=False):
        moment = self.now()
        if not refresh and self._mode_cache is not None and moment - self._mode_at <= MODE_TTL:
            return self._mode_cache
        payload = self._fetch_mode()
        if isinstance(payload, dict):
            self._mode_cache = payload
            self._mode_at = moment
            self._apply_epoch(payload.get('epoch'))
            return payload
        return None

    def _load_epoch(self):
        payload = self.mode(refresh=True)
        if isinstance(payload, dict):
            self._apply_epoch(payload.get('epoch'))

    def _apply_epoch(self, epoch):
        if epoch is None:
            return
        epoch = int(epoch)
        if self.epoch is None:
            self.epoch = epoch
            return
        if epoch == self.epoch:
            return
        cleared = self.clear_outbox(epoch)
        self.epoch = epoch
        return cleared

    def clear_outbox(self, epoch=None):
        """清掉非当前 epoch 的 outbox（epoch 变化时调用）；返回删除数量。"""
        removed = 0
        for path in self.outbox_dir.glob('*.json'):
            payload = self._read(path)
            if payload is None:
                path.unlink(missing_ok=True)
                removed += 1
                continue
            if epoch is not None and int(payload.get('epoch') or 0) != int(epoch):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    # ---- outbox ----
    def _path_for(self, request_id):
        return self.outbox_dir / f'{request_id}.json'

    def _read(self, path):
        try:
            payload = json.loads(Path(path).read_text(encoding='utf-8'))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    def _write(self, payload):
        path = self._path_for(payload['request_id'])
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
        return path

    def pending(self):
        items = []
        for path in self.outbox_dir.glob('*.json'):
            payload = self._read(path)
            if payload is not None:
                items.append(payload)
        items.sort(key=lambda item: (float(item.get('created_at') or 0), item.get('request_id')))
        return items

    def _enqueue(self, op, params, request_id=None):
        existing_result = self._results.get(request_id) if request_id else None
        if existing_result is not None:
            return {'ok': True, 'result': existing_result, 'cached': True}
        if request_id and self._path_for(request_id).exists():
            return {'ok': False, 'queued': True, 'duplicate': True, 'request_id': request_id}
        if len(self.pending()) >= self.max_outbox:
            return {'ok': False, 'queued': False, 'error': 'outbox_full'}
        request_id = request_id or uuid.uuid4().hex
        payload = {'request_id': request_id, 'op': op, 'params': params,
                   'epoch': self.epoch, 'created_at': self.now(), 'attempts': 0, 'next_at': 0.0}
        self._write(payload)
        return payload

    @staticmethod
    def backoff_seconds(attempts):
        return min(BACKOFF_MAX, float(max(1, 2 ** max(0, int(attempts) - 1))))

    def _send(self, payload):
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        body = {'request_id': payload['request_id'], 'op': payload['op'], 'params': payload['params']}
        status, response = self._post(f'{self.base_url}/m3/write', body, headers)
        return status, response

    def _finish(self, payload, result):
        self._results[payload['request_id']] = result
        self._path_for(payload['request_id']).unlink(missing_ok=True)

    def submit(self, op, params, request_id=None):
        """提交一次写：成功返回 result；失败返回 queued/error（不直写数据库）。"""
        with self._lock:
            mode = self.mode(refresh=True) if self._mode_cache is None else self.mode()
            if isinstance(mode, dict) and mode.get('epoch') is not None:
                self._apply_epoch(mode.get('epoch'))
            existing_result = self._results.get(request_id) if request_id else None
            if existing_result is not None:
                return {'ok': True, 'result': existing_result, 'cached': True}
            if request_id and self._path_for(request_id).exists():
                return self.flush_one(request_id) or {'ok': False, 'queued': True, 'duplicate': True,
                                                      'request_id': request_id}
            queued = self._enqueue(op, params, request_id)
            if 'request_id' not in queued:
                return queued
            return self.flush_one(queued['request_id']) or {'ok': False, 'queued': True,
                                                             'request_id': queued['request_id']}

    def flush_one(self, request_id):
        path = self._path_for(request_id)
        payload = self._read(path)
        if payload is None:
            return self._results.get(request_id) and {'ok': True, 'result': self._results[request_id], 'cached': True}
        epoch = self.epoch
        if epoch is not None and int(payload.get('epoch') or 0) != int(epoch):
            path.unlink(missing_ok=True)
            return {'ok': False, 'dropped': 'epoch_mismatch', 'request_id': request_id}
        status, response = self._send(payload)
        if status == 200 and isinstance(response, dict) and response.get('ok'):
            result = response.get('result')
            self._finish(payload, result)
            return {'ok': True, 'result': result, 'replayed': bool(response.get('replayed'))}
        if 400 <= status < 500 and status not in (408, 429):
            path.unlink(missing_ok=True)
            return {'ok': False, 'error': (response or {}).get('error') or f'http_{status}',
                    'request_id': request_id}
        payload['attempts'] = int(payload.get('attempts') or 0) + 1
        payload['next_at'] = self.now() + self.backoff_seconds(payload['attempts'])
        self._write(payload)
        return None

    def flush(self, limit=None):
        """按 created_at 顺序重放 outbox；返回（成功数, 仍待重试数）。"""
        done = pending = 0
        for item in self.pending()[:limit] if limit else self.pending():
            outcome = self.flush_one(item['request_id'])
            if outcome and outcome.get('ok'):
                done += 1
            else:
                pending += 1
        return done, pending

    def flush_due(self):
        due = [item for item in self.pending() if float(item.get('next_at') or 0) <= self.now()]
        done = pending = 0
        for item in due:
            outcome = self.flush_one(item['request_id'])
            if outcome and outcome.get('ok'):
                done += 1
            else:
                pending += 1
        return done, pending

    # ---- 后台重放（可选）----
    def start_worker(self, interval=5.0):
        if self._thread is not None:
            return self
        def loop():
            while not self._stop.is_set():
                try:
                    self.flush_due()
                except Exception:
                    pass
                self._stop.wait(interval)
        self._thread = threading.Thread(target=loop, name='m3-outbox', daemon=True)
        self._thread.start()
        return self

    def stop_worker(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
            self._thread = None
