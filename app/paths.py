"""运行目录、资源路径和日志轮转。"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

APP_NAME = "MoeCardWatch"
APP_TITLE = "萌卡观战助手"
APP_VERSION = "1.0.0"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    """exe 所在目录；开发时是项目根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """PyInstaller 打包后的资源路径（onefile 解压到 _MEIPASS）。"""
    base = Path(getattr(sys, "_MEIPASS", executable_dir()))
    return base.joinpath(*parts)


def default_data_dir() -> Path:
    """默认写入当前用户的 LocalAppData，避免 Program Files 只读。

    如果 exe 同目录存在 ``portable.flag``，则使用 exe 同目录的 data，
    方便放进 U 盘或整个文件夹拷走。
    """
    exe_dir = executable_dir()
    if (exe_dir / "portable.flag").is_file():
        return exe_dir / "data"
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if local:
        return Path(local) / APP_NAME
    return Path.home() / ("." + APP_NAME.lower())


def prepare_data_dir(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    (path / "logs").mkdir(exist_ok=True)
    (path / "work").mkdir(exist_ok=True)
    (path / "decks").mkdir(exist_ok=True)
    (path / "local-replays").mkdir(exist_ok=True)
    return path


class RotatingLog:
    """很小的线程安全文本日志：单文件 2 MB，保留 3 个历史文件。"""

    def __init__(self, directory: Path, name: str = "client.log", max_bytes: int = 2 * 1024 * 1024,
                 backups: int = 3):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / name
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n"
        with self._lock:
            try:
                if self.path.is_file() and self.path.stat().st_size + len(line.encode("utf-8")) > self.max_bytes:
                    self._rotate()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass

    def _rotate(self) -> None:
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else self.path.with_suffix(f".log.{index - 1}")
            target = self.path.with_suffix(f".log.{index}")
            try:
                if source.is_file():
                    if target.exists():
                        target.unlink()
                    source.replace(target)
            except OSError:
                pass
