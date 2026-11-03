"""Bounded local replay analysis, separate from watcher and read-only HTTP queries."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

from .replay_analysis import ROOT, VERSION, sidecar
from .replay_window import REPLAY_WINDOW_SECONDS
from .share import safe_replay_file


def candidates(database, replay_root, now):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT r.*,MIN((SELECT MIN(saved_at) FROM replays WHERE digest=r.digest)+?,COALESCE(g.expires_at,r.saved_at+?)) AS deadline '
            'FROM replays r LEFT JOIN stat_games g ON g.digest=r.digest '
            'WHERE r.saved_at+?>? ORDER BY r.recorded_at DESC',
            (REPLAY_WINDOW_SECONDS, REPLAY_WINDOW_SECONDS, REPLAY_WINDOW_SECONDS, now)).fetchall()
    grouped = {}
    for row in rows:
        if row['deadline'] <= now: continue
        path = safe_replay_file(replay_root, row['rel_path'])
        if path is not None: grouped.setdefault(row['digest'], []).append((dict(row), path))
    for copies in grouped.values():
        # An existing report, including a failure, means already attempted. Never
        # retry through another saved copy of the same replay.
        if not any(sidecar(p).exists() for _, p in copies): yield copies[0]


def process_one(database, replay_root, game_root, runner=subprocess.run, clock=time.time):
    item = next(candidates(database, replay_root, clock()), None)
    if item is None: return 'idle'
    row, path = item
    digest = row['digest']
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest: return 'identity_mismatch'
    try:
        result = runner([sys.executable, '-m', 'bot.replay_analysis', str(path),
            '--game-root', str(game_root)], cwd=ROOT, capture_output=True, timeout=40,
            env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode: raise ValueError('analysis_process_failed')
        report = json.loads(result.stdout)
        if report.get('digest') != digest or report.get('version') != VERSION:
            raise ValueError('analysis_identity_mismatch')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        report = dict(version=VERSION, digest=digest, playback='failed', players=[],
            warnings=['自动解析失败或超时；保留未知，不自动重试。'])
    # Do not resurrect a deleted source or extend the original lifetime.
    if clock() >= row['deadline'] or not path.is_file(): return 'source_gone'
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest: return 'identity_mismatch'
    with closing(sqlite3.connect(database, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        with db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM replays WHERE id=? AND digest=?', (row['id'], digest)).fetchone()
            if current is None or float(current['saved_at']) + REPLAY_WINDOW_SECONDS <= clock(): return 'source_gone'
            existing = db.execute('SELECT expires_at FROM stat_games WHERE digest=?',(digest,)).fetchone()
            if existing is not None and existing[0] <= clock(): return 'source_gone'
            target = sidecar(path)
            if target.exists(): return 'already_attempted'
            if len(report.get('players', [])) == 2:
                from .statistics_facts import import_replay
                import_replay(db, dict(current), path, report, clock())
            temp = target.with_name(target.name + '.auto-tmp')
            temp.write_text(json.dumps(report, ensure_ascii=False), encoding='utf8')
            temp.replace(target)
    return 'analyzed' if len(report.get('players', [])) == 2 else 'failed'


def _lock(handle):
    """跨进程排他锁：Windows 用 msvcrt 字节锁，POSIX 用 fcntl.lockf；拿不到返回 False。"""
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
        return True
    except OSError:
        return False


def _unlock(handle):
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.lockf(handle.fileno(), fcntl.LOCK_UN, 1)


def engine_ready(root=None):
    """本机能否真正解析录像：Windows 用 runtime/ocgcore.dll，其它平台用 runtime/ocgcore.so。"""
    base = Path(root) if root else ROOT
    return (base / 'runtime' / ('ocgcore.dll' if os.name == 'nt' else 'ocgcore.so')).is_file()


def start_worker(root):
    root = Path(root)
    cfg = json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
    data = Path(cfg.get('data_dir') or root/'data')
    replays = Path(cfg.get('replay_dir') or data/'replays')
    game_root = Path(str(cfg.get('ygopro_dir') or 'D:/MyCardLibrary/ygopro'))
    stop = threading.Event()
    if not engine_ready():
        # 没有引擎时每条录像都会解析失败，而失败 sidecar 会被当成“已尝试、永不重试”，
        # 等于把整段历史永久标死；所以宁可不启动，只留一行说明。
        print('draw-analysis 未启动：本机没有可用的解析引擎（runtime/ocgcore.dll 或 runtime/ocgcore.so）', file=sys.stderr)
        return stop
    def loop():
        # OS lock is released on process exit. It stores no replay/player facts.
        try:
            with (data/'draw-analysis.lock').open('a+b') as lock:
                lock.seek(0)
                if not lock.read(1): lock.write(b'0'); lock.flush()
                lock.seek(0)
                if not _lock(lock): return
                try:
                    while not stop.is_set():
                        try: state = process_one(data/'bot.sqlite3', replays, game_root)
                        except (OSError, ValueError, sqlite3.Error): state = 'error'
                        stop.wait(2 if state in ('analyzed', 'failed') else 30)
                finally:
                    lock.seek(0); _unlock(lock)
        except OSError: return
    thread = threading.Thread(target=loop, name='draw-analysis', daemon=True)
    thread.start()
    return stop
