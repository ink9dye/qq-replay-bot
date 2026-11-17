"""Bounded metadata-only QQ diagnostics. Never accepts message or API payloads."""
import contextvars
import asyncio
import functools
import inspect
from contextlib import contextmanager, asynccontextmanager
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
active_diagnostics = contextvars.ContextVar('qq_active_diagnostics', default=None)
command_operation = contextvars.ContextVar('qq_command_operation', default='unknown')
parent_span = contextvars.ContextVar('qq_parent_span', default=None)
request_timing = contextvars.ContextVar('qq_request_timing', default=None)
reply_role = contextvars.ContextVar('qq_reply_role', default='foreground')
_NUMBER_FIELDS = {'elapsed_ms', 'retcode', 'failures', 'retry_seconds', 'pending', 'dispatching', 'queued', 'message_count', 'command_count', 'reply_index', 'replies_ok', 'replies_failed', 'first_feedback_ms', 'last_reply_ms', 'last_result_ms'}
_BOOL_FIELDS = {'success', 'delivery_uncertain', 'group_enabled', 'unmentioned_enabled', 'actual_send', 'background'}
_WORD_FIELDS = {'action', 'operation', 'reason', 'state', 'kind', 'error_type', 'phase', 'outcome', 'role'}


class Diagnostics:
    def __init__(self, data_dir, *, max_bytes=8 * 1024 * 1024, clock=time.time):
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
            elif key in ('span', 'parent_span') and isinstance(value, str) and re.fullmatch('[0-9a-f]{12}', value):
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
                    if path.exists() and now - path.stat().st_mtime > 90 * 86400:
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


def _phase_record(event, **fields):
    sink = active_diagnostics.get()
    if sink is not None:
        try:
            sink.record(event, operation=command_operation.get(), **fields)
        except Exception:
            pass  # Observation must not affect results, cancellation or login state.


@contextmanager
def phase(name):
    """Nested wall-clock span; child spans are included, never add their durations."""
    if active_diagnostics.get() is None:
        yield
        return
    span = uuid.uuid4().hex[:12]
    fields = dict(phase=name, span=span, parent_span=parent_span.get())
    token = parent_span.set(span)
    started = time.monotonic()
    outcome = 'ok'
    _phase_record('phase_start', **fields)
    try:
        yield
    except BaseException as exc:
        outcome = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error'
        raise
    finally:
        _phase_record('phase_complete', **fields, outcome=outcome,
                      elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        parent_span.reset(token)


def observe(name):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                timing = request_timing.get() if name == 'background_job' else None
                if timing is not None:
                    timing.jobs += 1  # Register before create_task, not when coroutine starts.
                    timing.background = True
                async def run():
                    token = reply_role.set('result') if timing is not None else None
                    outcome = 'ok'
                    try:
                        with phase(name):
                            return await function(*args, **kwargs)
                    except BaseException as exc:
                        outcome = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error'
                        raise
                    finally:
                        if token is not None:
                            reply_role.reset(token)
                        if timing is not None:
                            timing.jobs -= 1
                            timing.outcome = outcome if outcome != 'ok' else timing.outcome
                            timing.complete()
                return run()
        else:
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                with phase(name):
                    return function(*args, **kwargs)
        return wrapped
    return decorate


class RequestTiming:
    """One accepted command, local receive to last observed send; event-loop owned.

    No message content or destination. API acceptance is not user receipt.
    A cancelled task before its coroutine starts has no completion record (unknown).
    """
    def __init__(self, received=None):
        self.received = received if received is not None else time.monotonic()
        self.accepted = self.foreground_done = self.background = self.done = False
        self.jobs = self.ok = self.failed = self.attempts = 0
        self.first = self.last = self.result = None
        self.outcome = 'ok'

    def sent(self, success, actual):
        elapsed = round((time.monotonic()-self.received)*1000, 3)
        self.attempts += 1
        role = reply_role.get()
        _phase_record('reply_milestone', role=role, elapsed_ms=elapsed,
                      success=bool(success), actual_send=actual, reply_index=self.attempts)
        if not actual:
            # Deduplication may report True without sending: never invent feedback.
            if not success:
                self.failed += 1
            return
        if success:
            self.ok += 1
            self.last = elapsed
            if role == 'result':
                self.result = elapsed
            if self.first is None:
                self.first = elapsed
                _phase_record('phase_complete', phase='first_feedback', elapsed_ms=elapsed, outcome='ok')
        else:
            self.failed += 1

    def complete(self):
        if not self.accepted or not self.foreground_done or self.jobs or self.done:
            return
        self.done = True
        outcome = self.outcome
        if outcome == 'ok':
            outcome = ('send_failure' if self.failed else 'no_result' if
                       (self.result is None if self.background else self.last is None) else 'reply_confirmed')
        fields = dict(elapsed_ms=round((time.monotonic()-self.received)*1000, 3),
                      background=self.background, outcome=outcome, replies_ok=self.ok,
                      replies_failed=self.failed)
        for key,value in (('first_feedback_ms',self.first),('last_reply_ms',self.last),('last_result_ms',self.result)):
            if value is not None:
                fields[key] = value
        _phase_record('command_lifecycle_complete', **fields)


@asynccontextmanager
async def observed_lock(lock):
    with phase('statistics_wait'):
        await lock.acquire()
    try:
        with phase('statistics_hold'):
            yield
    finally:
        lock.release()


async def observed_thread(function, /, *args, **kwargs):
    """Keep asyncio.to_thread semantics; record executor wait and actual work separately.

    Timeout/cancellation of the awaiter does not stop its thread. The worker span
    ends only when the function really exits, even after a timeout reply.
    """
    submitted = time.monotonic()
    def run():
        _phase_record('phase_complete', phase='executor_wait', span=uuid.uuid4().hex[:12],
                      parent_span=parent_span.get(), outcome='ok',
                      elapsed_ms=round((time.monotonic() - submitted) * 1000, 3))
        with phase('worker'):
            return function(*args, **kwargs)
    return await asyncio.to_thread(run)
