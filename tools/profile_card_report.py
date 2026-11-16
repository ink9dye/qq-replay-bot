#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对一次卡片报表查询做 cProfile 剖析，定位 cold 查询的时间去向。

默认复现页面默认参数（100/100、opening、无筛选），也可传 --own/--opponent/--code。
只读数据库，不写缓存；建议低峰期用 nice 跑，避免和预计算抢 I/O。

    nice -n 19 python3 tools/profile_card_report.py --root /opt/ygobot
    nice -n 19 python3 tools/profile_card_report.py --own 200 --opponent 200
    nice -n 19 python3 tools/profile_card_report.py --code 14558127 --top 40
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import statistics_card_report as card_report  # noqa: E402
from bot.card_statistics import Filters  # noqa: E402
from bot.statistics_rank_scope import scoped_query  # noqa: E402

PHASES = ('main_first', 'main_second', 'side_first', 'side_second')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--own', default='100', help='我方范围：100 / 200 / all')
    parser.add_argument('--opponent', default='100', help='对手范围：100 / 200 / all')
    parser.add_argument('--code', action='append', default=[], help='只看某张卡（可重复）')
    parser.add_argument('--metric', default='opening', choices=('opening', 'drawn', 'carried'))
    parser.add_argument('--limit', default='500')
    parser.add_argument('--top', type=int, default=30, help='打印前 N 个函数')
    parser.add_argument('--save', type=Path, default=None, help='把 .prof 存到指定路径')
    args = parser.parse_args()

    root = args.root.resolve()
    database = root / 'data' / 'bot.sqlite3'
    replays = root / 'data' / 'replays'
    if not database.is_file():
        print('找不到 %s' % database, file=sys.stderr)
        return 2

    db = sqlite3.connect(str(database), timeout=60)
    db.row_factory = sqlite3.Row
    filters = Filters((), (), (), (), None, False, deck_contains=True)
    now = time.time()
    started = time.perf_counter()
    profile = cProfile.Profile()
    try:
        value = scoped_query((args.own, args.opponent), lambda: profile.runcall(
            card_report.query, db, replays, now, codes=tuple(args.code), filters=filters,
            metric=args.metric, threshold='1', min_games=0, sort='rate',
            partition='auto', limit=args.limit, phases=PHASES))
    finally:
        db.close()
    elapsed = time.perf_counter() - started

    print('范围 %s/%s codes=%s metric=%s 耗时 %.1f 秒，total=%s shown=%s' % (
        args.own, args.opponent, list(args.code) or '全部', args.metric, elapsed,
        value.get('total'), value.get('shown')))
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        profile.dump_stats(str(args.save))
        print('剖面已存 %s' % args.save)
    stats = pstats.Stats(profile)
    print('累计耗时最高的 %d 个函数（cumulative）：' % args.top)
    stats.sort_stats('cumulative').print_stats(args.top)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
