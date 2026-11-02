#!/usr/bin/env python3
"""Convert literal backslashes in replay filenames into real directories.

Windows Compress-Archive stored paths like ``player\\room\\file.yrp`` inside the
zip; extracting on Linux produced one filename containing backslashes. The bot
database expects the directory structure, so flattening must be undone.
"""
from pathlib import Path

ROOT = Path('/opt/ygobot/data/replays')


def main():
    moved = 0
    skipped = 0
    for path in list(ROOT.rglob('*')):
        if not path.is_file() or '\\' not in path.name:
            continue
        parts = [part for part in path.name.split('\\') if part]
        if not parts:
            continue
        target = path.parent.joinpath(*parts)
        if target.exists():
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
        moved += 1
    print('moved:', moved, 'skipped(existing):', skipped)


if __name__ == '__main__':
    main()
