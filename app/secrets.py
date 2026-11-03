"""Windows DPAPI 加密，用于本机保存接口令牌和萌卡密码。

密文只能被同一 Windows 用户（默认同机）解开；配置文件被单独拷走也无法还原。
非 Windows 或 DPAPI 调用失败时返回 None，由调用方决定是否退化为不保存。
"""
from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes

PREFIX = "dpapi-v1:"
_DESCRIPTION = "MoeCardWatch local secret"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _make_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _take_blob(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        try:
            ctypes.windll.kernel32.LocalFree(blob.pbData)
        except Exception:
            pass


def _available() -> bool:
    return sys.platform == "win32"


def protect(text: str) -> str | None:
    """加密并返回 ``dpapi-v1:<base64>``；失败返回 None。空串返回空串。"""
    if not text:
        return ""
    if not _available():
        return None
    try:
        in_blob, in_buffer = _make_blob(text.encode("utf-8"))
        out_blob = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            _DESCRIPTION,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        raw = _take_blob(out_blob)
        return PREFIX + base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


def unprotect(value: str) -> str | None:
    """解密 ``dpapi-v1:`` 值；格式不对或当前用户无法解密时返回 None。"""
    if not value:
        return ""
    if not value.startswith(PREFIX):
        return None
    if not _available():
        return None
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
        in_blob, in_buffer = _make_blob(raw)
        out_blob = _DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        return _take_blob(out_blob).decode("utf-8", "replace")
    except Exception:
        return None


def wipe(value: bytearray) -> None:
    """尽力覆盖内存中的敏感字符（Python 字符串本身不可变，只能减少副本）。"""
    for index in range(len(value)):
        value[index] = 0
