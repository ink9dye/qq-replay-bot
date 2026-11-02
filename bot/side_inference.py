# -*- coding: utf-8 -*-
"""离线推断 Side 并写入 data/cache/side-inference.json；不阻塞网页弹窗。

建议在无录制时跑：
    sudo -u ygo python3 -m bot.side_inference --root /opt/ygobot --limit 100
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def run(root, limit=100):
    from . import statistics_build_detail, statistics_deck_naming, side_cache
    from . import series_naming
    root = Path(root).resolve()
    cfg = json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))
    data = Path(cfg.get('data_dir') or root / 'data')
    replays = Path(cfg.get('replay_dir') or data / 'replays')
    now = time.time()
    written = 0
    with closing(sqlite3.connect((data / 'bot.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        views = statistics_deck_naming._latest_views(db, now, None)
        for player, view in views.items():
            if written >= limit:
                break
            build = view.get('build')
            if not build or side_cache.get(data, build):
                continue
            result = statistics_build_detail.query(db, replays, now, player, view.get('digest'))
            inferred = (result or {}).get('inferred_side')
            if not inferred or not inferred.get('cards'):
                continue
            side_cache.put(data, build, inferred)
            written += 1
            print('cached', player, build, len(inferred.get('cards') or []))
    return written


def main():
    parser = argparse.ArgumentParser(description='离线推断 Side 并缓存')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    print('written', run(args.root, max(1, args.limit)))


if __name__ == '__main__':
    main()
