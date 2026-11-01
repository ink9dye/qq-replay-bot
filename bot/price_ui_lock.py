# -*- coding: utf-8 -*-
"""跨进程互斥：JHS UI 同时只能被查价或预定检查占用。"""
from __future__ import annotations

import ctypes
import os


class PriceUiLock:
    def __init__(self, timeout=120, name=r"Global\qq-replay-bot-price-ui"):
        self.timeout = int(timeout * 1000)
        self.name = name
        self.handle = None

    def acquire(self):
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            return False
        result = kernel32.WaitForSingleObject(ctypes.c_void_p(handle), self.timeout)
        if result not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            kernel32 = ctypes.windll.kernel32
            kernel32.ReleaseMutex(ctypes.c_void_p(self.handle))
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("集换社 UI 正被其他查价/预定检查占用")
        return self

    def __exit__(self, *exc):
        self.release()
