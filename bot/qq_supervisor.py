# -*- coding: utf-8 -*-
"""P14 候选：QQ/NapCat 生命周期与 Windows Job Object。

尚未接入 launcher/start-all，不在运行路径；真实关窗测试需用户在场授权。
本文件不对真实 NapCat/QQ 发起任何操作。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    created_at: float
    image: str = ""
    cmdline: str = ""


def identity_matches(recorded: Optional[dict], actual: Optional[ProcessInfo]) -> bool:
    """PID 复用防护：PID + 创建时间（+ 已知时镜像）同时匹配才算同一个进程。"""
    if not recorded or actual is None:
        return False
    try:
        if int(recorded.get("pid")) != int(actual.pid):
            return False
        if abs(float(recorded.get("created_at")) - float(actual.created_at)) > 1.0:
            return False
    except (TypeError, ValueError, KeyError):
        return False
    recorded_image = str(recorded.get("image") or "")
    return not recorded_image or recorded_image.lower() == (actual.image or "").lower()


class JobApi:
    """注入式 Job 接口；Windows 实现见 WindowsJobApi，测试用 FakeJob。"""

    def create(self):
        raise NotImplementedError

    def assign(self, job, pid: int) -> None:
        raise NotImplementedError

    def contains(self, job, pid: int) -> Optional[bool]:
        raise NotImplementedError

    def in_any_job(self, pid: int) -> Optional[bool]:
        raise NotImplementedError

    def close(self, job) -> None:
        raise NotImplementedError

    def terminate(self, job) -> None:
        raise NotImplementedError

    def terminate_pid(self, pid: int) -> None:
        raise NotImplementedError


class WindowsJobApi(JobApi):
    """Windows Job Object 最小封装；只在 Windows 上实例化。"""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._info_type = JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        self._kernel32 = ctypes.windll.kernel32
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self._kernel32.IsProcessInJob.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._jobs = []

    def create(self):
        job = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObjectW failed")
        info = self._info_type()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
                job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                self._ctypes.byref(info), self._ctypes.sizeof(info)):
            self._kernel32.CloseHandle(job)
            raise OSError("SetInformationJobObject failed")
        self._jobs.append(job)
        return job

    def assign(self, job, pid: int) -> None:
        rights = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = self._kernel32.OpenProcess(rights, False, int(pid))
        if not handle:
            raise OSError("OpenProcess failed")
        try:
            if not self._kernel32.AssignProcessToJobObject(job, handle):
                raise OSError("AssignProcessToJobObject failed")
        finally:
            self._kernel32.CloseHandle(handle)

    def contains(self, job, pid: int) -> Optional[bool]:
        handle = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            result = self._wintypes.BOOL()
            ok = self._kernel32.IsProcessInJob(handle, job, self._ctypes.byref(result))
            return bool(result.value) if ok else None
        finally:
            self._kernel32.CloseHandle(handle)

    def in_any_job(self, pid: int) -> Optional[bool]:
        handle = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            result = self._wintypes.BOOL()
            ok = self._kernel32.IsProcessInJob(handle, None, self._ctypes.byref(result))
            return bool(result.value) if ok else None
        finally:
            self._kernel32.CloseHandle(handle)

    def close(self, job) -> None:
        self._kernel32.CloseHandle(job)

    def terminate(self, job) -> None:
        if not self._kernel32.TerminateJobObject(job, 1):
            raise OSError("TerminateJobObject failed")

    def terminate_pid(self, pid: int) -> None:
        handle = self._kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not handle:
            raise OSError("OpenProcess(PROCESS_TERMINATE) failed")
        try:
            if not self._kernel32.TerminateProcess(handle, 1):
                raise OSError("TerminateProcess failed")
        finally:
            self._kernel32.CloseHandle(handle)


@dataclass
class Ownership:
    owned: bool
    reason: str


@dataclass
class LaunchStep:
    action: str
    detail: str


def build_launch_plan(root, napcat_batch: str, qq_module: str = "bot.qq_service",
                      external_napcat: bool = False) -> list[LaunchStep]:
    """只生成候选步骤，不执行任何进程/登录动作。

    Job 必须在 NapCat 创建前存在；外部 NapCat 跳过 spawn/attach，只提示。
    """
    if external_napcat:
        return [
            LaunchStep("external_napcat", "NapCat 由外部启动，跳过 spawn/attach，不按镜像名结束"),
            LaunchStep("manual_close_hint", "关闭窗口时请手动确认外部 NapCat；本项目只停 QQ 服务"),
        ]
    return [
        LaunchStep("create_job", "在 NapCat 创建前创建 KILL_ON_JOB_CLOSE Job"),
        LaunchStep("spawn_napcat", str(Path(root) / napcat_batch)),
        LaunchStep("attach_napcat_immediately", "NapCat 创建后立即 AssignProcessToJobObject"),
        LaunchStep("wait_login", "等待用户在 NapCat 窗口完成扫码/登录，不自动登录"),
        LaunchStep("spawn_qq_service", qq_module),
        LaunchStep("attach_qq_service_immediately", "QQ 服务创建后立即 AssignProcessToJobObject"),
        LaunchStep("verify_subtree", "枚举 NapCat 后代，确认全部在 Job 内"),
    ]


class Supervisor:
    """生命周期决策；系统动作由注入的 lookup/job_api 完成。"""

    def __init__(self, lookup, job_api: JobApi, log=None, state_path: Optional[Path] = None):
        self.lookup = lookup
        self.job_api = job_api
        self.log = log or (lambda message: None)
        self.state_path = Path(state_path) if state_path else None
        self.job = None
        self.mode = "none"

    def detect_ownership(self, recorded: Optional[dict], running: Optional[ProcessInfo]) -> Ownership:
        if running is None:
            return Ownership(False, "napcat_not_running")
        if identity_matches(recorded, running):
            return Ownership(True, "recorded_identity")
        return Ownership(False, "external_or_identity_changed")

    def prepare_job(self, supervisor_pid: int) -> str:
        nested = self.job_api.in_any_job(int(supervisor_pid))
        try:
            self.job = self.job_api.create()
            self.mode = "nested_job" if nested else "job"
            self.log(f"已创建 Job（{'嵌套' if nested else '普通'}模式）")
        except OSError as exc:
            self.mode = "pid_tree"
            self.log(f"Job 分配失败，回退受管 PID 树：{exc}")
        return self.mode

    def attach(self, pid: int) -> bool:
        if self.job is None:
            self.mode = "pid_tree"
            return False
        try:
            self.job_api.assign(self.job, int(pid))
            inside = self.job_api.contains(self.job, int(pid))
            if inside is not True:
                raise OSError(f"进程 {pid} 未进入 Job")
            return True
        except OSError as exc:
            self.mode = "pid_tree"
            try:
                self.job_api.close(self.job)
            except Exception:
                pass
            self.job = None
            self.log(f"Assign 失败，回退受管 PID 树：{exc}")
            return False

    def verify_subtree(self, root_pid: int, descendants: Iterable[int]) -> list[int]:
        leaks = []
        for pid in descendants:
            if self.job is None or self.job_api.contains(self.job, int(pid)) is not True:
                leaks.append(int(pid))
        return leaks

    def stop(self, ownership: Ownership, fallback_children: Iterable[int] = ()) -> list[str]:
        """先关/终止 Job，再写日志；外部 NapCat 不杀。"""
        actions = []
        if ownership.owned and self.job is not None:
            terminated = False
            try:
                self.job_api.terminate(self.job)
                actions.append("terminated_job")
                terminated = True
            except Exception as exc:
                actions.append(f"terminate_failed:{exc}")
            try:
                self.job_api.close(self.job)
                actions.append("closed_job")
            except Exception as exc:
                actions.append(f"close_failed:{exc}")
            self.job = None
            if not terminated:
                for pid in fallback_children:
                    try:
                        self.job_api.terminate_pid(int(pid))
                        actions.append(f"pid_tree_terminated:{int(pid)}")
                    except Exception as exc:
                        actions.append(f"pid_tree_failed:{int(pid)}:{exc}")
        elif ownership.owned:
            for pid in fallback_children:
                try:
                    self.job_api.terminate_pid(int(pid))
                    actions.append(f"pid_tree_terminated:{int(pid)}")
                except Exception as exc:
                    actions.append(f"pid_tree_failed:{int(pid)}:{exc}")
        else:
            actions.append("external_napcat_kept")
        self.log("停止顺序：" + ",".join(actions))
        return actions
