# -*- coding: utf-8 -*-
"""半自动切换分享模式：inline → process（D27 收尾工具）。

流程（每一步失败都会回退）：
1) 提示用户在 PyCharm 点 Stop；轮询 8787 直到空闲（连接拒绝或 health 里没有 watch 角色）。
   **本工具绝不按名字杀进程**，只提示、只等，也只 terminate 自己刚启动的 share 子进程。
2) 备份 config.json → `config.json.bak-<时间戳>`，写 `share.mode="process"`、`share.tunnel_owner="share"`。
3) 启动 `python -m bot.share_service`（只启动本工具自己创建的子进程），等 8787 /health 变成 roles=["share"]；
   配了 quick_tunnel 时再等 `data/share-public-base.json`。
4) 调 `bot.pycharm_link.launch()` 拉起 watch；等 `data/watch-status.json` 新鲜（updated_at ≤ 10 秒）。
5) 验收：8787 是 share（无 watch_pid）、8788 /health 正常、伪造 /stats/ 与 /s/ 都是 404。
6) 任一失败：停掉自己启动的 share、恢复 config 备份、重启 watch（pycharm_link.launch），写证据并退出非 0。

用法：python tools/switch_share_mode.py [--yes] [--timeout 60]
不加 --yes 只做只读预检并打印计划，不做任何改动。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAMP = time.strftime('%Y%m%d-%H%M%S')
FORGED_TOKEN = 'x' * 43


def _no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, timeout=3.0):
    """GET 并解析 JSON；连接失败返回 None（用于"8787 是否空闲"）。"""
    try:
        with _no_proxy_opener().open(url, timeout=timeout) as response:
            return json.loads(response.read(8192))
    except (OSError, ValueError, urllib.error.HTTPError):
        return None


def http_status(url, timeout=3.0):
    try:
        with _no_proxy_opener().open(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return None


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SwitchController:
    """可注入依赖的切换器；测试用 fake 替换网络/子进程/pycharm_link。"""

    def __init__(self, *, root=ROOT, now=time.time, sleep=time.sleep, print_fn=print,
                 http_json=None, http_status=None, share_factory=None, watch_launcher=None,
                 stop_share_fn=None, watch_status_reader=None, data_dir=None, config_path=None,
                 evidence_dir=None, timeout=60.0):
        self.root = Path(root)
        self.now = now
        self.sleep = sleep
        self.print = print_fn
        self.http_json = http_json or http_json_impl
        self.http_status = http_status or http_status_impl
        self.share_factory = share_factory or self._start_share_process
        self.watch_launcher = watch_launcher or self._launch_watch
        self.stop_share_fn = stop_share_fn or self._stop_share_process
        self.watch_status_reader = watch_status_reader or self._read_watch_status
        self.config_path = Path(config_path or self.root / 'config.json')
        self.data_dir = Path(data_dir or self.root / 'data')
        self.evidence_dir = Path(evidence_dir or self.root / 'data' / 'logs')
        self.timeout = float(timeout)
        self.share_process = None
        self.backup_path = None
        self.steps = []
        self.started_at = self.now()

    # ---- 基础动作 ----
    def _record(self, step, ok, detail=''):
        self.steps.append({'step': step, 'ok': bool(ok), 'detail': str(detail), 'at': round(self.now(), 3)})
        self.print(f"[{'OK' if ok else 'FAIL'}] {step}{('：' + str(detail)) if detail else ''}")

    def health8787(self):
        return self.http_json('http://127.0.0.1:8787/health')

    def health8788(self):
        return self.http_json('http://127.0.0.1:8788/health')

    def _start_share_process(self):
        env = dict(os.environ, PYTHONUTF8='1')
        return subprocess.Popen([sys.executable, '-m', 'bot.share_service'], cwd=str(self.root), env=env)

    def _stop_share_process(self, process):
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired, AttributeError):
            pass

    def _launch_watch(self):
        from bot import pycharm_link
        return bool(pycharm_link.launch(self.root))

    def _read_watch_status(self):
        from bot import watch_status
        return watch_status.read(self.data_dir)

    # ---- 步骤 ----
    def wait_idle(self):
        self.print('请先在 PyCharm 点 Stop 停止当前 watch（本工具只等待，不按名字杀进程）。')
        deadline = self.now() + self.timeout
        while self.now() < deadline:
            health = self.health8787()
            if health is None:
                self._record('等待 8787 空闲', True, '连接已断开')
                return True
            roles = health.get('service_roles') if isinstance(health, dict) else None
            if isinstance(roles, list) and 'watch' not in roles:
                self._record('等待 8787 空闲', True, f'roles={roles}')
                return True
            self.sleep(2)
        self._record('等待 8787 空闲', False, f'{self.timeout:g} 秒内 watch 仍在运行')
        return False

    def patch_config(self):
        if not self.config_path.is_file():
            self._record('备份并修改 config.json', False, '缺少 config.json')
            return None
        self.backup_path = self.config_path.with_name(self.config_path.name + f'.bak-{STAMP}')
        shutil.copy2(self.config_path, self.backup_path)
        if file_hash(self.backup_path) != file_hash(self.config_path):
            self._record('备份 config.json', False, '备份校验不一致')
            return None
        try:
            config = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            self._record('修改 config.json', False, f'读取失败 {type(exc).__name__}')
            return None
        share = config.setdefault('share', {})
        share['mode'] = 'process'
        share['tunnel_owner'] = 'share'
        temporary = self.config_path.with_name(self.config_path.name + '.tmp')
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.config_path)
        self._record('备份并修改 config.json', True, f'mode=process，备份 {self.backup_path.name}')
        return config

    def start_share(self):
        try:
            self.share_process = self.share_factory()
        except Exception as exc:
            self._record('启动 bot.share_service', False, f'{type(exc).__name__}: {exc}')
            return False
        self._record('启动 bot.share_service', True, f'pid={getattr(self.share_process, "pid", "?")}')
        return True

    def wait_share_health(self, quick_tunnel=False):
        deadline = self.now() + self.timeout
        while self.now() < deadline:
            health = self.health8787()
            if isinstance(health, dict) and health.get('service_roles') == ['share']:
                self._record('等待 8787 变成 share', True, 'roles=["share"]')
                if quick_tunnel:
                    base_file = self.data_dir / 'share-public-base.json'
                    while self.now() < deadline and not base_file.is_file():
                        self.sleep(2)
                    if not base_file.is_file():
                        self._record('等待 share-public-base.json', False, 'quick_tunnel 已开但 60 秒内未生成')
                        return False
                    self._record('等待 share-public-base.json', True, '已生成')
                return True
            self.sleep(2)
        self._record('等待 8787 变成 share', False, f'{self.timeout:g} 秒内仍是旧服务')
        return False

    def start_watch(self):
        try:
            ok = self.watch_launcher()
        except Exception as exc:
            self._record('请求 PyCharm 启动 watch', False, f'{type(exc).__name__}: {exc}')
            return False
        self._record('请求 PyCharm 启动 watch', bool(ok), '已提交' if ok else '未提交')
        return bool(ok)

    def wait_watch_status(self):
        deadline = self.now() + self.timeout
        while self.now() < deadline:
            payload = self.watch_status_reader()
            if isinstance(payload, dict):
                updated = float(payload.get('updated_at') or 0)
                if self.now() - updated <= 10:
                    self._record('等待 watch-status.json 新鲜', True, f'pid={payload.get("pid")}')
                    return True
            self.sleep(2)
        self._record('等待 watch-status.json 新鲜', False, '10 秒新鲜度未达成')
        return False

    def verify(self):
        health = self.health8787()
        ok_share = isinstance(health, dict) and health.get('service_roles') == ['share'] and 'watch_pid' not in health
        self._record('验收：8787 是 share 且无 watch_pid', ok_share, json.dumps(health, ensure_ascii=False)[:160])
        web = self.health8788()
        ok_web = isinstance(web, dict) and web.get('service') == 'qq-statistics-web'
        self._record('验收：8788 /health 正常', ok_web, str((web or {}).get('status')))
        stats_status = self.http_status(f'http://127.0.0.1:8787/stats/{FORGED_TOKEN}')
        self._record('验收：伪造 /stats/ 返回 404', stats_status == 404, f'status={stats_status}')
        share_status = self.http_status(f'http://127.0.0.1:8787/s/{FORGED_TOKEN}')
        self._record('验收：伪造 /s/ 返回 404', share_status == 404, f'status={share_status}')
        return ok_share and ok_web and stats_status == 404 and share_status == 404

    def rollback(self, reason):
        self._record('开始回退', True, reason)
        if self.share_process is not None:
            self.stop_share_fn(self.share_process)
            self.share_process = None
            self._record('停止本次启动的 share 进程', True, '仅 terminate 自己的子进程')
        if self.backup_path is not None and self.backup_path.is_file():
            shutil.copy2(self.backup_path, self.config_path)
            restored = file_hash(self.config_path) == file_hash(self.backup_path)
            self._record('恢复 config.json', restored, f'hash 一致={restored}')
        try:
            self.watch_launcher()
            self._record('回退：重新请求 watch', True, '已提交')
        except Exception as exc:
            self._record('回退：重新请求 watch', False, f'{type(exc).__name__}: {exc}')
        self.write_evidence(ok=False, reason=reason)
        return 1

    def write_evidence(self, ok, reason=''):
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / f'switch-share-mode-{STAMP}.json'
        payload = {'ok': bool(ok), 'reason': reason, 'steps': self.steps,
                   'config': str(self.config_path), 'backup': str(self.backup_path) if self.backup_path else None,
                   'config_sha256': file_hash(self.config_path) if self.config_path.is_file() else None,
                   'started_at': round(self.started_at, 3), 'finished_at': round(self.now(), 3)}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        self.print(f'证据：{path}')
        return payload

    def run(self, yes=False):
        if not yes:
            health = self.health8787()
            self.print('预检（未加 --yes，不做任何改动）：')
            self.print(f'  8787 /health = {json.dumps(health, ensure_ascii=False) if health else "连接失败（空闲）"}')
            self.print('  计划：等你在 PyCharm 点 Stop → 备份并改 config → 起 share → 起 watch → 验收；失败自动回退。')
            return 0
        if not self.wait_idle():
            return self._fail('等待 8787 空闲超时')
        config = self.patch_config()
        if config is None:
            return self._fail('备份或修改 config.json 失败')
        quick_tunnel = bool((config.get('share') or {}).get('quick_tunnel'))
        if not self.start_share():
            return self.rollback('share 启动失败')
        if not self.wait_share_health(quick_tunnel):
            return self.rollback('share 健康检查未通过')
        if not self.start_watch():
            return self.rollback('watch 未启动')
        if not self.wait_watch_status():
            return self.rollback('watch-status.json 不新鲜')
        if not self.verify():
            return self.rollback('验收未通过')
        self._record('切换完成', True, 'inline → process')
        self.write_evidence(ok=True)
        return 0

    def _fail(self, reason):
        self._record('中止', False, reason)
        self.write_evidence(ok=False, reason=reason)
        return 1


def http_json_impl(url, timeout=3.0):
    return http_json(url, timeout=timeout)


def http_status_impl(url, timeout=3.0):
    return http_status(url, timeout=timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description='半自动切换分享模式 inline → process（只 terminate 自己启动的子进程）')
    parser.add_argument('--yes', action='store_true', help='真正执行切换（缺省只预检）')
    parser.add_argument('--timeout', type=float, default=60.0, help='每步等待秒数')
    args = parser.parse_args(argv)
    controller = SwitchController(timeout=args.timeout)
    return controller.run(yes=args.yes)


if __name__ == '__main__':
    sys.exit(main())
