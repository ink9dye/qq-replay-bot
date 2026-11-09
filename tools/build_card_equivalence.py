# -*- coding: utf-8 -*-
"""生成同名同效果异画/先行号映射 data/card-equivalence.json。

公开卡库（data/cache/cards.json）收的是正式卡号，本地 YGOPro 卡库
（card-library.json 指向的 cards.cdb + expansions/*.cdb）里还有先行号
（1013xxxxx）。两边卡名、效果文字和卡面属性完全一致时视为同一张卡，
统计侧由 bot/card_identity.reference 合并，避免同一张卡被拆成两行、
按卡名查询落到没有数据的那个号。

用法（在 /opt/ygobot 下）：
    /usr/bin/python3 tools/build_card_equivalence.py            # 写 data/card-equivalence.json
    /usr/bin/python3 tools/build_card_equivalence.py --dry-run  # 只打印统计
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

FIELDS = ('type', 'atk', 'def', 'level', 'race', 'attribute')


def normalize(value):
    return ''.join(str(value or '').split())


def load_local(root):
    """本地 YGOPro 卡库：datas + texts，按卡号去重（主库优先）。"""
    records = {}
    files = [root / 'cards.cdb', *sorted((root / 'expansions').glob('*.cdb'))]
    for path in files:
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(texts)')}
                texts = {row[0]: row[1] for row in db.execute('SELECT id,name FROM texts')}
                descs = ({row[0]: row[1] for row in db.execute('SELECT id,desc FROM texts')}
                         if 'desc' in columns else {})
                for code, *attrs in db.execute(
                        'SELECT id,type,atk,def,level,race,attribute FROM datas'):
                    name = texts.get(code)
                    if not isinstance(name, str) or not name.strip():
                        continue
                    records.setdefault(code, dict(
                        name=name.strip(), effect=descs.get(code) or '',
                        **dict(zip(FIELDS, attrs)), source='local'))
        except sqlite3.Error:
            continue
    return records


def load_public(data_dir):
    path = Path(data_dir) / 'cache/cards.json'
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    records = {}
    for key, info in raw.items():
        if not isinstance(info, dict):
            continue
        code = info.get('id') if ('id' in info or 'cid' in info) else key
        if isinstance(code, str) and code.isdigit():
            code = int(code)
        if type(code) is not int or not 0 < code <= 4294967295:
            continue
        text = info.get('text') or {}
        data = info.get('data') or {}
        records[code] = dict(
            name=str(info.get('cn_name') or text.get('name') or '').strip(),
            effect=text.get('desc') or '',
            **{field: data.get(field) for field in FIELDS}, source='public')
    return records


def build(records, public_codes):
    buckets = {}
    for code, row in records.items():
        if not row['name']:
            continue
        key = (normalize(row['name']), normalize(row['effect']),
               *(row.get(field) for field in FIELDS))
        buckets.setdefault(key, []).append(code)
    groups = []
    for codes in buckets.values():
        if len(codes) < 2:
            continue
        codes.sort()
        official = [code for code in codes if code in public_codes]
        groups.append(dict(canonical=min(official or codes), codes=codes,
                           name=records[codes[0]]['name']))
    groups.sort(key=lambda group: group['canonical'])
    return groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='/opt/ygobot/data')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    library = json.loads((data_dir / 'card-library.json').read_text(encoding='utf-8-sig'))
    local = load_local(Path(library['ygopro_root']))
    public = load_public(data_dir)
    merged = dict(local)
    merged.update(public)
    groups = build(merged, set(public))
    covered = sum(len(group['codes']) for group in groups)
    body = dict(version=1, generated_at=int(time.time()),
                generator='tools/build_card_equivalence.py',
                sources=dict(local_cards=len(local), public_cards=len(public)),
                groups=groups)
    body['reference_version'] = hashlib.sha256(
        json.dumps(body['groups'], sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    print(json.dumps(dict(groups=len(groups), codes=covered, local=len(local),
                          public=len(public), reference_version=body['reference_version']),
                     ensure_ascii=False))
    for group in groups[:5]:
        print(json.dumps(group, ensure_ascii=False))
    if args.dry_run:
        return
    target = data_dir / 'card-equivalence.json'
    temp = target.with_suffix('.json.tmp')
    temp.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
    temp.replace(target)  # 原子替换，避免服务读到半截 JSON
    print('written', target, target.stat().st_size)


if __name__ == '__main__':
    main()
