# -*- coding: utf-8 -*-
"""一次性补齐非前100玩家的官方名次缓存（7 天 TTL）。

只读主库、只写 data/statistics-rank-cache.json；不重启服务、不写事实库。
前200 口径依赖这份缓存：缺名次的玩家会被判为“不在前200”，样本会偏小。
"""
import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def write_snapshot(data_dir, ranks):
    """独立快照：只由本工具写，不会被其它进程的迟到请求整份改写。"""
    cleaned = {str(name).strip(): int(rank) for name, rank in ranks.items()
               if isinstance(rank, int) and rank > 0 and str(name).strip()}
    if not cleaned:
        return 0
    path = Path(data_dir) / 'rank-scope-index.json'
    payload = {'version': 1, 'saved_at': time.time(), 'players': dict(sorted(cleaned.items()))}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)
    return len(cleaned)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/opt/ygobot')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    from bot.arena_profile import fetch_profile, parse_profile
    from bot.statistics_rank_cache import load, update

    database = root / 'data' / 'bot.sqlite3'
    db = sqlite3.connect('file:%s?mode=ro' % database, uri=True)
    roster = {row[0] for row in db.execute('SELECT player FROM stat_current_top100')}
    players = {row[0] for row in db.execute('SELECT DISTINCT player FROM stat_views')} - roster
    db.close()

    now = time.time()
    known = load(database.parent, now)
    todo = sorted(name for name in players if name.strip() not in known)
    if args.limit:
        todo = todo[:args.limit]
    print('epoch=%.0f players_outside_roster=%d known=%d todo=%d'
          % (now, len(players), len(known), len(todo)), flush=True)
    if args.dry_run or not todo:
        return 0

    def work(name):
        try:
            started = time.time()
            rank = parse_profile(fetch_profile(name), name, started)['rank']
            return name, rank if isinstance(rank, int) and rank > 0 else None
        except Exception:
            return name, None

    done = {}
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for index, (name, rank) in enumerate(pool.map(work, todo), 1):
            if rank:
                done[name] = rank
            if index % 200 == 0:
                print('fetched %d/%d ok=%d elapsed=%.0fs' % (index, len(todo), len(done), time.time() - started), flush=True)
                update(database.parent, done, time.time())
    if done:
        update(database.parent, done, time.time())
    full = {name: rank for name, (rank, _) in known.items()}
    full.update(done)
    snapshot = write_snapshot(database.parent, full)
    after = load(database.parent, time.time())
    print('written=%d cache_entries=%d snapshot=%d elapsed=%.0fs'
          % (len(done), len(after), snapshot, time.time() - started), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
