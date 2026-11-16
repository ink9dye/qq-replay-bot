#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填小局身份索引 stat_match_identity。

只读 stat_evidence 的证据推导一次，把“场次/局次/是否从开局录”写进小表；
查询此后不再解析三万多条 payload_json。可重复执行，已建索引的 digest 默认跳过。

    python3 tools/backfill_match_identity.py --root /opt/ygobot
    python3 tools/backfill_match_identity.py --root /opt/ygobot --dry-run
    python3 tools/backfill_match_identity.py --root /opt/ygobot --verify 200
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--limit', type=int, default=0, help='只回填前 N 条（试跑用）')
    parser.add_argument('--dry-run', action='store_true', help='只统计待回填数量')
    parser.add_argument('--verify', type=int, default=0, metavar='N', help='回填后随机核对 N 条')
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))

    database = root / 'data' / 'bot.sqlite3'
    if not database.is_file():
        raise SystemExit('找不到 %s' % database)
    from bot.statistics_facts import initialize
    from bot.statistics_match_identity import _read_capture_metadata
    from bot.statistics_match_index import lookup, rebuild

    db = sqlite3.connect(str(database), timeout=60)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=60000')
    try:
        initialize(db)
        digests = [row[0] for row in db.execute('SELECT digest FROM stat_games')]
        _, indexed = lookup(db, digests)
        todo = [digest for digest in digests if digest not in indexed]
        print('录像 %d 条，已建索引 %d 条，待回填 %d 条' % (
            len(digests), len(indexed), len(todo)))
        if args.limit:
            todo = todo[:args.limit]
            print('本次只处理前 %d 条' % len(todo))
        if args.dry_run or not todo:
            return 0
        started = time.time()
        values = _read_capture_metadata(db, todo)
        complete = {digest: values.get(digest) for digest in todo}
        with db:
            rebuild(db, complete)
        resolved = sum(1 for value in complete.values() if value)
        print('已回填 %d 条（有可靠身份 %d 条，明确无身份 %d 条），耗时 %.1f 秒' % (
            len(todo), resolved, len(todo) - resolved, time.time() - started))

        if args.verify:
            sample = random.sample(todo, min(args.verify, len(todo)))
            fresh = _read_capture_metadata(db, sample)
            stored, _ = lookup(db, sample)
            mismatch = [digest for digest in sample
                        if stored.get(digest) != fresh.get(digest)]
            print('抽查 %d 条，与现算结果不一致 %d 条' % (len(sample), len(mismatch)))
            for digest in mismatch[:5]:
                print('  %s 表内=%r 现算=%r' % (digest[:12], stored.get(digest), fresh.get(digest)))
            return 1 if mismatch else 0
    finally:
        db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
