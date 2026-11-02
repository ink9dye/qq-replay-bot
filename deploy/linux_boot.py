#!/usr/bin/env python3
"""POSIX launcher shim.

The upstream bot uses msvcrt file locking (Windows only). Importing a fake
``msvcrt`` module confuses CPython's subprocess module, so instead we replace
``bot.launcher.StartupLock`` with an fcntl-based implementation before the
service modules import it.
"""
import fcntl
import sys
from pathlib import Path

sys.path.insert(0, '/opt/ygobot')

import bot.launcher as L  # noqa: E402


class PosixStartupLock:
    def __init__(self, root, filename='startup.lock'):
        self.path = Path(root) / 'data' / filename
        self._handle = None
        self.acquired = False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        self.acquired = True
        return True

    def release(self):
        handle, self._handle, self.acquired = self._handle, None, False
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


L.StartupLock = PosixStartupLock

mode = sys.argv[1] if len(sys.argv) > 1 else 'collector'
if mode == 'collector':
    from bot.collector import main
elif mode == 'qq':
    from bot.qq_service import main
else:
    raise SystemExit('usage: linux_boot.py collector|qq')

main()
