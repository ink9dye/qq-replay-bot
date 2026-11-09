#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把旧的 data/cache/side-inference.json 迁移到 SQLite（流式读，内存只占单条）。

- 逐条解析顶层对象，边解析边 INSERT OR IGNORE，不把 171 MB JSON 整体载入；
- 完成后把旧 JSON 改名为 side-inference.json.migrated-<时间>（保留可回滚）；
- 可重复执行：已有键不覆盖。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'bot') not in sys.path:
    sys.path.insert(0, str(ROOT / 'bot'))

import side_cache  # noqa: E402


def iter_json_object(path, chunk=1024 * 1024):
    """流式解析顶层 JSON 对象，逐 (key, value) 产出。"""
    decoder = json.JSONDecoder()
    handle = open(path, 'r', encoding='utf-8')
    buf = ''
    pos = 0
    eof = False

    def fill():
        nonlocal buf, pos, eof
        if eof:
            return False
        data = handle.read(chunk)
        if not data:
            eof = True
            return False
        buf = buf[pos:] + data
        pos = 0
        return True

    def decode():
        nonlocal pos
        while True:
            if pos >= len(buf):
                if not fill():
                    raise ValueError('unexpected eof')
            if buf[pos] in ' \t\r\n':
                pos += 1
                continue
            break
        start = pos
        while True:
            try:
                value, end = decoder.raw_decode(buf, start)
                pos = end
                return value
            except json.JSONDecodeError:
                if not fill():
                    raise
                start = 0

    try:
        while True:
            if pos >= len(buf):
                if not fill():
                    raise ValueError('empty json')
            char = buf[pos]
            if char in ' \t\r\n':
                pos += 1
                continue
            if char == '{':
                pos += 1
                break
            raise ValueError('top level is not an object')

        while True:
            while True:
                if pos >= len(buf):
                    if not fill():
                        raise ValueError('unexpected eof')
                char = buf[pos]
                if char in ' \t\r\n,':
                    pos += 1
                    continue
                break
            if buf[pos] == '}':
                pos += 1
                return
            key = decode()
            while True:
                if pos >= len(buf):
                    if not fill():
                        raise ValueError('unexpected eof')
                char = buf[pos]
                if char in ' \t\r\n':
                    pos += 1
                    continue
                if char == ':':
                    pos += 1
                    break
                raise ValueError('expected colon')
            value = decode()
            yield str(key), value
    finally:
        handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default='/opt/ygobot/data')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--keep-json', action='store_true', default=True,
                        help='rename old json instead of deleting (default)')
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    source = side_cache.json_path(data_dir)
    if not source.is_file():
        print('no json to migrate:', source)
        return 0

    started = time.time()
    count = 0
    db = side_cache.connect(data_dir)
    try:
        db.execute('BEGIN')
        batch = []
        for key, value in iter_json_object(source):
            count += 1
            if args.dry_run:
                continue
            batch.append((key, json.dumps(value, ensure_ascii=False), time.time()))
            if len(batch) >= 200:
                db.executemany('INSERT OR IGNORE INTO entries(build,payload,updated) VALUES(?,?,?)',
                               batch)
                db.execute('COMMIT')
                db.execute('BEGIN')
                batch = []
        if batch and not args.dry_run:
            db.executemany('INSERT OR IGNORE INTO entries(build,payload,updated) VALUES(?,?,?)',
                           batch)
        db.execute('COMMIT')
        entries = db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
    finally:
        db.close()

    elapsed = time.time() - started
    print('parsed %d entries, db has %d, %.1fs, dry_run=%s' % (count, entries, elapsed, args.dry_run))
    if args.dry_run:
        return 0
    if count and entries < count:
        print('WARN: db entries %d < parsed %d（可能有重复键）' % (entries, count))
    if args.keep_json:
        target = source.with_name(source.name + '.migrated-' + time.strftime('%Y%m%d-%H%M%S'))
        source.replace(target)
        print('renamed', target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
