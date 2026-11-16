"""卡片统计的紧凑摘要：把每副构筑的卡表、每个视角的卡牌标记压成一条 BLOB。

卡片报表原本要读 stat_build_cards（34 万行 / 覆盖索引 123MB）和
stat_card_facts（69 万行 / 部分索引 28MB + NULL 索引 95MB）。在 2GB 内存、
5MB/s 磁盘的机器上，这些索引冷读一次要十几到二十秒。

这里把同样的信息按主键压成 6 字节一条：

- 构筑：partition(1) + code(4) + copies(1)，平均 40 条 ≈ 240 字节；
- 视角：partition(1) + code(4) + flags(1)，平均 41 条 ≈ 246 字节。

flags 位：1=起手有(initial_count>0)，2=起手 0，4=起手未知(NULL)，
8=抽到，16=使用，32=使用发生在抽到之后。

解码后仍产出与 stat_card_facts 相同的字段形状，报表的合并/组合/口径逻辑不变。
"""
from __future__ import annotations

import time

PART_IDS = {'main': 0, 'side': 1, 'extra': 2, 'other': 3}
PART_NAMES = ('main', 'side', 'extra', 'other')

FLAG_OPENING = 1
FLAG_ZERO = 2
FLAG_NULL = 4
FLAG_DRAWN = 8
FLAG_USED = 16
FLAG_UAD = 32

SCHEMA = '''
CREATE TABLE IF NOT EXISTS stat_build_card_sets (
    build TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_view_card_sets (
    digest TEXT NOT NULL,
    player TEXT NOT NULL,
    data BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (digest, player)
);
'''

# 过期清理先删 stat_games；摘要跟着删，避免报表读到已失效视角。
VIEW_TRIGGER = '''CREATE TRIGGER IF NOT EXISTS stat_view_card_sets_removed
AFTER DELETE ON stat_games
BEGIN
 DELETE FROM stat_view_card_sets WHERE digest=OLD.digest;
END;'''


def initialize(db):
    db.executescript(SCHEMA)
    try:
        db.execute(VIEW_TRIGGER)
    except Exception:
        # 测试库可能没有 stat_games；摘要表仍可用，清理交给调用方。
        pass


def encode_build(rows):
    """rows: (partition, code, copies)"""
    out = bytearray()
    for partition, code, copies in rows:
        part = PART_IDS.get(partition)
        if part is None or not 0 < int(copies) < 256:
            continue
        out += bytes((part,))
        out += int(code).to_bytes(4, 'little')
        out += bytes((int(copies),))
    return bytes(out)


def decode_build(blob):
    for offset in range(0, len(blob), 6):
        part = PART_NAMES[blob[offset]]
        code = int.from_bytes(blob[offset + 1:offset + 5], 'little')
        yield part, code, blob[offset + 5]


def encode_view(rows):
    """rows: (partition, code, drawn, used, used_after_draw, initial_count)"""
    out = bytearray()
    for partition, code, drawn, used, used_after_draw, initial_count in rows:
        part = PART_IDS.get(partition)
        if part is None:
            continue
        flags = 0
        if initial_count is not None:
            flags |= FLAG_OPENING if initial_count > 0 else FLAG_ZERO
        else:
            flags |= FLAG_NULL
        if drawn == 1:
            flags |= FLAG_DRAWN
        if used == 1:
            flags |= FLAG_USED
        if used_after_draw == 1:
            flags |= FLAG_UAD
        out += bytes((part,))
        out += int(code).to_bytes(4, 'little')
        out += bytes((flags,))
    return bytes(out)


def decode_view(blob):
    for offset in range(0, len(blob), 6):
        part = PART_NAMES[blob[offset]]
        code = int.from_bytes(blob[offset + 1:offset + 5], 'little')
        flags = blob[offset + 5]
        if flags & FLAG_OPENING:
            initial = 1
        elif flags & FLAG_ZERO:
            initial = 0
        else:
            initial = None
        yield (part, code, 1 if flags & FLAG_DRAWN else 0,
               1 if flags & FLAG_USED else 0,
               1 if flags & FLAG_UAD else None, initial)


def refresh_build(db, build, now=None):
    rows = db.execute('SELECT partition,code,copies FROM stat_build_cards WHERE build=?', (build,))
    data = encode_build(rows)
    if data:
        db.execute('INSERT INTO stat_build_card_sets VALUES(?,?,?) ON CONFLICT(build) DO UPDATE '
                   'SET data=excluded.data,updated_at=excluded.updated_at',
                   (build, data, float(now or time.time())))
    else:
        db.execute('DELETE FROM stat_build_card_sets WHERE build=?', (build,))


def refresh_view(db, digest, player, now=None):
    rows = db.execute('SELECT partition,code,drawn,used,used_after_draw,initial_count '
                      'FROM stat_card_facts WHERE digest=? AND player=?', (digest, player))
    data = encode_view(rows)
    if data:
        db.execute('INSERT INTO stat_view_card_sets VALUES(?,?,?,?) '
                   'ON CONFLICT(digest,player) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                   (digest, player, data, float(now or time.time())))
    else:
        db.execute('DELETE FROM stat_view_card_sets WHERE digest=? AND player=?', (digest, player))


def refresh_digest(db, digest, now=None):
    """一条录像入库后刷新它涉及的所有视角。"""
    for player in [r[0] for r in db.execute('SELECT DISTINCT player FROM stat_card_facts WHERE digest=?', (digest,))]:
        refresh_view(db, digest, player, now)


def load_builds(db, builds):
    placeholders = ','.join('?' for _ in builds)
    for build, data in db.execute('SELECT build,data FROM stat_build_card_sets WHERE build IN (%s)' % placeholders, builds):
        for partition, code, copies in decode_build(data):
            yield build, partition, code, copies


def load_views(db, keys):
    placeholders = ','.join('(?,?)' for _ in keys)
    for digest, player, data in db.execute(
            'SELECT digest,player,data FROM stat_view_card_sets WHERE (digest,player) IN (%s)' % placeholders,
            [value for key in keys for value in key]):
        for partition, code, drawn, used, used_after_draw, initial in decode_view(data):
            yield digest, player, partition, code, drawn, used, used_after_draw, initial
