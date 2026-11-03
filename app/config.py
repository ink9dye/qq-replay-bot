"""本地配置读写。令牌/密码用 DPAPI 加密，日志和界面都不回显完整值。

从 1.1 起支持多个萌卡账号：``accounts`` 是一组账号，一个账号同一时刻只观战一间，
所以并发路数 = 账号数量。旧版单账号配置读取时会自动迁移成一条账号。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .secrets import protect, unprotect

DEFAULT_SERVER = "http://101.201.108.188:8787"
MAX_CAPACITY = 6
MAX_ACCOUNTS = MAX_CAPACITY


@dataclass
class AccountConfig:
    """一组萌卡凭据。password 只在内存里是明文，落盘走 DPAPI。"""

    account: str = ""
    password: str = ""

    def masked(self) -> str:
        return f"{self.account[:3]}***" if len(self.account) > 3 else "***"


@dataclass
class AppConfig:
    server: str = DEFAULT_SERVER
    token: str = ""
    name: str = ""
    accounts: list[AccountConfig] = field(default_factory=list)
    remember_password: bool = True
    keep_local: bool = True
    auto_start: bool = False

    @property
    def capacity(self) -> int:
        """并发路数由账号数决定：每个账号只负责一间房。"""
        return max(1, min(MAX_CAPACITY, len(self.accounts) or 1))

    def real_accounts(self) -> list[AccountConfig]:
        """过滤掉界面上留空的行。"""
        return [item for item in self.accounts
                if item.account.strip() or item.password]

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("token", None)
        for item in data.get("accounts") or []:
            item.pop("password", None)
        data["capacity"] = self.capacity
        return data


def _safe_hostname() -> str:
    raw = socket.gethostname().strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")
    return cleaned[:24] or "pc"


def default_worker_name() -> str:
    return f"pc-{_safe_hostname()}-{secrets.token_hex(2)}"


def normalize_server(value: str) -> str:
    """接受 http(s) 或裸主机名；去掉末尾斜杠和误粘贴的 /watch-api。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError("服务器地址必须是 http:// 或 https:// 开头的完整地址")
    path = parts.path.rstrip("/")
    if path.lower().endswith("/watch-api"):
        path = path[: -len("/watch-api")]
    if path.lower().endswith("/watch-api/status"):
        path = path[: -len("/watch-api/status")]
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, "", "")).rstrip("/")


def validate(cfg: AppConfig, need_account: bool = True) -> list[str]:
    errors: list[str] = []
    try:
        server = normalize_server(cfg.server)
        if not server:
            errors.append("请填写服务器地址")
    except ValueError as exc:
        errors.append(str(exc))
    if not cfg.token.strip():
        errors.append("请填写接口令牌（向服务器管理员获取）")
    name = cfg.name.strip()
    if not name:
        errors.append("请填写客户端名称")
    elif len(name) > 64:
        errors.append("客户端名称不能超过 64 个字符")

    rows = cfg.real_accounts()
    if need_account:
        if not rows:
            errors.append("请至少添加一个萌卡账号")
        elif len(rows) > MAX_ACCOUNTS:
            errors.append(f"最多支持 {MAX_ACCOUNTS} 个账号（一账号一间房）")
        seen: set[str] = set()
        for index, item in enumerate(rows, start=1):
            account = item.account.strip()
            if not account:
                errors.append(f"第 {index} 个账号没有填写账号名")
                continue
            if not item.password and cfg.remember_password:
                errors.append(f"账号 {account} 没有填写密码")
            key = account.lower()
            if key in seen:
                errors.append(f"账号 {account} 重复，同一个账号不能同时看两间房")
            seen.add(key)
    return errors


@dataclass
class LoadResult:
    config: AppConfig
    warnings: list[str] = field(default_factory=list)
    migrated: bool = False


class ConfigStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> LoadResult:
        warnings: list[str] = []
        cfg = AppConfig(name=default_worker_name())
        migrated = False
        if not self.path.is_file():
            return LoadResult(cfg, warnings)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return LoadResult(cfg, [f"配置文件无法读取，已使用默认值：{exc}"])
        if not isinstance(raw, dict):
            return LoadResult(cfg, ["配置文件格式不正确，已使用默认值"])

        def text(key: str, default: str = "") -> str:
            value = raw.get(key, default)
            return str(value).strip() if isinstance(value, (str, int, float)) else default

        cfg.server = text("server", DEFAULT_SERVER) or DEFAULT_SERVER
        cfg.name = text("name") or cfg.name
        cfg.remember_password = bool(raw.get("remember_password", True))
        cfg.keep_local = bool(raw.get("keep_local", True))
        cfg.auto_start = bool(raw.get("auto_start", False))

        # 兼容旧版本/手工编辑：同时接受明文键，读取后下次保存自动迁移为密文。
        plain_token = text("token")
        token_enc = text("token_enc")
        if token_enc:
            value = unprotect(token_enc)
            if value is None:
                warnings.append("接口令牌无法用当前 Windows 账户解密，请重新填写")
            else:
                cfg.token = value
        elif plain_token:
            cfg.token = plain_token
            migrated = True

        accounts: list[AccountConfig] = []
        raw_accounts = raw.get("accounts")
        if isinstance(raw_accounts, list):
            for entry in raw_accounts:
                if not isinstance(entry, dict):
                    continue
                account = str(entry.get("account") or "").strip()
                password = ""
                password_enc = str(entry.get("password_enc") or "").strip()
                plain = str(entry.get("password") or "")
                if password_enc:
                    value = unprotect(password_enc)
                    if value is None:
                        if account:
                            warnings.append(f"账号 {account} 的密码无法用当前 Windows 账户解密，请重新填写")
                    else:
                        password = value
                elif plain:
                    password = plain
                    migrated = True
                if account or password:
                    accounts.append(AccountConfig(account=account, password=password))

        if not accounts:
            # 旧版单账号字段 -> 一条账号
            account = text("account")
            password_enc = text("password_enc")
            plain_password = text("password")
            password = ""
            if password_enc:
                value = unprotect(password_enc)
                if value is None:
                    if account:
                        warnings.append("萌卡密码无法用当前 Windows 账户解密，请重新填写")
                else:
                    password = value
            elif plain_password:
                password = plain_password
                migrated = True
            if account or password:
                accounts.append(AccountConfig(account=account, password=password))
                migrated = True

        if accounts and not cfg.remember_password:
            for item in accounts:
                item.password = ""
        cfg.accounts = accounts
        return LoadResult(cfg, warnings, migrated)

    def save(self, cfg: AppConfig) -> list[str]:
        warnings: list[str] = []
        token_enc = protect(cfg.token.strip()) if cfg.token.strip() else ""
        if cfg.token.strip() and token_enc is None:
            warnings.append("当前系统无法加密接口令牌，本次不会保存令牌")
            token_enc = ""

        accounts: list[dict] = []
        for item in cfg.real_accounts():
            entry = {"account": item.account.strip(), "password_enc": ""}
            if cfg.remember_password and item.password:
                password_enc = protect(item.password)
                if password_enc is None:
                    warnings.append(f"当前系统无法加密账号 {item.account.strip()} 的密码，本次不会保存该密码")
                else:
                    entry["password_enc"] = password_enc
            accounts.append(entry)
        if len(accounts) > MAX_ACCOUNTS:
            warnings.append(f"只保存前 {MAX_ACCOUNTS} 个账号")

        payload = {
            "version": 2,
            "server": normalize_server(cfg.server) if cfg.server else DEFAULT_SERVER,
            "token_enc": token_enc,
            "name": cfg.name.strip(),
            "accounts": accounts[:MAX_ACCOUNTS],
            "remember_password": bool(cfg.remember_password),
            "keep_local": bool(cfg.keep_local),
            "auto_start": bool(cfg.auto_start),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            warnings.append(f"配置保存失败：{exc}")
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return warnings
