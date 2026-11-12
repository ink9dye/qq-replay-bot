"""Request-local, observational timing only; no cache or query policy changes."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_current = ContextVar('web_perf_metrics', default=None)


@contextmanager
def request_metrics():
    values = {}
    token = _current.set(values)
    try:
        yield values
    finally:
        _current.reset(token)


@contextmanager
def measure(name):
    started = perf_counter()
    try:
        yield
    finally:
        values = _current.get()
        if values is not None:
            values[name] = values.get(name, 0) + round((perf_counter() - started) * 1000, 3)


def acquire_query_lock(lock, timeout):
    with measure('lock_wait_ms'):
        acquired = lock.acquire(timeout=timeout)
    values = _current.get()
    if values is not None:
        values['lock_observed'] = True
        if not acquired:
            values['lock_timeout'] = True
    return acquired
