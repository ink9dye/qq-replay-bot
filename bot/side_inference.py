# -*- coding: utf-8 -*-
"""离线推断 Side 并写入 data/cache/side-inference.json；不阻塞网页弹窗。

建议在无录制时跑：
    sudo -u ygo python3 -m bot.side_inference --root /opt/ygobot --limit 100

批量任务只加载一次缓存、结束时一次原子写回（经 side_cache.commit 的加锁合并）：
旧实现每个候选都重新解析整份缓存（45MB 量级），一次 run 会因此耗到十分钟并占满 web 进程。
推断规则、参数、证据规则不变；超时/异常时缓存保持原样，不写半成品。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

try:  # 仅用于记录峰值内存；非 POSIX 平台没有 resource 模块
    import resource
except ImportError:  # pragma: no cover
    resource = None


def _peak_mb():
    if resource is None:  # pragma: no cover
        return None
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def run(root, limit=100, state_path=None, deadline=None, stats=None):
    """按“每位玩家最新视角”推断未缓存的构筑；返回本次真正写入条数。

    - 缓存只读一次；结束时用 side_cache.commit 加锁合并 + 原子写回；
    - 判断顺序、写入条数上限、跳过条件与旧实现一致；
    - deadline（time.monotonic 口径）到点即停：已推断的条目照常合并落盘，
      stats['partial']=True，调用方不得把这种情况当成完整成功；
    - 可选把进度写到 state_path（原子替换），供外部观察。
    """
    from . import statistics_build_detail, statistics_deck_naming, side_cache
    root = Path(root).resolve()
    cfg = json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))
    data = Path(cfg.get('data_dir') or root / 'data')
    replays = Path(cfg.get('replay_dir') or data / 'replays')
    now = time.time()
    started = time.time()
    cache = side_cache.load(data)
    cache_stamp = side_cache.stamp(data)
    added = {}
    written = 0
    considered = 0
    skipped_cached = 0
    no_side = 0
    partial = False
    with closing(sqlite3.connect((data / 'bot.sqlite3').as_uri() + '?mode=ro', uri=True,
                                 timeout=10)) as db:
        db.row_factory = sqlite3.Row
        views = statistics_deck_naming._latest_views(db, now, None)
        for player, view in views.items():
            if written >= limit:
                break
            if deadline is not None and time.monotonic() >= deadline:
                partial = True
                break
            considered += 1
            build = view.get('build')
            if not build or cache.get(str(build)):
                skipped_cached += 1
                continue
            result = statistics_build_detail.query(db, replays, now, player, view.get('digest'))
            inferred = (result or {}).get('inferred_side')
            if not inferred or not inferred.get('cards'):
                no_side += 1
                continue
            added[str(build)] = inferred
            written += 1
            print('cached', player, build, len(inferred.get('cards') or []))
    commit = dict(status='nochange', inserted=0, entries=len(cache))
    if added:
        # 把已加载的快照交给 commit：文件未变就不再解析第二份 45MB 缓存。
        commit = side_cache.commit(data, added, base=cache, base_stamp=cache_stamp)
        if commit.get('status') == 'conflict':
            # 期间有人写了文件：先释放旧快照，再重试一次（此时内存里只有一份新快照）；
            # 仍冲突则本轮不提交，留待下轮重算，绝不覆盖别人的写入。
            cache = None
            commit = side_cache.commit(data, added)
    state = dict(started_at=started, finished_at=time.time(), players=len(views),
                 considered=considered, skipped_cached=skipped_cached, no_side=no_side,
                 inferred=len(added), written=commit.get('inserted'), limit=limit,
                 commit_status=commit.get('status'), entries=commit.get('entries'),
                 partial=partial, peak_mb=_peak_mb())
    if stats is not None:
        stats.update(state)
    if state_path:
        path = Path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    return commit.get('inserted') or 0


def main():
    parser = argparse.ArgumentParser(description='离线推断 Side 并缓存')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--state', type=Path, default=None)
    parser.add_argument('--max-seconds', type=float, default=0.0)
    args = parser.parse_args()
    deadline = time.monotonic() + args.max_seconds if args.max_seconds else None
    stats = {}
    written = run(args.root, max(1, args.limit), state_path=args.state, deadline=deadline,
                  stats=stats)
    print(json.dumps(dict(written=written, partial=stats.get('partial'),
                          commit_status=stats.get('commit_status')), ensure_ascii=False))


if __name__ == '__main__':
    main()
