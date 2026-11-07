# -*- coding: utf-8 -*-
"""Website-owned deck pie image: render once from deck_single, cache on disk, QQ reuses."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _cache_path(database) -> Path:
    return Path(database).parent / 'cache' / 'deck-pie' / 'latest.png'


def _key(record) -> str:
    raw = json.dumps(record.get('single') or {}, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def _load(path: Path, key: str):
    try:
        record = path.read_bytes()
    except OSError:
        return None
    prefix = key.encode('ascii')
    if len(record) <= len(prefix) + 1 or record[:len(prefix)] != prefix:
        return None
    blob = record[len(prefix) + 1:]
    return blob if blob.startswith(b'\x89PNG\r\n\x1a\n') else None


def _store(path: Path, key: str, blob: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(key.encode('ascii') + b'\n' + blob)
        temporary.replace(path)
    except OSError:
        pass


def render(database, replay_root, now):
    """Return (png_bytes, payload) for the current deck_single; cache by payload hash."""
    import sqlite3
    from contextlib import closing

    from . import statistics_dashboard

    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        data = statistics_dashboard.query(db, Path(replay_root), float(now))
    single = data.get('deck_single')
    if not single or not single.get('total'):
        raise ValueError('deck_single unavailable')
    items = [dict(name=str(row.get('name')), count=int(row.get('players') or 0))
             for row in data.get('deck_distribution') or [] if int(row.get('players') or 0) > 0]
    items.sort(key=lambda row: (-row['count'], row['name']))
    record = dict(single=single, covered=int(data.get('covered_players') or 0),
                  current=int(data.get('current_players') or 0))
    key = _key(record)
    path = _cache_path(database)
    with _LOCK:
        blob = _load(path, key)
        if blob is None:
            from .top100_pie import _render_single_payload

            blob, _ = _render_single_payload(record)
            _store(path, key, blob)
    payload = dict(items=items, pie=data.get('deck_pie'), single=single,
                   covered=record['covered'], current=record['current'],
                   unidentified=int(data.get('unidentified_players') or 0))
    return blob, payload
