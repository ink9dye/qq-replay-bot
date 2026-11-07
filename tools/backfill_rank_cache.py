# -*- coding: utf-8 -*-
"""刷新非前100玩家的官方名次（7 天 TTL），供前200 口径使用。

只读主库；写 data/rank-scope-index.json 快照（原子替换）+ 共享的
data/statistics-rank-cache.json。以 ygo 身份运行（systemd 定时器），
不重启服务、不写事实库。

增量规则：快照比 --max-age-hours 旧就整轮重查，否则只补没有名次的玩家。
"""
import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def write_snapshot(data_dir, ranks, saved_at=None):
    """独立快照：只由本工具写，不会被其它进程的迟到请求整份改写。

    saved_at 是"整轮核对完成"的时间；增量运行沿用上一次的值，避免把整轮刷新
    一直往后推、永远等不到 full。
    """
    cleaned = {str(name).strip(): int(rank) for name, rank in ranks.items()
               if isinstance(rank, int) and rank > 0 and str(name).strip()}
    if not cleaned:
        return 0
    path = Path(data_dir) / 'rank-scope-index.json'
    payload = {'version': 1, 'saved_at': float(saved_at or time.time()),
               'players': dict(sorted(cleaned.items()))}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)
    return len(cleaned)


def snapshot_state(data_dir):
    """返回 (距上次整轮核对的秒数, saved_at)；没有快照就 (None, None)。"""
    try:
        payload = json.loads((Path(data_dir) / 'rank-scope-index.json').read_text(encoding='utf-8'))
        saved = float(payload['saved_at'])
        return time.time() - saved, saved
    except (OSError, ValueError, TypeError, KeyError):
        return None, None


def available_mb():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/opt/ygobot')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--max-age-hours', type=float, default=20.0)
    parser.add_argument('--min-available-mb', type=int, default=250)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    from bot.arena_profile import fetch_profile, parse_profile
    from bot.statistics_rank_cache import update
    from bot.statistics_rank_scope import _stored_ranks

    free = available_mb()
    if free is not None and free < args.min_available_mb:
        print('skip: available_memory=%dMB < %dMB' % (free, args.min_available_mb), flush=True)
        return 0

    data_dir = root / 'data'
    database = data_dir / 'bot.sqlite3'
    db = sqlite3.connect('file:%s?mode=ro' % database, uri=True)
    roster = {row[0] for row in db.execute('SELECT player FROM stat_current_top100')}
    players = {row[0] for row in db.execute('SELECT DISTINCT player FROM stat_views')} - roster
    db.close()

    now = time.time()
    known = _stored_ranks(data_dir, now)
    age, saved_at = snapshot_state(data_dir)
    stale = age is None or age > args.max_age_hours * 3600
    todo = sorted(name for name in players
                  if stale or name.strip() not in known
                  or now - float(known[name.strip()][1]) > args.max_age_hours * 3600)
    if args.limit:
        todo = todo[:args.limit]
    print('epoch=%.0f players=%d known=%d snapshot_age=%s todo=%d full=%s'
          % (now, len(players), len(known),
             'none' if age is None else '%.0fs' % age, len(todo), stale), flush=True)
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
            if index % 500 == 0:
                print('fetched %d/%d ok=%d elapsed=%.0fs'
                      % (index, len(todo), len(done), time.time() - started), flush=True)
    full = {name: rank for name, (rank, _) in known.items()}
    full.update(done)
    if done:
        try:
            update(data_dir, done, time.time())
        except (OSError, TypeError):
            pass
    if not done and not stale:
        print('written=0 snapshot=%d unchanged' % len(full), flush=True)
        return 0
    snapshot = write_snapshot(data_dir, full, time.time() if stale else saved_at)
    print('written=%d snapshot=%d elapsed=%.0fs' % (len(done), snapshot, time.time() - started), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
