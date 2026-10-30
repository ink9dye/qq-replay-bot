"""Bounded metadata-only QQ diagnostics. Never accepts message or API payloads."""
import contextvars
import json
import math
import os
import re
import threading
import time
import traceback
import uuid
from pathlib import Path

trace_id = contextvars.ContextVar('qq_diagnostic_trace', default=None)
_NUMBER_FIELDS = {'elapsed_ms', 'retcode', 'failures', 'retry_seconds', 'pending', 'dispatching', 'queued', 'message_count', 'command_count'}
_BOOL_FIELDS = {'success', 'delivery_uncertain', 'group_enabled', 'unmentioned_enabled'}
_WORD_FIELDS = {'action', 'operation', 'reason', 'state', 'kind', 'error_type'}


class Diagnostics:
    def __init__(self, data_dir, *, max_bytes=1024 * 1024, clock=time.time):
        self.path = Path(data_dir) / 'logs' / 'qq-diagnostics.jsonl' if data_dir else None
        self.session = uuid.uuid4().hex[:12]
        self.max_bytes, self.clock = max_bytes, clock
        self.lock = threading.Lock()

    def record(self, event, **fields):
        if self.path is None or not re.fullmatch('[a-z_]{1,48}', event):
            return
        now = self.clock()
        row = dict(time=now, pid=os.getpid(), session=self.session, version=1, event=event)
        trace = trace_id.get()
        if trace and re.fullmatch('[0-9a-f]{12}', trace):
            row['trace'] = trace
        for key, value in fields.items():
            if key in _NUMBER_FIELDS and type(value) in (int, float) and math.isfinite(value):
                row[key] = value
            elif key in _BOOL_FIELDS and type(value) is bool:
                row[key] = value
            elif key in _WORD_FIELDS and isinstance(value, str) and re.fullmatch('[A-Za-z_]{1,64}', value):
                row[key] = value
            elif key == 'error_site' and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.]+:[A-Za-z0-9_<>]+:\d+', value):
                row[key] = value
        encoded = json.dumps(row, ensure_ascii=True) + '\n'
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                older = self.path.with_name(self.path.name + '.2')
                previous = self.path.with_name(self.path.name + '.1')
                # These three exact owned files only; no directory traversal or recursive cleanup.
                for path in (self.path, previous, older):
                    if path.exists() and now - path.stat().st_mtime > 7 * 86400:
                        path.unlink()
                if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
                    older.unlink(missing_ok=True)
                    if previous.exists():
                        previous.replace(older)
                    self.path.replace(previous)
                with self.path.open('a', encoding='utf-8') as handle:
                    handle.write(encoded)
        except OSError:
            # Diagnostics failure must never stop command processing or cause a login retry.
            pass


def exception_fields(exc):
    result = dict(error_type=type(exc).__name__)
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        last = frames[-1]
        result['error_site'] = f'{Path(last.filename).name}:{last.name}:{last.lineno}'
    return result
