#!/usr/bin/env python3
"""Build data/cache/roster.json out of process (2 GB memory plan, 2026-11-07).

The web process normally builds the roster inline: 30-90 s of CPU, a
250-300 MB intermediate, and `_query_compute_lock` held for the whole build
(every other heavy query waits 8 s and then gets a 503). This one-shot runs
the same code in its own process and writes the exact response to a file;
the patched portal serves the file instead of building it.

Run by ygobot-roster-precompute.timer every 4 minutes.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path


def build(root: Path, dry_run: bool) -> int:
    data = root / 'data'
    db = data / 'bot.sqlite3'
    replays = data / 'replays'
    out = data / 'cache' / 'roster.json'
    if not db.is_file():
        print(f'roster_precompute: missing database {db}', file=sys.stderr)
        return 2

    sys.path.insert(0, str(root))
    from bot.statistics_portal import StatisticsPortal, owner_secret

    started = time.monotonic()
    portal = StatisticsPortal(
        db,
        replays,
        time.time,
        owner_secret(data / 'statistics-owner.key'),
        background=False,
    )
    status, kind, body, headers = portal._roster_reply(force=True)
    elapsed = time.monotonic() - started
    if status != 200 or not isinstance(body, (bytes, bytearray)) or not body:
        print(f'roster_precompute: build failed status={status} elapsed={elapsed:.1f}s',
              file=sys.stderr)
        return 1

    saved = time.time()
    text = body.decode('utf8')
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        payload['_query_refresh'] = {
            'state': 'precomputed',
            'saved_at': saved,
            'refreshing': False,
        }
        text = json.dumps(payload, ensure_ascii=False)

    item = {
        'saved': saved,
        'status': status,
        'kind': kind,
        'body': text,
        'headers': dict(headers or {}),
        'elapsed': round(elapsed, 1),
        'bytes': len(text.encode('utf8')),
    }
    if dry_run:
        print(f'roster_precompute: dry-run ok bytes={item["bytes"]} elapsed={elapsed:.1f}s')
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + '.tmp')
    tmp.write_text(json.dumps(item, ensure_ascii=False), encoding='utf8')
    os.chmod(tmp, 0o644)
    os.replace(tmp, out)
    print(f'roster_precompute: wrote {out} bytes={item["bytes"]} elapsed={elapsed:.1f}s')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='/opt/ygobot')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    return build(Path(args.root).resolve(), args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
