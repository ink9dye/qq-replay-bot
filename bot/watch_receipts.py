"""Durable, bounded provenance for received remote small-game replays."""
import json
import threading
import time
from pathlib import Path

_LOCK=threading.Lock()
MAX_RECEIPTS=9
MAX_RECEIPT_BYTES=64*1024


def read_receipts(path):
    try:
        with Path(path).open('rb') as handle:
            raw=handle.read(MAX_RECEIPT_BYTES+1)
        if len(raw)>MAX_RECEIPT_BYTES:return []
        data=json.loads(raw.decode('utf-8'))
        return [row for row in data if isinstance(row,dict) and row.get('digest')][-MAX_RECEIPTS:] if isinstance(data,list) else []
    except (OSError,ValueError):return []


def record_receipt(path, digest, worker, players, recorded_at, received_at=None):
    path=Path(path)
    with _LOCK:
        rows=read_receipts(path)
        if any(row['digest']==digest for row in rows):return
        rows.append(dict(digest=str(digest),worker=str(worker or '')[:64],players=[str(p)[:100] for p in players[:4]],
                         recorded_at=recorded_at,received_at=time.time() if received_at is None else received_at))
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(rows[-MAX_RECEIPTS:],ensure_ascii=False),encoding='utf-8')
        temporary.replace(path)
