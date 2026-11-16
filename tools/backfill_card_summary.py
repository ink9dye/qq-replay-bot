#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填卡片报表的紧凑摘要表（stat_build_card_sets / stat_view_card_sets）。

流式读 stat_build_cards 与 stat_card_facts，按主键分组编码成 6 字节/条 的 BLOB。
可重复执行；已存在的行会覆盖。--verify N 会随机抽 N 条重建并逐字节比对。
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import statistics_card_summary as summary  # noqa: E402


def batched(rows, key_index):
    def key_of(row):
        return tuple(row[i] for i in key_index) if isinstance(key_index, tuple) else row[key_index]
    group_key = None
    group = []
    for row in rows:
        key = key_of(row)
        if group_key is not None and key != group_key:
            yield group_key, group
            group = []
        group_key = key
        group.append(row)
    if group:
        yield group_key, group


def backfill_builds(read_db, write_db, batch=1000):
    now = time.time()
    pending = []
    total = 0
    for build, rows in batched(read_db.execute(
            'SELECT build,partition,code,copies FROM stat_build_cards ORDER BY build'), 0):
        data = summary.encode_build((r[1], r[2], r[3]) for r in rows)
        if data:
            pending.append((build, data, now))
            total += 1
        if len(pending) >= batch:
            write_db.executemany('INSERT INTO stat_build_card_sets VALUES(?,?,?) '
                                 'ON CONFLICT(build) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                                 pending)
            write_db.commit()
            pending.clear()
    if pending:
        write_db.executemany('INSERT INTO stat_build_card_sets VALUES(?,?,?) '
                             'ON CONFLICT(build) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                             pending)
    write_db.commit()
    return total


def backfill_views(read_db, write_db, batch=1000):
    now = time.time()
    pending = []
    total = 0
    for (digest, player), rows in batched(read_db.execute(
            'SELECT digest,player,partition,code,drawn,used,used_after_draw,initial_count '
            'FROM stat_card_facts ORDER BY digest,player'), (0, 1)):
        data = summary.encode_view((r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows)
        if data:
            pending.append((digest, player, data, now))
            total += 1
        if len(pending) >= batch:
            write_db.executemany('INSERT INTO stat_view_card_sets VALUES(?,?,?,?) '
                                 'ON CONFLICT(digest,player) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                                 pending)
            write_db.commit()
            pending.clear()
    if pending:
        write_db.executemany('INSERT INTO stat_view_card_sets VALUES(?,?,?,?) '
                             'ON CONFLICT(digest,player) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                             pending)
    write_db.commit()
    return total


def verify(db, count):
    bad = 0
    builds = [r[0] for r in db.execute('SELECT build FROM stat_build_card_sets ORDER BY RANDOM() LIMIT ?', (count,))]
    for build in builds:
        rows = db.execute('SELECT partition,code,copies FROM stat_build_cards WHERE build=?', (build,))
        expected = summary.encode_build(rows)
        stored = db.execute('SELECT data FROM stat_build_card_sets WHERE build=?', (build,)).fetchone()[0]
        bad += expected != stored
    views = [tuple(r) for r in db.execute(
        'SELECT digest,player FROM stat_view_card_sets ORDER BY RANDOM() LIMIT ?', (count,))]
    for digest, player in views:
        rows = db.execute('SELECT partition,code,drawn,used,used_after_draw,initial_count '
                          'FROM stat_card_facts WHERE digest=? AND player=?', (digest, player))
        expected = summary.encode_view(rows)
        stored = db.execute('SELECT data FROM stat_view_card_sets WHERE digest=? AND player=?',
                            (digest, player)).fetchone()[0]
        bad += expected != stored
    return bad, len(builds) + len(views)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--database', default='')
    parser.add_argument('--verify', type=int, default=0)
    parser.add_argument('--skip-builds', action='store_true')
    args = parser.parse_args()
    root = Path(args.root)
    database = Path(args.database) if args.database else root / 'data' / 'bot.sqlite3'
    read_db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=60)
    db = sqlite3.connect(str(database), timeout=60)
    db.execute('PRAGMA busy_timeout=60000')
    summary.initialize(db)
    db.commit()
    started = time.time()
    builds = 0 if args.skip_builds else backfill_builds(read_db, db)
    print('builds %d %.1fs' % (builds, time.time() - started), flush=True)
    started = time.time()
    views = backfill_views(read_db, db)
    print('views %d %.1fs' % (views, time.time() - started), flush=True)
    build_bytes = db.execute('SELECT COALESCE(SUM(LENGTH(data)),0) FROM stat_build_card_sets').fetchone()[0]
    view_bytes = db.execute('SELECT COALESCE(SUM(LENGTH(data)),0) FROM stat_view_card_sets').fetchone()[0]
    print('blob MB %.1f (builds %.1f + views %.1f)' % ((build_bytes + view_bytes) / 1048576,
                                                       build_bytes / 1048576, view_bytes / 1048576))
    if args.verify:
        bad, checked = verify(db, args.verify)
        print('verify checked=%d mismatch=%d' % (checked, bad))
    read_db.close()
    db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
