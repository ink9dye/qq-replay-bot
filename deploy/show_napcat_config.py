#!/usr/bin/env python3
"""Print NapCat onebot11 config structure with token values masked."""
import glob
import json
from pathlib import Path


def mask(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if 'token' in key.lower() and isinstance(value, str):
                out[key] = '<masked len=%d>' % len(value)
            else:
                out[key] = mask(value)
        return out
    if isinstance(node, list):
        return [mask(item) for item in node]
    return node


for path in glob.glob('/opt/napcat/config/onebot11_*.json'):
    print('==', path)
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    print(json.dumps(mask(data), ensure_ascii=False, indent=2))
