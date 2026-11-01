# -*- coding: utf-8 -*-
"""独立分享进程（D27 候选）：只读 Store + /s/ 与 /stats/ + 隧道所有权。

目标：watch 停止/重启时，公网录像与统计链接不再断。
- 只读：Store(readonly=True, migrate=False)，不建表/ALTER/WAL，schema 由 collector 唯一升级。
- 端口：沿用 config.share.bind_port（默认 8787），测试时可直接传 bind_port=0。
- /health：service_roles=["share"]、share_only=true，**不带 watch_pid**，
  避免 launcher/pycharm_link 把分享进程误判成"watch 在跑"。
- /s/<token>/analysis-refresh 是 GET 触发写（重解析 + sidecar），本进程关掉（409），留在 watch。
- 隧道：config.share.tunnel_owner == "share" 时由本进程拥有；成功后写 data/share-public-base.json，
  QQ 侧读该文件取公网 base（失败回退 config.share.public_base_url）。
- 回退：config.share.mode=inline（默认）→ 本模块拒绝启动，仍由 watch 进程起分享服务。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_BASE_FILE = 'share-public-base.json'
MODE_PROCESS = 'process'


def share_public_base_path(data_dir: Path) -> Path:
    return Path(data_dir) / PUBLIC_BASE_FILE


def write_public_base(data_dir: Path, base: str, source: str = 'share') -> Path:
    """原子写公网 base；QQ 与其他进程读它拿链接前缀。"""
    path = share_public_base_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'url': (base or '').rstrip('/'), 'updated_at': time.time(),
                                'source': source}, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)
    return path


def read_public_base(data_dir: Path, fallback: str = '') -> str:
    path = share_public_base_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return str(payload.get('url') or fallback)
    except (OSError, ValueError):
        return fallback


def share_mode(config: dict) -> str:
    share = config.get('share') or {}
    mode = str(share.get('mode') or 'inline').strip().lower()
    return mode if mode in ('inline', MODE_PROCESS) else 'inline'


def build_share_app(config: dict, clock=None, bind_port=None):
    """构造只读分享 App（可被测试直接调用，不绑定端口）。"""
    from .store import Store
    from .share import ShareApp

    share_cfg = config.get('share') or {}
    data_dir = Path(str(config.get('data_dir') or (ROOT / 'data')))
    database = data_dir / 'bot.sqlite3'
    replay_root = Path(str(config.get('replay_dir') or (data_dir / 'replays')))
    clock = clock or time.time
    store = Store(database, readonly=True, migrate=False)

    required = int(share_cfg.get('require_schema_version') or 0)
    if required and store.schema_version() < required:
        raise RuntimeError(f'schema_version {store.schema_version()} < required {required}; 等 collector 升级')

    app = ShareApp(
        store, replay_root, clock,
        public_base_url=str(share_cfg.get('public_base_url') or ''),
        bind_host=str(share_cfg.get('bind_host') or '127.0.0.1'),
        bind_port=int(share_cfg.get('bind_port', 8787) if bind_port is None else bind_port),
        link_hours=float(share_cfg.get('link_hours') or 5),
    )
    app.allow_analysis_refresh = False
    app.share_only = True
    app.watch_runtime = {'service_roles': ['share'], 'share_only': True}
    try:
        from .statistics_portal import StatisticsPortal, owner_secret
        secret = owner_secret(data_dir / 'statistics-owner.key')
        app.statistics_portal = StatisticsPortal(database, replay_root, clock, secret)
    except (OSError, ValueError):
        app.statistics_portal = None
    return app


def health_payload(app) -> dict:
    """独立分享进程的 /health 语义：roles 只有 share，且没有 watch_pid。"""
    from retention_policy import RETENTION_DAYS
    return {'service': 'qq-replay-bot', 'status': 'running',
            'retention_days': RETENTION_DAYS,
            'service_roles': ['share'], 'share_only': True,
            'statistics_portal': app.statistics_portal is not None}


def build_tunnel(config: dict, app):
    """按 share.tunnel_owner 决定是否由本进程拥有隧道。"""
    share_cfg = config.get('share') or {}
    owner = str(share_cfg.get('tunnel_owner') or 'watch').strip().lower()
    if owner != 'share' or share_cfg.get('quick_tunnel') is not True:
        return None
    from .cpolar_tunnel import resolve_mode
    data_dir = Path(str(config.get('data_dir') or (ROOT / 'data')))
    try:
        mode = resolve_mode(share_cfg)
    except ValueError:
        mode = 'ssh'
    if mode == 'cpolar':
        from .cpolar_runtime import CpolarTunnel
        return CpolarTunnel(ROOT, data_dir, app.local_base(), share_cfg)
    from .tunnel import QuickTunnel
    return QuickTunnel(ROOT, data_dir, app.local_base(),
                       int(share_cfg.get('tunnel_metrics_port', 47879)))


class ShareService:
    """可测试的分享服务壳：start() 起 HTTP + 可选隧道，stop() 收尾。"""

    def __init__(self, config: dict, clock=None, bind_port=None, tunnel=None):
        self.config = config
        self.app = build_share_app(config, clock=clock, bind_port=bind_port)
        self.tunnel = tunnel if tunnel is not None else build_tunnel(config, self.app)
        if self.tunnel is not None:
            self.app.public_url_provider = self.tunnel.current_url
        from .share import ShareServer
        self.server = ShareServer(self.app)
        self._stop = threading.Event()
        self._thread = None
        self.data_dir = Path(str(config.get('data_dir') or (ROOT / 'data')))

    def health(self) -> dict:
        return health_payload(self.app)

    def start(self) -> dict:
        self.server.start()
        if self.tunnel is not None:
            self._thread = threading.Thread(target=self._tunnel_loop, name='share-tunnel', daemon=True)
            self._thread.start()
        return self.health()

    def _tunnel_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tunnel.origin = self.app.local_base()
                self.tunnel.ensure_started()
                base = self.tunnel.current_url() or ''
                if base:
                    write_public_base(self.data_dir, base, source='share-tunnel')
            except Exception as exc:  # 隧道失败不能影响本地分享
                print(f'分享隧道暂不可用：{exc}', flush=True)
            self._stop.wait(15)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        self.server.stop()
        if self.tunnel is not None:
            stop = getattr(self.tunnel, 'stop', None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        try:
            self.app.store._db.close()
        except Exception:
            pass


def main() -> int:
    from .launcher import StartupLock
    from .setup import read_config
    from .watch_core import configure_paths
    config_path = ROOT / 'config.json'
    if not config_path.is_file():
        print('缺少 config.json。', flush=True)
        return 2
    try:
        config = read_config(config_path)
    except Exception as exc:
        print(f'config.json 无法读取：{exc}', flush=True)
        return 2
    if share_mode(config) != MODE_PROCESS:
        print('share.mode 不是 process，独立分享进程不启动（仍由 watch 进程内联提供）。', flush=True)
        return 3
    # 运行期持有 data/share.lock：launcher/start-all 靠它判断"分享服务已在跑"，
    # 重复启动的进程在这里直接退出，绝不再去 bind 8787（避免 WinError 10048）。
    lock = StartupLock(ROOT, 'share.lock')
    if not lock.acquire():
        print('已有分享服务运行（data/share.lock 被占用），本进程退出，不绑定 8787。', flush=True)
        return 4
    data_dir = Path(str(config.get('data_dir') or (ROOT / 'data')))
    ygopro_dir = Path(str(config.get('ygopro_dir') or r"D:\MyCardLibrary\ygopro"))
    service = None
    try:
        configure_paths(ygopro_dir, data_dir)
        service = ShareService(config)
        health = service.start()
        print('分享服务（独立进程，只读）：', json.dumps(health, ensure_ascii=False), flush=True)
        print(f'监听 http://{service.app.bind_host}:{service.app.bind_port}/', flush=True)
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        if service is not None:
            try:
                service.stop()
            except Exception:
                pass
        lock.release()
    return 0


if __name__ == '__main__':
    sys.exit(main())
