# -*- coding: utf-8 -*-
"""录像筛选 → 按当前全部条件打包 ZIP。

和录像页的筛选共用同一个 query（同一套资格、过期、排除、排名与卡组口径），
区别只有两点：不受页面 2000 条显示上限影响；按 digest 去重后只打包原始 .yrp。

失败即整体失败（不返回"少几个文件"的包）：源过期、被排除、文件缺失或字节变了都抛 ValueError，
由调用方转成 409；这样下载到的包永远和筛选结果一致。所有判断都是只读。
"""
from __future__ import annotations

import csv
import hashlib
import io
import time
import zipfile
from datetime import datetime

from .share import safe_replay_file
from .statistics_recording_filters import query as _query
from .statistics_recording_names import CST, safe_filename

MAX_VIEWS = 5000          # 命中的玩家小局视角上限（超过请缩小条件）
MAX_FILES = 2000          # 去重后的录像文件上限
MAX_BYTES = 256 * 1024 * 1024

FIRST_LABEL = {1: '先攻', 0: '后攻', None: '未知'}
OUTCOME_LABEL = {'win': '胜', 'loss': '负', 'draw': '平', 'unknown': '未知'}
MANIFEST = '_manifest.csv'


def _manifest_text(entries) -> str:
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(['日期', '场次', '小局', '玩家', '我方卡组', '对手', '对手卡组',
                     '先后攻', '结果', '玩家当前排名', '对手当前排名', '录像文件', 'digest'])
    for row, name in entries:
        writer.writerow([
            row.get('observed_date') or '',
            row.get('observed_match_no') or '',
            row.get('observed_game_no') or '',
            row.get('player') or '',
            row.get('deck_type') or '卡组未识别',
            row.get('opponent') or '',
            row.get('opponent_deck_type') or '卡组未识别',
            FIRST_LABEL.get(row.get('first_player'), '未知'),
            OUTCOME_LABEL.get(row.get('outcome'), '未知'),
            row.get('current_rank') or '',
            row.get('opponent_current_rank') or '',
            name,
            row.get('digest') or '',
        ])
    # 带 BOM，Excel 直接双击打开不乱码。
    return '\ufeff' + buffer.getvalue()


def _order(row):
    return (str(row.get('observed_date') or ''),
            int(row.get('observed_match_no') or 0),
            int(row.get('observed_game_no') or 999),
            str(row.get('digest') or ''))


def filtered_archive(db, root, now, args, clock=time.time, query=None, limits=None):
    """返回 (zip_bytes, 下载文件名, 文件数, 命中视角数)。args 与 /statistics/api/recordings 相同。"""
    query = query or _query
    limits = limits or {}
    max_views = int(limits.get('views') or MAX_VIEWS)
    max_files = int(limits.get('files') or MAX_FILES)
    max_bytes = int(limits.get('bytes') or MAX_BYTES)
    # 多取一条用来判断是否超过视角上限；超过就整体拒绝，避免给出不完整的包。
    result = query(db, root, now, args, limit=max_views + 1)
    total = int(result.get('total') or 0)
    samples = result.get('samples') or []
    if total == 0 or not samples:
        raise ValueError('empty_scope')
    if total > max_views:
        raise ValueError('scope_too_large')
    current = max(float(now), float(clock()))
    output = io.BytesIO()
    seen, names, size, entries = set(), set(), 0, []
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as package:
        for row in sorted(samples, key=_order):
            digest = str(row.get('digest') or '')
            if not digest or digest in seen:
                continue
            if len(seen) >= max_files:
                raise ValueError('archive_too_many_files')
            expires_at = row.get('expires_at')
            if expires_at is not None and float(expires_at) <= current:
                raise ValueError('source_expired_during_archive')
            if db.execute("SELECT 1 FROM stat_exclusions WHERE digest=? AND (player='' OR player=?) "
                          "AND scope='default'", (digest, row.get('player'))).fetchone():
                raise ValueError('source_excluded')
            candidates = db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f '
                                    'ON f.replay_id=r.id WHERE f.digest=? ORDER BY r.id',
                                    (digest,)).fetchall()
            path = next((found for candidate in candidates
                         if (found := safe_replay_file(root, candidate['rel_path'])) is not None), None)
            if path is None:
                raise ValueError('source_missing')
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != digest:
                raise ValueError('source_changed')
            size += len(blob)
            if size > max_bytes:
                raise ValueError('archive_too_large')
            name = str(row.get('download_filename') or '')
            if not name:
                name = safe_filename(str(row.get('recording_title') or digest))
            if name in names:
                name = safe_filename(str(row.get('recording_title') or digest) + ' ' + digest[:12])
            names.add(name)
            observed_date = str(row.get('observed_date') or '未知日期')
            match_no = row.get('observed_match_no')
            player_folder = safe_filename(str(row.get('player') or '未知玩家').strip(), '')
            match_folder = f'{observed_date} 第{int(match_no)}场/' if match_no else ''
            folder = player_folder + '/' + match_folder
            package.writestr(folder + name, blob)
            package.writestr('总/' + name, blob)
            entries.append((row, folder + name))
            seen.add(digest)
        if entries:
            package.writestr(MANIFEST, _manifest_text(entries))
    label = '录像筛选 ' + datetime.fromtimestamp(current, CST).strftime('%Y-%m-%d %H-%M')
    return output.getvalue(), safe_filename(label, '.zip'), len(seen), total
