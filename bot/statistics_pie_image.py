# -*- coding: utf-8 -*-
"""Website pie image with cache + metadata sidecar; QQ reuses both."""
from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from contextlib import closing
from pathlib import Path

_LOCK = threading.Lock()
RENDER_VERSION = 9


def _cache_path(database) -> Path:
    return Path(database).parent / 'cache' / 'deck-pie' / 'latest.png'


def _meta_path(database) -> Path:
    return Path(database).parent / 'cache' / 'deck-pie' / 'latest.json'


def _record_from_roster(data) -> dict:
    single = data.get('deck_single')
    if not single or not single.get('total'):
        raise ValueError('deck_single unavailable')
    return dict(single=single, pie=data.get('deck_pie'),
                covered=int(data.get('covered_players') or 0),
                current=int(data.get('current_players') or 0))


def _key(record: dict) -> str:
    raw = json.dumps(dict(v=RENDER_VERSION, single=record.get('single') or {}),
                     ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def _load(path: Path, key: str):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    prefix = key.encode('ascii')
    if len(data) <= len(prefix) + 1 or data[:len(prefix)] != prefix:
        return None
    blob = data[len(prefix) + 1:]
    return blob if blob.startswith(b'\x89PNG\r\n\x1a\n') else None


def _load_any(path: Path, max_age: float = 600.0):
    try:
        if not path.is_file() or time.time() - path.stat().st_mtime > max_age:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) <= 25:
        return None
    blob = data[25:]
    return blob if blob.startswith(b'\x89PNG\r\n\x1a\n') else None


def _store(path: Path, key: str, blob: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(key.encode('ascii') + b'\n' + blob)
        temporary.replace(path)
    except OSError:
        pass


def _write_meta(database, data) -> None:
    try:
        meta = dict(covered=int(data.get('covered_players') or 0),
                    current=int(data.get('current_players') or 0),
                    unidentified=int(data.get('unidentified_players') or 0),
                    items=[dict(name=str(row.get('name')), count=int(row.get('players') or 0))
                           for row in data.get('deck_distribution') or []
                           if int(row.get('players') or 0) > 0])
        path = _meta_path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def _read_meta(database):
    try:
        return json.loads(_meta_path(database).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _render(record: dict, database) -> bytes:
    from .top100_pie import _render_single_payload

    blob, _ = _render_single_payload(record)
    _store(_cache_path(database), _key(record), blob)
    return blob


def render_from_roster(database, data) -> bytes:
    record = _record_from_roster(data)
    _write_meta(database, data)
    path = _cache_path(database)
    key = _key(record)
    with _LOCK:
        cached = _load(path, key)
        return cached if cached is not None else _render(record, database)


def render(database, replay_root, now) -> bytes:
    import sqlite3
    from . import statistics_dashboard

    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        data = statistics_dashboard.query(db, Path(replay_root), float(now))
    return render_from_roster(database, data)


def _payload_from_meta_or_data(database, data):
    if data is not None:
        items = [dict(name=str(row.get('name')), count=int(row.get('players') or 0))
                 for row in data.get('deck_distribution') or [] if int(row.get('players') or 0) > 0]
        items.sort(key=lambda row: (-row['count'], row['name']))
        return dict(items=items, pie=data.get('deck_pie'), single=data.get('deck_single'),
                    covered=int(data.get('covered_players') or 0),
                    current=int(data.get('current_players') or 0),
                    unidentified=int(data.get('unidentified_players') or 0))
    meta = _read_meta(database) or {}
    items = sorted((dict(name=str(row.get('name')), count=int(row.get('count') or 0))
                    for row in meta.get('items') or []),
                   key=lambda row: (-row['count'], row['name']))
    return dict(items=items, pie=None, single=None,
                covered=int(meta.get('covered') or 0),
                current=int(meta.get('current') or 0),
                unidentified=int(meta.get('unidentified') or 0))


def _parts(blob: bytes):
    from PIL import Image, ImageChops

    image = Image.open(io.BytesIO(blob)).convert('RGB')
    width, height = image.size
    middle = height // 2

    def content(part, target_w, target_h=None, top_skip=0):
        part = part.crop((0, top_skip, part.width, part.height))
        diff = ImageChops.difference(part, Image.new('RGB', part.size, 'white'))
        bbox = diff.getbbox()
        if not bbox:
            return part
        margin = 24
        cropped = part.crop((max(0, bbox[0] - margin), max(0, bbox[1] - margin),
                             min(part.width, bbox[2] + margin),
                             min(part.height, bbox[3] + margin)))
        scale = target_w / cropped.width
        if target_h and cropped.height * scale > target_h:
            scale = target_h / cropped.height
        if abs(scale - 1.0) > 0.01:
            cropped = cropped.resize((max(1, int(cropped.width * scale)),
                                      max(1, int(cropped.height * scale))), Image.LANCZOS)
        return cropped

    pie = content(image.crop((0, 0, width, middle)), 1600, 1600, top_skip=96)
    canvas = Image.new('RGB', (1600, 1600), 'white')
    canvas.paste(pie, ((1600 - pie.width) // 2, (1600 - pie.height) // 2))
    pie_buffer = io.BytesIO(); canvas.save(pie_buffer, format='PNG', optimize=True)

    legend = content(image.crop((0, middle, width, height)), 1600)
    legend_buffer = io.BytesIO(); legend.save(legend_buffer, format='PNG', optimize=True)
    return {'pie': pie_buffer.getvalue(), 'legend': legend_buffer.getvalue()}


def render_parts(database, replay_root, now, roster_data=None):
    from . import statistics_dashboard

    if roster_data is not None:
        data = roster_data
        blob = render_from_roster(database, data)
    else:
        cached = _load_any(_cache_path(database))
        if cached is not None:
            blob = cached
            data = None
        else:
            import sqlite3
            uri = Path(database).resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                data = statistics_dashboard.query(db, Path(replay_root), float(now))
            blob = render_from_roster(database, data)
    payload = _payload_from_meta_or_data(database, data)
    return _parts(blob), payload
