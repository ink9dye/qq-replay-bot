# -*- coding: utf-8 -*-
"""离线 Side 推断缓存：key=构筑指纹，value=inferred_side。"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _path(data_dir):
    return Path(data_dir) / 'cache' / 'side-inference.json'


def load(data_dir):
    path = _path(data_dir)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def get(data_dir, build):
    return (load(data_dir).get(str(build)) or None)


def put(data_dir, build, inferred):
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load(data_dir)
    data[str(build)] = inferred
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding='utf-8')
    os.replace(tmp, path)
