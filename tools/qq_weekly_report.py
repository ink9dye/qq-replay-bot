#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周只读巡检：统计近 N 天 QQ 账号离线 / 接口断连 / 发送量，追加到日志。

只读 qq-diagnostics.jsonl（含 .gz 轮转），不连网、不发消息、不写数据目录以外的东西。
由 ygobot-qq-weekly.timer 每周一 09:00 调用。
"""
from __future__ import annotations

import argparse
import gzip
import json
import time
from datetime import datetime
from pathlib import Path

ACCOUNT = "account_offline"
INTERFACE = "interface"


def iter_rows(paths):
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("time"), (int, float)):
                        yield row
        except OSError:
            continue


def build_episodes(rows):
    """按状态迁移切分：qq_offline=账号离线，reconnecting=接口断连，online=结束。"""
    timeline = []
    for row in rows:
        event = row.get("event")
        if event == "connection_state":
            timeline.append((float(row["time"]), "state", row.get("state")))
        elif event == "connect_attempt":
            timeline.append((float(row["time"]), "attempt", None))
    timeline.sort(key=lambda item: item[0])

    episodes, current = [], None
    for when, kind, value in timeline:
        if kind == "attempt":
            if current is not None:
                current["attempts"] += 1
            continue
        if value == "qq_offline":
            if current is not None:
                episodes.append(current)
            current = {"kind": ACCOUNT, "start": when, "end": when, "attempts": 0}
        elif value == "reconnecting":
            if current is None:
                current = {"kind": INTERFACE, "start": when, "end": when, "attempts": 0}
        elif value == "online":
            if current is not None:
                current["end"] = when
                episodes.append(current)
                current = None
        elif value in ("manual_attention", "stopped"):
            if current is not None:
                current["end"] = when
                episodes.append(current)
                current = None
    if current is not None:
        current["end"] = timeline[-1][0]
        episodes.append(current)
    return episodes


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ 诊断周报（只读）")
    parser.add_argument("--log-dir", default="/opt/ygobot/data/logs")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    paths = sorted(log_dir.glob("qq-diagnostics.jsonl*"))
    since = time.time() - max(1, args.days) * 86400

    rows = list(iter_rows(paths))
    episodes = [e for e in build_episodes(rows) if e["start"] >= since]
    account = [e for e in episodes if e["kind"] == ACCOUNT]
    interface = [e for e in episodes if e["kind"] == INTERFACE]

    sends = commands = api_errors = 0
    for row in rows:
        if float(row["time"]) < since:
            continue
        event = row.get("event")
        if event == "reply_queued":
            sends += 1
        elif event == "command_start":
            commands += 1
        elif event == "api_error":
            api_errors += 1

    def minutes(items):
        return round(sum(max(0.0, e["end"] - e["start"]) for e in items) / 60, 1)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = (f"{stamp} 近{args.days}天：账号离线 {len(account)} 段/{minutes(account)} 分钟；"
            f"接口断连 {len(interface)} 段/{minutes(interface)} 分钟；"
            f"发送 {sends}；命令 {commands}；接口错误 {api_errors}")
    if account:
        line += "；离线时间：" + "、".join(
            datetime.fromtimestamp(e["start"]).strftime("%m-%d %H:%M") for e in account)

    target = Path(args.out) if args.out else (log_dir / "qq-weekly-report.log")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
