"""Request-local, observational timing only; no cache or query policy changes."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
import threading
from functools import wraps

_current = ContextVar('web_perf_metrics', default=None)


@contextmanager
def request_metrics(source='request'):
    values = {'metric_source': source}
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


def timed(name):
    """Fixed-name phase timing; results and exceptions pass through unchanged."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if _current.get() is None:return function(*args, **kwargs)
            with measure(name):return function(*args, **kwargs)
        return wrapped
    return decorate


def add_metric(name,value):
    """Add fixed-name numeric counters without storing query data."""
    values=_current.get()
    if values is not None:values[name]=values.get(name,0)+value


def task_label(value):
    value = str(value).rsplit('/', 1)[-1]
    return value if value in {'roster','recordings','matrix','card-report','card-table',
        'groups','deck-candidates','cards-extra','deck-detail','build','side-analysis',
        'side-recent','usage','winrate','flow'} else 'other'


class ObservedQueryLock:
    """RLock with bounded metadata only; acquire/release policy is unchanged."""
    def __init__(self):
        self._lock = threading.RLock()
        self._meta = threading.Lock()
        self._owner = None
        self._sequence = 0

    def snapshot(self):
        with self._meta:
            if self._owner is None:return None
            owner = self._owner
            return dict(task=owner['task'],source=owner['source'],sequence=owner['sequence'],
                        held_ms=round((perf_counter()-owner['started'])*1000,3))

    def acquire(self, blocking=True, timeout=-1, task='other'):
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            with self._meta:
                if self._owner is None:
                    self._sequence += 1
                    values = _current.get() or {}
                    self._owner = dict(task=task_label(task),source=values.get('metric_source','unknown'),
                        sequence=self._sequence,started=perf_counter(),depth=1)
                else:self._owner['depth'] += 1
        return acquired

    def release(self):
        with self._meta:
            # Release first: an invalid owner must raise without changing metadata.
            self._lock.release()
            self._owner['depth'] -= 1
            if self._owner['depth'] == 0:
                values = _current.get()
                if values is not None:
                    values['lock_hold_ms'] = values.get('lock_hold_ms',0) + round(
                        (perf_counter()-self._owner['started'])*1000,3)
                    values['lock_task'] = self._owner['task']
                self._owner = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self,*args):self.release()


def acquire_query_lock(lock, timeout, task='other'):
    values = _current.get()
    if isinstance(lock,ObservedQueryLock) and values is not None:
        values['lock_owner_before'] = lock.snapshot()
    with measure('lock_wait_ms'):
        acquired = lock.acquire(timeout=timeout,task=task) if isinstance(lock,ObservedQueryLock) else lock.acquire(timeout=timeout)
    values = _current.get()
    if values is not None:
        values['lock_observed'] = True
        if not acquired:
            values['lock_timeout'] = True
            if isinstance(lock,ObservedQueryLock):values['lock_owner_timeout'] = lock.snapshot()
    return acquired
