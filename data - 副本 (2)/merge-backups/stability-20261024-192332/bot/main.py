# -*- coding: utf-8 -*-
"""启动 QQ 观战录像机器人。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .service import BotService
from .watch_core import configure_paths
from .setup import read_config

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
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
    ygopro_dir = Path(str(config.get("ygopro_dir") or r"D:\MyCardLibrary\ygopro"))
    configure_paths(ygopro_dir, data_dir)
    service = BotService(config, ROOT)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        print("已停止")
    except OSError:
        service.share_server.stop()
        service.store.close()
        print("启动失败：分享端口可能被占用，或数据目录不可写。请先关闭重复运行的机器人。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
