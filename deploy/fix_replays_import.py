#!/usr/bin/env python3
"""Fix GBK/UTF-8 zip entry names and import replays safely.

1. removes mojibake directories created by a previous bad unzip
2. extracts /tmp/replays.zip with correctly decoded names
3. reports counts
"""
import shutil
import zipfile
from pathlib import Path

REPLAYS = Path('/opt/ygobot/data/replays')
TMP = Path('/tmp/replays-import')
ZIP = Path('/tmp/replays.zip')
BAD_CHARS = set('щ╗СцтЯ╝╕╨╡Ё┐└┴┬├─┼')


def fix_name(name, utf8_flag):
    if utf8_flag:
        return name
    raw = name.encode('cp437', 'replace')
    for encoding in ('utf-8', 'gbk'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name


def main():
    removed = 0
    if REPLAYS.is_dir():
        for path in REPLAYS.iterdir():
            if path.is_dir() and any(ch in path.name for ch in BAD_CHARS):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
    print('removed mojibake dirs:', removed)

    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True)

    extracted = 0
    skipped = 0
    with zipfile.ZipFile(ZIP) as archive:
        for info in archive.infolist():
            name = fix_name(info.filename, bool(info.flag_bits & 0x800))
            target = TMP / name
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(target, 'wb') as output:
                    shutil.copyfileobj(source, output)
                extracted += 1
            except OSError as exc:
                skipped += 1
                if skipped <= 3:
                    print('skip:', name[:80], exc)
    print('extracted:', extracted, 'skipped:', skipped)


if __name__ == '__main__':
    main()
