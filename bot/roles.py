# -*- coding: utf-8 -*-
"""服务角色与启动锁命名。

全开模式（roles=None）仍然是一个进程一把 bot-runtime.lock；
只有显式拆分角色时才按角色分锁，观战/采集与 QQ 互不阻塞。
锁文件都在 data/ 目录下：
    bot-runtime.lock  全开单进程（保持今天的行为）
    collector.lock    拆分后的观战/采集（含分享服务）
    bot.lock          拆分后的 QQ 收发
"""

from __future__ import annotations

ALL_ROLES = frozenset({"watch", "qq", "share"})
FULL_LOCK = "bot-runtime.lock"
ROLE_LOCKS = {"watch": "collector.lock", "share": "collector.lock", "qq": "bot.lock"}


class RoleError(ValueError):
    """角色参数不合法。"""


def parse_roles(text: str) -> frozenset[str]:
    """解析逗号分隔的角色；all 展开为全部合法角色，重复值自动去重。"""
    roles: set[str] = set()
    for part in str(text).split(","):
        name = part.strip()
        if name == "all":
            roles.update(ALL_ROLES)
            continue
        if name not in ALL_ROLES:
            shown = name or "空值"
            raise RoleError(f"未知角色：{shown}；--roles 只支持 watch、qq、share、all（逗号分隔）")
        roles.add(name)
    if not roles:
        raise RoleError("--roles 不能为空；只支持 watch、qq、share、all（逗号分隔）")
    return frozenset(roles)


def parse_roles_argument(argv: list[str]) -> frozenset[str] | None:
    """从启动参数里取 --roles；没传时返回 None，保持原来的全开行为。"""
    value: str | None = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--roles":
            if index + 1 >= len(argv):
                raise RoleError("--roles 需要一个用逗号分隔的角色列表")
            value = argv[index + 1]
            index += 2
            continue
        if item.startswith("--roles="):
            value = item.split("=", 1)[1]
        index += 1
    return None if value is None else parse_roles(value)


def service_mode(config) -> str:
    """config.json 的 service_mode：unified（缺省）或 split。"""
    if not isinstance(config, dict):
        return "unified"
    return str(config.get("service_mode") or "").strip().lower() or "unified"


def roles_from_config(config, roles=None) -> frozenset[str]:
    """把入口参数和 config.json 合起来决定本进程跑哪些角色。

    - 显式传了 --roles（含 all）：以显式为准，配置文件不覆盖。
    - 没传角色：看 service_mode。unified/缺省 = 全开（今天的行为）；
      split = 本进程只做 watch+share，QQ 由另一个进程负责。
    PyCharm 固定 watch Run 仍然传 --unified 且不带 --roles，所以在 split 模式里
    它就是采集进程；退回 unified 只改 config.json 一处，不用改 IDE 配置。
    """
    if roles is not None:
        return normalize_roles(roles)
    if service_mode(config) == "split":
        return frozenset({"watch", "share"})
    return ALL_ROLES


def normalize_roles(roles) -> frozenset[str]:
    """把 None、字符串或可迭代对象统一成角色集合；None 表示全开。"""
    if roles is None:
        return ALL_ROLES
    if isinstance(roles, str):
        return parse_roles(roles)
    normalized = frozenset(str(role).strip() for role in roles)
    if "all" in normalized:
        return ALL_ROLES
    unknown = normalized - ALL_ROLES
    if unknown:
        raise RoleError("未知角色：" + "、".join(sorted(unknown)))
    if not normalized:
        raise RoleError("角色不能为空")
    return normalized


def lock_filenames(roles=None) -> tuple[str, ...]:
    """返回该角色组合需要占用的锁文件，顺序固定，避免多锁启动时互相等待。

    全开只认 bot-runtime.lock；显式 all 也走全开。
    """
    active = normalize_roles(roles)
    if active == ALL_ROLES:
        return (FULL_LOCK,)
    names: list[str] = []
    for role in ("watch", "qq", "share"):
        if role in active:
            name = ROLE_LOCKS[role]
            if name not in names:
                names.append(name)
    return tuple(names)
