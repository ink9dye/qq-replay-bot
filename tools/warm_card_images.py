# -*- coding: utf-8 -*-
"""预热当前前100最新构筑的卡图；缺失图从百鸽新 CDN 拉取到本地。"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(r'D:\pythons\qq-replay-bot')
sys.path.insert(0, str(ROOT))
from bot.statistics_deck_naming import _latest_views

DATA = ROOT / 'data'
root = Path(json.loads((DATA / 'card-library.json').read_text(encoding='utf-8-sig'))['ygopro_root'])
pics = root / 'pics'
c = sqlite3.connect('file:D:/pythons/qq-replay-bot/data/bot.sqlite3?mode=ro', uri=True)
c.row_factory = sqlite3.Row
latest = _latest_views(c, time.time(), DATA / 'replays')
codes = set()
for view in latest.values():
    for row in c.execute('SELECT code FROM stat_build_cards WHERE build=? AND copies>0', (view['build'],)):
        codes.add(int(row['code']))
missing = [code for code in codes if not any((pics / f'{code}{ext}').is_file() for ext in ('.jpg', '.png', '.webp'))]
print('builds', len(latest), 'codes', len(codes), 'missing', len(missing), flush=True)


def fetch(code):
    url = f'https://cdntx.moecube.com/ygopro-super-pre/data/pics/{code}.jpg'
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            blob = response.read(8 * 1024 * 1024 + 1)
        if not blob or len(blob) > 8 * 1024 * 1024:
            return code, 'bad'
        target = pics / f'{code}.jpg'
        temporary = target.with_suffix('.jpg.tmp')
        temporary.write_bytes(blob)
        temporary.replace(target)
        return code, 'ok'
    except Exception as exc:
        return code, type(exc).__name__


start = time.time()
ok = 0
fail = {}
with ThreadPoolExecutor(max_workers=8, thread_name_prefix='warm-card') as pool:
    futures = [pool.submit(fetch, code) for code in missing]
    for index, future in enumerate(as_completed(futures), 1):
        code, status = future.result()
        if status == 'ok':
            ok += 1
        else:
            fail[status] = fail.get(status, 0) + 1
        if index % 10 == 0:
            print('progress', index, '/', len(missing), 'ok', ok, 'elapsed', round(time.time() - start, 1), flush=True)
print('done ok', ok, 'fail', fail, 'elapsed', round(time.time() - start, 1), flush=True)
