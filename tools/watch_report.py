# -*- coding: utf-8 -*-
"""离线看观战运行日志：data/logs/watch-metrics.jsonl + watch-events.jsonl。

用法：
    python tools/watch_report.py                 # 最近 6 小时小结
    python tools/watch_report.py --hours 24      # 最近一天
    python tools/watch_report.py --events        # 附带事件（startup/pause/breaker/shutdown）
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(r"D:\pythons\qq-replay-bot\data") if Path(r"D:\pythons\qq-replay-bot\data").is_dir() else ROOT / "data"


def load(name):
    path = DATA / "logs" / name
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def hour_key(item):
    return time.strftime("%m-%d %H", time.localtime(item.get("at") or 0))


def report(hours, show_events):
    since = time.time() - hours * 3600
    metrics = [row for row in load("watch-metrics.jsonl") if (row.get("at") or 0) >= since]
    print(f"=== watch-metrics（最近 {hours} 小时，{len(metrics)} 条）===")
    if not metrics:
        print("没有数据：确认 watch 已重启并加载了带指标的版本。")
        return
    groups = OrderedDict()
    for row in metrics:
        groups.setdefault(hour_key(row), []).append(row)
    previous = None
    for hour, rows in groups.items():
        last = rows[-1]
        entry = {
            "hour": hour,
            "cap": last.get("cap"),
            "active": last.get("active"),
            "waiting": last.get("waiting"),
            "started": last.get("started"),
            "failed": last.get("failed"),
            "pauses": last.get("pauses"),
            "saved": last.get("saved"),
            "silent": last.get("silent"),
            "secret_age": last.get("mycard_secret_age"),
        }
        if previous:
            def delta(key):
                try:
                    return int(last.get(key) or 0) - int(previous.get(key) or 0)
                except (TypeError, ValueError):
                    return None
            entry["d_started"] = delta("started")
            entry["d_failed"] = delta("failed")
            entry["d_saved"] = delta("saved")
            entry["d_pauses"] = delta("pauses")
        previous = last
        print(json.dumps(entry, ensure_ascii=False))
    last = metrics[-1]
    flags = []
    if (last.get("silent") or 0) > 0:
        flags.append(f"silent 累计 {last.get('silent')}（0 包静默关闭）")
    if last.get("block_reason"):
        flags.append(f"闸门阻塞中：{last.get('block_reason')}")
    try:
        if last.get("mycard_secret_age") is not None and float(last["mycard_secret_age"]) > 240:
            flags.append(f"凭据年龄 {last['mycard_secret_age']}s（>240s，注意刷新）")
    except (TypeError, ValueError):
        pass
    print("=== 当前 ===")
    print(json.dumps({k: last.get(k) for k in ("cap", "active", "waiting", "block_reason",
                                               "join_window", "success_window", "silent_window",
                                               "silent_ip_seconds", "silent_ip_windows",
                                               "peak_in_flight_window", "mycard_secret_age",
                                               "probation_left", "catchup_joined")},
                     ensure_ascii=False))
    print("=== 提示 ===")
    for flag in flags or ["无异常"]:
        print(" -", flag)
    if show_events:
        events = [row for row in load("watch-events.jsonl") if (row.get("at") or 0) >= since]
        print(f"=== 事件（最近 {hours} 小时）===")
        print(dict(Counter(row.get("event") for row in events)))
        for row in events[-12:]:
            print(json.dumps(row, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="观战运行日志小结（离线只读）")
    parser.add_argument("--hours", type=float, default=6)
    parser.add_argument("--events", action="store_true")
    args = parser.parse_args()
    report(args.hours, args.events)


if __name__ == "__main__":
    main()
