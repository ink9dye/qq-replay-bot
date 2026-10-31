# -*- coding: utf-8 -*-
"""启动 QQ 观战录像机器人。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from .roles import ALL_ROLES, normalize_roles, roles_from_config
from .service import BotService
from .watch_core import configure_paths
from .setup import read_config
from .launcher import startup_lock

ROOT = Path(__file__).resolve().parent.parent
_ACTIVE_SERVICE: BotService | None = None


def stop_active_service() -> None:
    """控制台窗口关闭时用：停分享服务、关数据库；启动锁由入口模块释放。"""
    global _ACTIVE_SERVICE
    service, _ACTIVE_SERVICE = _ACTIVE_SERVICE, None
    if service is None:
        return
    try:
        service.share_server.stop()
    except Exception:
        pass
    try:
        service.store.close()
    except Exception:
        pass


def run_main(controlled_by_pycharm=False, roles=None) -> None:
    global _ACTIVE_SERVICE
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        # 显式 --roles 先校验，参数写错时不用等到读完配置才报。
        requested_roles = None if roles is None else normalize_roles(roles)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from None
    path = ROOT / "config.json"
    if not path.is_file():
        print("缺少 config.json。")
        print("请把 config.example.json 复制为 config.json，再填写萌卡账号、NapCat 接口 Token 和授权 QQ。")
        print("公网分享还要填写 share.public_base_url。本机的 127.0.0.1 不能当作好友打开的地址。")
        raise SystemExit(2)
    try:
        config = read_config(path)
    except ValueError:
        print("config.json 无法读取或格式错误，请运行 setup.bat 检查配置。")
        raise SystemExit(2) from None
    data_dir = Path(str(config.get("data_dir") or (ROOT / "data")))
    if controlled_by_pycharm:
        config['_pycharm_watch_entry'] = True
    # 不带 --roles 的入口（PyCharm 固定 watch Run 就是这种）按 config.json 决定角色：
    # service_mode=split 时本进程只做观战+分享，QQ 在独立窗口；unified 缺省仍全开。
    active_roles = roles_from_config(config, requested_roles)
    ygopro_dir = Path(str(config.get("ygopro_dir") or r"D:\MyCardLibrary\ygopro"))
    configure_paths(ygopro_dir, data_dir)
    service = BotService(config, ROOT, roles=active_roles)
    _ACTIVE_SERVICE = service
    if active_roles != ALL_ROLES:
        labels = {"watch": "观战录像", "qq": "QQ 收发", "share": "分享服务"}
        print("服务拆分：本进程只跑 " + "、".join(
            labels[role] for role in ("watch", "qq", "share") if role in active_roles)
            + "；关闭本窗口只停这些。", flush=True)
    if controlled_by_pycharm and "share" in active_roles:
        # 全开与拆分都可能是 PyCharm 固定 Run 的窗口；健康检查用它做“已在运行”的幂等判断。
        entry = "pycharm/watch.py" if os.environ.get("QQ_WATCH_IN_PYCHARM") == "1" else "bot.collector"
        service.share.watch_runtime = {
            'watch_entry': entry,
            'watch_pid': os.getpid(),
            'service_roles': sorted(active_roles),
        }
    try:
        asyncio.run(service.run(active_roles))
    except KeyboardInterrupt:
        print("已停止")
    except OSError:
        service.share_server.stop()
        service.store.close()
        print("启动失败：分享端口可能被占用，或数据目录不可写。请先关闭重复运行的机器人。")
        raise SystemExit(1) from None
    finally:
        _ACTIVE_SERVICE = None


def main() -> None:
    # 兼容旧命令，但观战不再在这个入口的后台进程内启动。
    from .pycharm_link import main as launch_pycharm
    launch_pycharm()


if __name__ == "__main__":
    main()
