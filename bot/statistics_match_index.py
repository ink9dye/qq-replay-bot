"""小局身份索引：入库时算一次，查询只读表。

身份判定（哪一场、第几局、是否从开局录）原本每次查询都要把 stat_evidence 里
三万多条 payload_json 解一遍，再做跨会话合并；2C1.7G 的机器上单次十几秒，
I/O 紧张时接近三分钟。本表只存推导结果，规则仍在 statistics_match_identity，
模块本身不做任何新的判定。
"""
import json
import sqlite3
import time
from collections import deque

SCHEMA = '''CREATE TABLE IF NOT EXISTS stat_match_identity (
 digest TEXT PRIMARY KEY, session_id TEXT, game_no INTEGER NOT NULL DEFAULT 0,
 captured_from_start INTEGER NOT NULL DEFAULT 0, parent TEXT,
 resolved INTEGER NOT NULL DEFAULT 0, computed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS stat_match_identity_parent ON stat_match_identity(parent);'''

# 过期清理先删 stat_games；触发器不依赖连接的 foreign_keys 开关。
TRIGGER = '''CREATE TRIGGER IF NOT EXISTS stat_match_identity_removed AFTER DELETE ON stat_games
BEGIN
 DELETE FROM stat_match_identity WHERE digest=OLD.digest;
END;'''

# 证据变了索引就作废：任何绕过 import_replay 的写入也不会读到旧身份。
# import_replay 在同一事务里随后重算，正常入库不会留下未索引状态。
EVIDENCE_TRIGGER = '''CREATE TRIGGER IF NOT EXISTS stat_match_identity_stale
AFTER INSERT ON stat_evidence
BEGIN
 DELETE FROM stat_match_identity WHERE digest=NEW.digest;
END;
CREATE TRIGGER IF NOT EXISTS stat_match_identity_stale_update
AFTER UPDATE ON stat_evidence
BEGIN
 DELETE FROM stat_match_identity WHERE digest=NEW.digest;
END;
CREATE TRIGGER IF NOT EXISTS stat_match_identity_stale_delete
AFTER DELETE ON stat_evidence
BEGIN
 DELETE FROM stat_match_identity WHERE digest=OLD.digest;
END;'''


def initialize(db):
    db.executescript(SCHEMA)
    for script in (TRIGGER, EVIDENCE_TRIGGER):
        try:
            db.executescript(script)
        except sqlite3.Error:
            # 没有 stat_games/stat_evidence 的只读或测试库：索引表可用，清理交给调用方。
            pass


def lookup(db, digests):
    """(已解析元组, 已建索引的 digest)。元组与 capture_metadata 完全同形。"""
    found, indexed = {}, set()
    values = list(dict.fromkeys(digests))
    try:
        for start in range(0, len(values), 400):
            chunk = values[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            for digest, session, number, full, resolved in db.execute(
                    'SELECT digest,session_id,game_no,captured_from_start,resolved '
                    'FROM stat_match_identity WHERE digest IN (%s)' % marks, chunk):
                indexed.add(digest)
                if resolved:
                    found[digest] = (session, number, bool(full))
    except sqlite3.Error:
        # 老库还没建表时按未索引处理，查询端会回退旧路径。
        return {}, set()
    return found, indexed


def indexed(db, digest):
    """该 digest 是否已经算过（无论有没有可靠身份）。入库时用来决定要不要重算。"""
    try:
        return db.execute('SELECT 1 FROM stat_match_identity WHERE digest=?',
                          (digest,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _parent_map(db, digests):
    """每个 digest 唯一的前一局；多处指向不同前局时留空，与合并规则一致。"""
    result = {}
    values = list(dict.fromkeys(digests))
    for start in range(0, len(values), 400):
        chunk = values[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        found = {}
        try:
            rows = db.execute("SELECT digest,json_extract(payload_json,'$.capture') "
                              'FROM stat_evidence WHERE digest IN (%s)' % marks, chunk)
        except sqlite3.Error:
            return result
        for digest, raw in rows:
            try:
                info = json.loads(raw) if raw else None
            except ValueError:
                continue
            previous = info.get('previous_game') if isinstance(info, dict) else None
            digest_ref = previous.get('digest') if isinstance(previous, dict) else None
            if isinstance(digest_ref, str) and len(digest_ref) == 64:
                found.setdefault(digest, set()).add(digest_ref)
        result.update({d: next(iter(s)) for d, s in found.items() if len(s) == 1})
    return result


def _write(db, digest, value, parent, now):
    if value:
        session, number, full = value
        db.execute('INSERT INTO stat_match_identity VALUES(?,?,?,?,?,?,?) '
                   'ON CONFLICT(digest) DO UPDATE SET session_id=excluded.session_id,'
                   'game_no=excluded.game_no,captured_from_start=excluded.captured_from_start,'
                   'parent=excluded.parent,resolved=excluded.resolved,computed_at=excluded.computed_at',
                   (digest, session, int(number or 0), int(bool(full)), parent, 1, now))
    else:
        # 明确记下“算过但没有可靠身份”，避免每次查询都回退到证据表。
        db.execute('INSERT INTO stat_match_identity VALUES(?,?,?,?,?,?,?) '
                   'ON CONFLICT(digest) DO UPDATE SET session_id=NULL,game_no=0,'
                   'captured_from_start=0,parent=excluded.parent,resolved=0,'
                   'computed_at=excluded.computed_at',
                   (digest, None, 0, 0, parent, 0, now))


def rebuild(db, values):
    """values: {digest: 元组}，缺键表示该 digest 没有可靠身份；批量重写。"""
    parents = _parent_map(db, values)
    now = time.time()
    for digest, value in values.items():
        _write(db, digest, value, parents.get(digest), now)


def refresh(db, digests):
    """按当前证据重算这些 digest，并把结果传给引用它们的后续小局。

    只在入库与回填时调用；查询路径不写库。
    """
    from .statistics_match_identity import _read_capture_metadata
    queue = deque((digest, 0) for digest in dict.fromkeys(digests) if digest)
    seen = set()
    while queue:
        digest, level = queue.popleft()
        if digest in seen:
            continue
        seen.add(digest)
        value = _read_capture_metadata(db, {digest}).get(digest)
        _write(db, digest, value, _parent_map(db, [digest]).get(digest), time.time())
        if level >= 20:
            continue
        children = [row[0] for row in db.execute(
            'SELECT digest FROM stat_match_identity WHERE parent=?', (digest,))]
        queue.extend((child, level + 1) for child in children if child not in seen)
