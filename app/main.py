"""GUI 入口：解析参数、准备数据目录、配置 watch 模块、启动窗口。"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import ConfigStore  # noqa: E402
from app.paths import (APP_NAME, APP_TITLE, APP_VERSION, default_data_dir,  # noqa: E402
                       fallback_data_dir, prepare_data_dir, resource_path)
from app.ui import App  # noqa: E402


def _set_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _prepare_watch(data_dir: Path) -> None:
    import watch

    # 客户端只录制/上传，不读游戏目录；系统昵称和房间列表由服务器任务提供。
    watch.configure_paths(data_dir / "ygopro", data_dir)
    watch.REFERENCE_DECKS_PATH = resource_path("reference_decks.json")
    # 本地不下载百鸽全卡表：录像文件名中的卡组名不是客户端必需功能，
    # 避免第一次遇到未命中参考库的卡组时卡住 15–60 秒。
    watch._CARD_NAMES = {}
    os.environ.setdefault("YGOPRO_SKIP_ARENA_RANK", "1")
    watch.log = print

    # 萌卡 sign_in 会写 data/session.json（含短期 token）。桌面客户端只在进程内
    # 保留它：既能让 60 秒刷新复用登录，又不会把 token 留到下一次启动。
    session: dict = {}

    def load_session() -> dict:
        return dict(session)

    def save_session(payload: dict) -> None:
        session.clear()
        session.update(payload or {})

    watch.load_session = load_session
    watch.save_session = save_session
    try:
        (data_dir / "session.json").unlink(missing_ok=True)
    except OSError:
        pass


_MUTEX_HANDLE = None


def _acquire_single_instance(data_dir: Path) -> bool:
    """同一数据目录只允许一个客户端进程，避免 worker 名称/配置互相覆盖。"""
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    digest = hashlib.sha1(str(Path(data_dir).resolve()).lower().encode("utf-8")).hexdigest()[:16]
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, "Local\\MoeCardWatch-" + digest)
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            0,
            "萌卡观战助手已经在运行。\n请不要重复启动，避免同一客户端名称抢占任务。",
            "萌卡观战助手",
            0x40,
        )
        return False
    _MUTEX_HANDLE = handle
    return True


def _choose_data_dir(preferred: Path) -> tuple[Path, str]:
    """首选目录不可写时，自动退到 LocalAppData/临时目录，并返回提示。"""
    candidates: list[Path] = [Path(preferred)]
    fallback = fallback_data_dir()
    if fallback not in candidates:
        candidates.append(fallback)
    temp_fallback = Path(tempfile.gettempdir()) / APP_NAME
    if temp_fallback not in candidates:
        candidates.append(temp_fallback)
    errors: list[str] = []
    for candidate in candidates:
        try:
            ready = prepare_data_dir(candidate)
            warning = ""
            if candidate != Path(preferred):
                warning = f"首选数据目录不可写，已改用：{ready}"
            return ready, warning
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError("没有可写的数据目录：\n" + "\n".join(errors))


def _center(root: tk.Tk) -> None:
    root.update_idletasks()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    width = min(1180, max(980, screen_w - 120))
    height = min(790, max(660, screen_h - 110))
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 2 - 12)
    root.geometry(f"{width}x{height}+{x}+{y}")


def _set_icon(root: tk.Tk) -> None:
    try:
        icon = resource_path("assets", "app.ico")
        if icon.is_file():
            root.iconbitmap(default=str(icon))
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} {APP_VERSION}")
    parser.add_argument("--data-dir", default="", help="自定义数据目录")
    parser.add_argument("--portable", action="store_true",
                        help="兼容旧参数；数据现在默认就写在 exe 同级的 data 目录")
    parser.add_argument("--smoke-test", action="store_true", help="创建窗口后自动退出（自检）")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{APP_TITLE} {APP_VERSION}")
        return 0

    if args.data_dir:
        requested_dir = Path(args.data_dir).expanduser()
    else:
        requested_dir = default_data_dir()
    data_dir, dir_warning = _choose_data_dir(requested_dir)
    if not _acquire_single_instance(data_dir):
        return 0
    _prepare_watch(data_dir)

    _set_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    root.title(f"{APP_TITLE} {APP_VERSION}")
    _set_icon(root)
    _center(root)
    root.minsize(980, 640)
    root.deiconify()

    store = ConfigStore(data_dir / "config.json")
    loaded = store.load()
    if loaded.migrated:
        store.save(loaded.config)
    warnings = list(loaded.warnings)
    if dir_warning:
        warnings.insert(0, dir_warning)
    app = App(root, store, loaded.config, data_dir, warnings, smoke=args.smoke_test)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    if args.smoke_test:
        root.after(1200, app.smoke_close)
        root.mainloop()
        print("SMOKE_OK")
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
