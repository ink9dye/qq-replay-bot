#!/usr/bin/env python3
"""Normalize Windows paths in the bot config files to this Linux deployment."""
import json
from pathlib import Path

ROOT = Path('/opt/ygobot')

TARGETS = ('config.json', 'data/watch-config.json')

for name in TARGETS:
    path = ROOT / name
    if not path.exists():
        print('skip', name)
        continue
    data = json.loads(path.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, str):
            value = value.replace('\\', '/')
            value = value.replace('D:/pythons/qq-replay-bot', str(ROOT))
            value = value.replace('D:/MyCardLibrary/ygopro', str(ROOT / 'ygopro'))
            return value
        if isinstance(value, list):
            return [fix(item) for item in value]
        if isinstance(value, dict):
            return {key: fix(item) for key, item in value.items()}
        return value

    fixed = fix(data)
    path.write_text(json.dumps(fixed, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('fixed', name)
