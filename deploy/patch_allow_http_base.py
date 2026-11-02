#!/usr/bin/env python3
"""Allow http:// public base URLs in bot/service.py (direct IP setup).

Backs up the original file next to it before patching.
"""
from pathlib import Path

TARGET = Path('/opt/ygobot/bot/service.py')
OLD = 'if not text.startswith("https://"):'
NEW = 'if not text.startswith(("https://", "http://")):'


def main():
    text = TARGET.read_text(encoding='utf-8')
    if NEW in text:
        print('already patched')
        return
    if OLD not in text:
        raise SystemExit('pattern not found, file layout changed')
    backup = TARGET.with_name(TARGET.name + '.bak-allow-http')
    backup.write_text(text, encoding='utf-8')
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding='utf-8')
    print('patched', TARGET, 'backup', backup)


if __name__ == '__main__':
    main()
