#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按当前组合命名规则把全库构筑名重算一遍（运维用，分批提交）。

后台 naming_loop 每 2 分钟只处理 200 副（SERIES_NAME_BATCH），配置/规则一变就是
三万副的存量，靠它要跑十几个小时，这期间页面上新旧名字混杂。这个脚本复用同一套
plan/apply_plan，但自己按 --chunk 分批提交，几分钟追平；配合 nice -n 19 跑，
不和用户的请求抢 CPU。

    python3 tools/recompute_series_names.py --root /opt/ygobot --dry-run
    python3 tools/recompute_series_names.py --root /opt/ygobot --apply
    python3 tools/recompute_series_names.py --root /opt/ygobot --apply --chunk 1000 --limit 2000
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--chunk', type=int, default=1000, help='每批提交多少副（默认 1000）')
    parser.add_argument('--limit', type=int, default=0, help='只处理前 N 副（试跑用）')
    parser.add_argument('--apply', action='store_true', help='写入数据库（默认只统计待重算数量）')
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))

    from bot import series_naming
    from bot.statistics_card_names import index
    from bot.store import Store

    data, db_path = series_naming._default_paths(root)
    config = series_naming.load_config(data)
    store = Store(db_path)
    try:
        db = store._db
        catalog = series_naming.catalog_from_db(db)
        names = index(data)
        now = time.time()
        pending = series_naming.pending_series_builds(db, now, config, scope='everything')
        total = len(pending)
        print('版本 %s；待重算 %d 副' % (series_naming.version_for(config), total))
        before = db.execute("SELECT count(*) FROM stat_builds WHERE deck_type LIKE '60%'").fetchone()[0]
        print("重算前名字以「60」开头：%d 副" % before)
        if not args.apply:
            sample = pending[:min(5, total)]
            for row in series_naming.plan(db, now, config, catalog, names, builds=sample,
                                          scope='everything')['rows']:
                print('  %s → %s' % ((row.get('current') or '未命名')[:24], row.get('name') or '-'))
            print('（预览；加 --apply 才写库）')
            return 0
        if not total:
            return 0
        step = max(1, int(args.chunk))
        if args.limit:
            pending = pending[:max(1, int(args.limit))]
            total = len(pending)
        started = time.time()
        changed = 0
        for start in range(0, total, step):
            piece = pending[start:start + step]
            result = series_naming.apply_names_chunk(db, now, config, catalog=catalog, names=names,
                                                     builds=piece, limit=len(piece), commit=True)
            changed += int(result.get('changed') or 0)
            done = min(total, start + step)
            print('  %d/%d 副，累计改名 %d 副，%.1f 秒'
                  % (done, total, changed, time.time() - started), flush=True)
        after = db.execute("SELECT count(*) FROM stat_builds WHERE deck_type LIKE '60%'").fetchone()[0]
        print('完成：%d 副，改名 %d 副，名字以「60」开头 %d → %d 副，用时 %.1f 秒'
              % (total, changed, before, after, time.time() - started))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
