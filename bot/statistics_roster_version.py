"""Shared, cheap invalidation inputs for ranking and deck naming snapshots."""
from pathlib import Path


def snapshot_version(database):
    database = Path(database)
    paths = [database, Path(str(database) + '-wal')]
    for name in ('first-game-confirmations.sqlite3', 'watch-upload-sessions.sqlite3',
                 'config.json', 'series-naming.json', 'card-series-reference.json',
                 'card-equivalence.json'):
        path = database.parent / name
        paths.append(path)
        if path.suffix == '.sqlite3':
            paths.append(Path(str(path) + '-wal'))
    version = []
    for path in paths:
        try:
            stat = path.stat()
            version.append((path.name, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except FileNotFoundError:
            version.append((path.name, None, None, None))
    return tuple(version)
