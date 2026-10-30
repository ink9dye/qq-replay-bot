"""本机配置向导和只读检查，不输出账号密码或接口密钥。"""
from __future__ import annotations

import argparse
import asyncio
import copy
import getpass
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .qq import NapCatTransport, connection_hint

ROOT = Path(__file__).resolve().parent.parent


def read_config(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("配置无法读取，请检查 JSON 格式；文件原文不会显示。") from None
    if not isinstance(value, dict):
        raise ValueError("配置必须是 JSON 对象。")
    for key in ("mycard", "qq", "share", "watch"):
        if key in value and not isinstance(value[key], dict):
            raise ValueError(f"配置中的 {key} 必须是对象。")
    return value


def initial_config(root: Path) -> dict:
    config = read_config(root / "config.example.json")
    config["data_dir"] = str(root / "data")
    return config


def parse_users(text: str) -> list[int]:
    fields = [part for part in re.split(r"[,，;；\s]+", text.strip()) if part]
    if not fields or any(not re.fullmatch(r"[1-9][0-9]{4,11}", part) for part in fields):
        raise ValueError("请填写日常使用的 QQ 号码，多人用逗号分隔。")
    return list(dict.fromkeys(int(part) for part in fields))


def napcat_servers(root: Path) -> list[dict]:
    """只查当前项目里的 NapCat，不扫描或复制其他 QQ 的登录状态。"""
    patterns = (
        "NapCat*/NapCat*.Shell/versions/*/resources/app/napcat/config/onebot11_*.json",
        "NapCat*.Shell/versions/*/resources/app/napcat/config/onebot11_*.json",
    )
    candidates = []
    paths = set(path for pattern in patterns for path in root.glob(pattern))
    for path in sorted(paths):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            servers = raw.get("network", {}).get("websocketServers", [])
            for server in servers:
                host = str(server.get("host") or "127.0.0.1")
                port = int(server.get("port", 0))
                token = str(server.get("token") or "").strip()
                if not server.get("enable") or not token or not 1 <= port <= 65535:
                    continue
                if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::", "::1"):
                    continue
                candidates.append({"url": f"ws://127.0.0.1:{port}", "token": token})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    # 同一个配置可能存在于多个版本的安装目录中。
    return list({(item["url"], item["token"]): item for item in candidates}.values())


def valid_public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https" and parsed.hostname
            and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and parsed.path in ("", "/")
        )
    except ValueError:
        return False


def config_issues(config: dict) -> list[str]:
    issues = []
    qq = config.get("qq") or {}
    if not str(qq.get("access_token") or "").strip():
        issues.append("QQ：没有接口 Token，请运行 setup.bat。")
    try:
        parsed = urlsplit(str(qq.get("onebot_ws") or ""))
        valid_ws = parsed.scheme in ("ws", "wss") and parsed.hostname and parsed.port
        valid_ws = valid_ws and not parsed.username and not parsed.password
    except ValueError:
        valid_ws = False
    if not valid_ws:
        issues.append("QQ：连接地址无效，通常应为 ws://127.0.0.1:3001。")
    try:
        if not isinstance(qq.get("admins", []), list) or not isinstance(qq.get("users", []), list):
            raise TypeError()
        users = list(qq.get("admins") or []) + list(qq.get("users") or [])
        if (not users and qq.get("allow_all_users") is not True) or any(not re.fullmatch(r"[1-9][0-9]{4,11}", str(user)) for user in users):
            issues.append("授权：请填写允许发指令的日常 QQ 号码。")
    except TypeError:
        issues.append("授权：admins 和 users 应为 QQ 号码数组。")
    mycard = config.get("mycard") or {}
    if not mycard.get("account") or not mycard.get("password"):
        issues.append("观战：萌卡账号或密码未填写，可以先测试 QQ 回复。")
    public = str((config.get("share") or {}).get("public_base_url") or "").strip()
    if not public and (config.get("share") or {}).get("quick_tunnel") is not True:
        issues.append("分享：还没有公网地址，外网好友暂时打不开链接。")
    elif public and not valid_public_url(public):
        issues.append("分享：请填写隧道提供的 https 根地址。")
    return issues


def save_config(path: Path, config: dict) -> None:
    """原子替换，先备份旧配置；备份只留在本项目的 data 目录。"""
    encoded = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        backup_dir = path.parent / "data" / "config-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(path, backup_dir / f"config-{stamp}.json")
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def wizard(root: Path) -> None:
    path = root / "config.json"
    config = copy.deepcopy(read_config(path) if path.exists() else initial_config(root))
    qq = config.setdefault("qq", {})
    mycard = config.setdefault("mycard", {})
    share = config.setdefault("share", {})
    print("录像机器人配置向导。直接回车保留原值；密码和 Token 输入时不显示。")
    servers = napcat_servers(root)
    imported = False
    if len(servers) == 1:
        print("找到本项目里已启用的 NapCat 正向 WS 配置。")
        choice = input("导入接口地址和 Token？[Y/n]：").strip().lower()
        if choice not in ("n", "no"):
            qq["onebot_ws"] = servers[0]["url"]
            qq["access_token"] = servers[0]["token"]
            imported = True
            print("已读取接口配置，Token 不会显示。")
    elif len(servers) > 1:
        print("找到多个 NapCat 配置，请手动填写当前使用的接口。")
    if not imported:
        address = input("正向 WS 地址（回车默认 ws://127.0.0.1:3001）：").strip()
        qq["onebot_ws"] = address or qq.get("onebot_ws") or "ws://127.0.0.1:3001"
        token = getpass.getpass("Websocket Server 的 Token（不是网页登录密码）：").strip()
        if token:
            qq["access_token"] = token
    if qq.get("allow_all_users") is True:
        print("当前向所有 QQ 私聊用户开放，无需填写逐人授权名单。")
    while qq.get("allow_all_users") is not True:
        text = input("允许发指令的日常 QQ 号码（不是工具号，多人用逗号分隔）：").strip()
        if not text and (qq.get("admins") or qq.get("users")):
            break
        try:
            qq["users"] = parse_users(text)
            break
        except ValueError as exc:
            print(exc)
    account = input("萌卡账号（回车保留；尚未填写可先跳过）：").strip()
    if account:
        # 换账号时不能沿用另一账号的旧密码。
        if account != mycard.get("account"):
            mycard["password"] = ""
        mycard["account"] = account
    password = getpass.getpass("萌卡密码（回车保留）：")
    if password:
        mycard["password"] = password
    while True:
        public = input("公网分享 https 地址（还没有就回车，输入 - 清空）：").strip()
        if public == "-":
            share["public_base_url"] = ""
            break
        if not public:
            break
        if valid_public_url(public):
            share["public_base_url"] = public.rstrip("/")
            break
        print("请填写 https 根地址，例如隧道提供的地址，不带路径。")
    if not str(qq.get("access_token") or "").strip():
        raise ValueError("尚未填写接口 Token，原配置未修改。")
    save_config(path, config)
    print("配置已保存，旧配置已备份（如果存在）。")
    for issue in config_issues(config):
        print("提示：" + issue)
    print("接下来双击 doctor.bat 检查连接，再双击 start.bat 启动。")


async def probe_napcat(config: dict, transport=None, timeout: float = 8) -> tuple[bool, str]:
    from .account_guard import verify_transport_account
    qq = config.get("qq") or {}
    if not str(qq.get("access_token") or "").strip():
        return False, "QQ：未填写接口 Token。"
    transport = transport or NapCatTransport(str(qq.get("onebot_ws") or ""), str(qq["access_token"]))

    async def check():
        await transport.connect()
        await verify_transport_account(transport, qq.get('expected_self_id'))
        echo = "replay-bot-health"
        await transport.send_text(json.dumps({"action": "get_status", "params": {}, "echo": echo}))
        while True:
            raw = await transport.recv_text()
            if raw is None:
                raise ConnectionError()
            response = json.loads(raw)
            if not isinstance(response, dict) or response.get("echo") != echo:
                continue
            data = response.get("data") or {}
            if response.get("retcode") != 0:
                return False, "QQ：接口已连通，但状态查询失败，请检查 NapCat。"
            if data.get("online") is not True:
                return False, "QQ：接口已连通，但工具号还没登录或已掉线，请在 NapCat 登录。"
            return True, "QQ：接口 Token 验证成功，工具号在线（未发送任何 QQ 消息）。"

    try:
        return await asyncio.wait_for(check(), timeout=timeout)
    except Exception as exc:
        return False, "QQ：" + connection_hint(exc)
    finally:
        try:
            await asyncio.wait_for(transport.close(), timeout=2)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="录像机器人本机配置和检查")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--offline", action="store_true", help="只检查配置，不连接 NapCat")
    args = parser.parse_args()
    try:
        if not args.check:
            wizard(ROOT)
            return
        config = read_config(ROOT / "config.json")
        print("配置文件可以读取。以下检查不会显示账号、密码或 Token。")
        servers = napcat_servers(ROOT)
        token = str((config.get("qq") or {}).get("access_token") or "")
        if servers and not any(item["token"] == token for item in servers):
            print("QQ：机器人 Token 与本项目 NapCat 不一致，请运行 setup.bat 导入。")
        for issue in config_issues(config):
            print(issue)
        if not args.offline:
            ok, message = asyncio.run(probe_napcat(config))
            print(message)
            if not ok:
                raise SystemExit(1)
    except (ValueError, OSError):
        print("操作未完成：请检查配置格式和文件权限；缺少配置时先运行 setup.bat。")
        raise SystemExit(2) from None
    except (EOFError, KeyboardInterrupt):
        print("已取消，未完成的输入不会保存。")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
