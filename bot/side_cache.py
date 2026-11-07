# -*- coding: utf-8 -*-
"""离线 Side 推断缓存：key=构筑指纹，value=inferred_side。

内存与并发约束：
- `load()` 用 `json.load(文件句柄)` 直接解析，不额外保留整份 45MB 字符串；
- `commit()` 在**排他文件锁**内做 读最新 -> 只新增未存在的键 -> 流式序列化到临时文件 -> 原子替换；
- 调用方可以把已加载的快照作为 `base` 传入（附 `base_stamp`）：文件未变时不再重复解析，
  避免同一份 45MB 数据在内存里出现两份；文件变了则以文件为准重新合并；
- 已存在的键一律保留（与原缓存“已有构筑跳过推断”的契约一致），并发写入不会丢数据；
- 写前复核文件指纹，变化则重读重合并，最多 `attempts` 次；仍冲突则不写并返回 conflict。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

try:  # POSIX 排他锁；其他平台退化为无锁（只有测试会跑到）
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


def _path(data_dir):
    return Path(data_dir) / 'cache' / 'side-inference.json'


def _lock_path(data_dir):
    return Path(data_dir) / 'cache' / 'side-inference.write-lock'


def stamp(data_dir):
    """缓存文件的 (mtime_ns, size)；不存在返回 None。"""
    try:
        stat = _path(data_dir).stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def load(data_dir):
    """整份读入；用文件句柄直接 json.load，避免再复制一份 45MB 字符串。"""
    path = _path(data_dir)
    try:
        with path.open('r', encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {}


def get(data_dir, build):
    return (load(data_dir).get(str(build)) or None)


def _write_atomic(path, payload):
    """流式写临时文件（不构造整份字符串），返回临时文件路径。"""
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=0)
    return tmp


def save(data_dir, cache):
    """无条件整份写回（兼容旧调用）；需要并发安全时用 commit()。"""
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_atomic(path, cache)
    os.replace(tmp, path)
    return 'ok'


def commit(data_dir, added, base=None, base_stamp=None, attempts=3):
    """把 added 合并进缓存并原子写回，返回 dict(status, inserted, entries)。

    - 已存在的键不会被覆盖（保留其他人的新增/更正）；
    - `base`/`base_stamp` 命中且文件未变时复用已加载快照，避免重复解析整份缓存；
    - 每次尝试都在排他锁内读最新再合并；写前指纹变化则重试；
    - 全部尝试仍冲突时返回 status='conflict'，不写文件，缓存保持完整。
    """
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(data_dir)
    added = added or {}
    for _ in range(max(1, attempts)):
        handle = None
        try:
            if fcntl is not None:
                handle = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(handle, fcntl.LOCK_EX)
            before = stamp(data_dir)
            if base is not None and base_stamp is not None:
                if before != base_stamp:
                    # 文件在批次期间被改过：不在这里重新解析整份缓存（否则要在持有旧快照的
                    # 同时再读一份，峰值翻倍）。直接报告冲突，由调用方释放旧快照后重试一次，
                    # 或本轮不提交、留待下轮重算；绝不覆盖其他写入。
                    return dict(status='conflict', inserted=0, entries=None,
                                reason='stamp_changed')
                latest = base
            else:
                latest = load(data_dir)
            fresh = {str(key): value for key, value in added.items() if str(key) not in latest}
            if not fresh:
                return dict(status='nochange', inserted=0, entries=len(latest))
            merged = dict(latest)
            merged.update(fresh)
            tmp = _write_atomic(path, merged)
            if stamp(data_dir) != before:
                # 读之后、写之前有人改了文件：丢掉这次临时文件，重读重合并。
                try:
                    tmp.unlink()
                except OSError:
                    pass
                continue
            os.replace(tmp, path)
            return dict(status='ok', inserted=len(fresh), entries=len(merged))
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                finally:
                    os.close(handle)
    return dict(status='conflict', inserted=0, entries=None)


def put(data_dir, build, inferred):
    commit(data_dir, {str(build): inferred})
