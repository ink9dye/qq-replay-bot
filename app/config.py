"""本地配置读写。令牌/密码用 DPAPI 加密，日志和界面都不回显完整值。"""
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


@dataclass
class AppConfig:
    server: str = DEFAULT_SERVER
    token: str = ""
    name: str = ""
    capacity: int = 1
    account: str = ""
    password: str = ""
    remember_password: bool = True
    keep_local: bool = False
    auto_start: bool = False

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("token", None)
        data.pop("password", None)
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
    try:
        capacity = int(cfg.capacity)
    except (TypeError, ValueError):
        capacity = 0
    if not 1 <= capacity <= MAX_CAPACITY:
        errors.append(f"并发路数需在 1–{MAX_CAPACITY} 之间")
    if need_account:
        if not cfg.account.strip():
            errors.append("请填写萌卡账号")
        if not cfg.password:
            errors.append("请填写萌卡密码")
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
        try:
            cfg.capacity = max(1, min(MAX_CAPACITY, int(raw.get("capacity") or 1)))
        except (TypeError, ValueError):
            cfg.capacity = 1
        cfg.account = text("account")
        cfg.remember_password = bool(raw.get("remember_password", True))
        cfg.keep_local = bool(raw.get("keep_local", False))
        cfg.auto_start = bool(raw.get("auto_start", False))

        # 兼容旧版本/手工编辑：同时接受明文键，读取后下次保存自动迁移为密文。
        plain_token = text("token")
        plain_password = text("password")
        token_enc = text("token_enc")
        password_enc = text("password_enc")
        if token_enc:
            value = unprotect(token_enc)
            if value is None:
                warnings.append("接口令牌无法用当前 Windows 账户解密，请重新填写")
            else:
                cfg.token = value
        elif plain_token:
            cfg.token = plain_token
            migrated = True
        if password_enc:
            value = unprotect(password_enc)
            if value is None:
                warnings.append("萌卡密码无法用当前 Windows 账户解密，请重新填写")
            else:
                cfg.password = value
        elif plain_password:
            cfg.password = plain_password
            migrated = True
        if cfg.password and not cfg.remember_password:
            cfg.password = ""
        return LoadResult(cfg, warnings, migrated)

    def save(self, cfg: AppConfig) -> list[str]:
        warnings: list[str] = []
        token_enc = protect(cfg.token.strip()) if cfg.token.strip() else ""
        if cfg.token.strip() and token_enc is None:
            warnings.append("当前系统无法加密接口令牌，本次不会保存令牌")
            token_enc = ""
        if cfg.remember_password and cfg.password:
            password_enc = protect(cfg.password)
            if password_enc is None:
                warnings.append("当前系统无法加密萌卡密码，本次不会保存密码")
                password_enc = ""
        else:
            password_enc = ""

        payload = {
            "version": 1,
            "server": normalize_server(cfg.server) if cfg.server else DEFAULT_SERVER,
            "token_enc": token_enc,
            "name": cfg.name.strip(),
            "capacity": int(cfg.capacity),
            "account": cfg.account.strip(),
            "password_enc": password_enc,
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
