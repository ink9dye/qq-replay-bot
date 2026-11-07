#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等到连续 QUIET_SECONDS 没有 QQ 命令/回复后，重启一次 ygo-qq 让新代码生效。

只读诊断文件；重启动作由用户明确要求。首次安装后由人工 nohup 启动，
完成后写 marker 并退出，不常驻。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

LOG = Path("/opt/ygobot/data/logs/qq-diagnostics.jsonl")
MARKER = Path("/opt/ygobot/data/logs/qq-restart-waiter.json")
QUIET_SECONDS = 300
POLL_SECONDS = 15
MAX_HOURS = 12
EVENTS = ("command_start", "reply_queued", "reply_complete")


def last_activity() -> float:
    if not LOG.exists():
        return 0.0
    last = 0.0
    with LOG.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 500_000))
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") in EVENTS:
                last = max(last, float(row.get("time") or 0))
    return last


def write_marker(**payload) -> None:
    MARKER.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    started = time.time()
    write_marker(status="waiting", started_at=started)
    while True:
        now = time.time()
        last = last_activity()
        quiet = now - last if last else QUIET_SECONDS
        if quiet >= QUIET_SECONDS:
            result = subprocess.run(["systemctl", "restart", "ygo-qq"], capture_output=True, text=True)
            write_marker(status="restarted", restarted_at=time.time(), quiet_seconds=round(quiet),
                         returncode=result.returncode, stderr=result.stderr[-500:])
            return 0
        if now - started > MAX_HOURS * 3600:
            write_marker(status="timeout", quiet_seconds=round(quiet))
            return 1
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
