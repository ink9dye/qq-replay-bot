# -*- coding: utf-8 -*-
"""P14 launcher 候选：QQ 生命周期计划与状态文件。

只生成计划；不启动真实 NapCat/QQ、不关窗口、不杀进程。真实执行留给后续接入。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .qq_supervisor import LaunchStep, build_launch_plan


@dataclass
class LifecycleState:
    supervisor_pid: Optional[int] = None
    supervisor_started_at: Optional[float] = None
    napcat_pid: Optional[int] = None
    napcat_created_at: Optional[float] = None
    napcat_image: str = ""
    qq_pid: Optional[int] = None
    job_name: str = ""
    owned: bool = False

    @classmethod
    def load(cls, path: Path) -> "LifecycleState":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: data.get(key) for key in allowed if key in data})

    def save(self, path: Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(temporary, target)

    def has_qq_process(self) -> bool:
        return bool(self.qq_pid or self.supervisor_pid)


@dataclass
class StartPlan:
    action: str
    steps: list[LaunchStep] = field(default_factory=list)
    reason: str = ""


def plan_start(root, state: LifecycleState, *, qq_health: str = "missing",
               existing_napcat: bool = False, enabled: bool = True,
               job_name: str = "qq-replay-bot") -> StartPlan:
    """start-all 候选：健康复用、只补缺、外部 NapCat 跳过 spawn/attach、旧路径回退。"""
    if not enabled:
        return StartPlan("legacy_fallback", [LaunchStep("legacy_start_qq", "旧 start-all 路径")],
                         "supervisor 未启用")
    if qq_health == "online" and state.has_qq_process():
        return StartPlan("reuse", [], "QQ 运行窗口健康，已复用")
    if qq_health == "process_stale" and state.has_qq_process():
        return StartPlan("attention", [], "进程存在但未在线；不重复启动，请查看运行窗口")
    if existing_napcat and not (state.owned and state.napcat_pid):
        return StartPlan("external", build_launch_plan(root, "start-napcat.bat", external_napcat=True),
                         "NapCat 已由外部启动，跳过 spawn/attach")
    steps = build_launch_plan(root, "start-napcat.bat")
    return StartPlan("start", steps, f"只补缺，Job={job_name}")


def execute_plan_dummy(plan: StartPlan, handlers: dict[str, Callable[[LaunchStep], None]]) -> list[str]:
    """测试/候选执行器：只调用注入的 handler，不接触真实进程。"""
    called = []
    for step in plan.steps:
        handler = handlers.get(step.action)
        if handler is None:
            raise KeyError(f"missing handler: {step.action}")
        handler(step)
        called.append(step.action)
    return called
