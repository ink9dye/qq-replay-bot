"""Query-time statistics quality filter.

Excludes, for every statistics read:

* every game of a reliably associated match when any game's result is unknown;
* a game whose result is unknown and cannot be tied to a reliable match;
* a game without a reliable positive game number.

The filter is opt-in per process (``enable``). ``ygo-web`` enables it from
config; tests and other roles keep the old behaviour until they opt in.
The computed set is cached and invalidated by a cheap row/outcome stamp, so
late-ingested evidence is picked up without restarting the service.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

_ENABLED = False
_LOCK = threading.Lock()
_CACHE = {'key': None, 'at': 0.0, 'excluded': frozenset(), 'stats': {}}
_MAX_AGE = 600.0
_KNOWN = ('win', 'loss', 'draw')


def enable(value=True):
    global _ENABLED
    _ENABLED = bool(value)


def enabled():
    return _ENABLED


def _database_path(db):
    try:
        row = db.execute('PRAGMA database_list').fetchone()
    except sqlite3.Error:
        return ''
    return str(row[2] or '') if row else ''


def _stamp(db):
    """Cheap change detector: inserts, deletes and known/unknown flips."""
    try:
        row = db.execute(
            "SELECT (SELECT COUNT(*) FROM stat_views),"
            " (SELECT COALESCE(MAX(rowid),0) FROM stat_views),"
            " (SELECT COALESCE(SUM(CASE WHEN outcome IN ('win','loss','draw') THEN 1 ELSE 0 END),0)"
            "  FROM stat_views),"
            " (SELECT COUNT(*) FROM stat_games),"
            " (SELECT COALESCE(MAX(rowid),0) FROM stat_games),"
            " (SELECT COUNT(*) FROM stat_evidence),"
            " (SELECT COALESCE(MAX(rowid),0) FROM stat_evidence),"
            " (SELECT COUNT(*) FROM stat_files),"
            " (SELECT COALESCE(MAX(rowid),0) FROM stat_files),"
            " (SELECT COUNT(*) FROM replay_sessions),"
            " (SELECT COALESCE(MAX(rowid),0) FROM replay_sessions)").fetchone()
    except sqlite3.Error:
        return None
    key = [_database_path(db), tuple(row) if row else ()]
    directory = Path(key[0]).parent if key[0] else None
    if directory:
        for name in ('watch-upload-sessions.sqlite3', 'first-game-confirmations.sqlite3'):
            try:
                key.append(os.stat(directory / name).st_mtime_ns)
            except OSError:
                key.append(0)
    return tuple(key)


def compute(db):
    """Return (excluded digests, counters) for the current database state."""
    if db.row_factory is None:
        db.row_factory = sqlite3.Row
    views = db.execute('SELECT digest,player,outcome FROM stat_views').fetchall()
    by_digest = {}
    for row in views:
        by_digest.setdefault(row['digest'], []).append(row['outcome'])
    digests = sorted(by_digest)
    from .statistics_match_identity import identity_metadata
    meta = identity_metadata(db, digests)
    known = {digest: item for digest, item in meta.items() if item and item[1] > 0}
    unknown = {digest for digest, outcomes in by_digest.items()
               if all(outcome not in _KNOWN for outcome in outcomes)}
    sessions = {}
    for digest in digests:
        item = known.get(digest)
        if item and isinstance(item[0], str) and item[0]:
            sessions.setdefault(item[0], set()).add(digest)
    bad_sessions = {sid for sid, members in sessions.items() if members & unknown}
    match_digests = {digest for sid in bad_sessions for digest in sessions[sid]}
    game_digests = set(digests) - set(known)
    standalone = unknown - match_digests - game_digests
    excluded = frozenset(match_digests | game_digests | standalone)
    counters = dict(
        views=len(views), digests=len(digests), unknown_digests=len(unknown),
        known_identity_digests=len(known), unknown_game_number_digests=len(game_digests),
        bad_sessions=len(bad_sessions), match_digests=len(match_digests),
        standalone_unknown_digests=len(standalone), excluded_digests=len(excluded))
    return excluded, counters


def excluded_digests(db):
    """Currently excluded digest set; cached until a cheap stamp changes."""
    if not _ENABLED:
        return frozenset()
    key = _stamp(db)
    age = time.monotonic() - _CACHE['at']
    if key is not None and key == _CACHE['key'] and age < _MAX_AGE:
        return _CACHE['excluded']
    with _LOCK:
        if (key is not None and key == _CACHE['key']
                and time.monotonic() - _CACHE['at'] < _MAX_AGE):
            return _CACHE['excluded']
        try:
            excluded, counters = compute(db)
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            # Fail open: a transient evidence/lock problem must not blank all
            # statistics. Do not cache the failure; the next request retries.
            return frozenset()
        _CACHE.update(key=key, at=time.monotonic(), excluded=excluded, stats=counters)
        return excluded


def stats():
    """Counters from the last computation (diagnostics only)."""
    return dict(_CACHE['stats'])
