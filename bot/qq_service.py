# -*- coding: utf-8 -*-
"""QQ 收发瘦入口：python -m bot.qq_service。

只启动 QQ 任务，占 data/bot.lock；观战/分享在 collector 进程里跑。
M1 仍让 QQ 进程打开同一个 bot.sqlite3（多进程共享库，不是单写者）；
M3 才会把 QQ 的写请求收敛到 collector。
"""

from __future__ import annotations

from pathlib import Path

from .launcher import StartupLock, install_close_handler, set_console_title
from .main import run_main, stop_active_service

ROOT = Path(__file__).resolve().parent.parent
ROLES = frozenset({"qq"})
LOCK_FILE = "bot.lock"


def main() -> None:
    import faulthandler
    log_dir = ROOT / 'data' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    fault = (log_dir / 'qq-faulthandler.log').open('a', encoding='utf-8')
    faulthandler.enable(file=fault, all_threads=True)
    set_console_title("QQ机器人 - bot.qq_service")
    lock = StartupLock(ROOT, LOCK_FILE)
    if not lock.acquire():
        print("已有QQ服务运行（data/bot.lock 被占用），本次不重复启动。", flush=True)
        return

    def cleanup() -> None:
        stop_active_service()
        lock.release()

    install_close_handler(cleanup)
    try:
        run_main(controlled_by_pycharm=False, roles=ROLES)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
