# -*- coding: utf-8 -*-
"""观战/采集瘦入口：python -m bot.collector。

只启动 watch + share，占 data/collector.lock；QQ 在另一个进程里跑。
"""

from __future__ import annotations

from pathlib import Path

from .launcher import StartupLock, install_close_handler, set_console_title
from .main import run_main, stop_active_service

ROOT = Path(__file__).resolve().parent.parent
ROLES = frozenset({"watch", "share"})
LOCK_FILE = "collector.lock"


def main() -> None:
    set_console_title("观战采集 - bot.collector")
    lock = StartupLock(ROOT, LOCK_FILE)
    if not lock.acquire():
        print("已有观战采集服务运行（data/collector.lock 被占用），本次不重复启动。", flush=True)
        return

    def cleanup() -> None:
        # 关窗时先停分享、关库，再释放锁；锁文件本身不删，避免和下一个进程抢创建。
        stop_active_service()
        lock.release()

    install_close_handler(cleanup)
    try:
        # 采集进程沿用 watch 入口的统计/前100/独立名单能力；分享服务也归它。
        run_main(controlled_by_pycharm=True, roles=ROLES)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
