# -*- coding: utf-8 -*-
"""录像文件存活判定的等价快路径：一次列目录代替逐个文件 stat。

`share.safe_replay_file` 对普通相对路径（不含 ".."、非绝对路径）的判定是：

    candidate = root_resolved / rel_path
    candidate.is_file() and not candidate.is_symlink()  -> 存活

也就是“目录项存在、跟随解析后是普通文件、且该目录项本身不是符号链接”。
因此对同一父目录里的全部文件，目录项类型就足够判定：

- 目录项是普通文件（DT_REG）：跟随仍是普通文件、本身不是链接 -> 存活，与原型一致；
- 目录项是其他确定类型（目录/FIFO/设备）：原型 is_file() 为假，最终解析仍为假 -> 不存活；
- 目录项是符号链接：可能指向根内文件（原型会 resolve 后判定），回退原函数；
- 目录里没有该名字：确定大小的相对路径在区分大小写的文件系统上 stat 也必然失败 -> 不存活；
  非 POSIX 平台可能区分大小写不敏感，回退原函数；
- 父目录无法列举、条目类型未知、路径为绝对路径/含 ".."/反斜杠/异常写法：一律回退原函数。

每次请求重新列举一次父目录（生产约 412 个玩家目录、1.5 万文件），不使用任何跨请求
结论、不设 TTL、不落库、不写连接、不产生 WAL；目录一有增删改名，下一次请求直接看到。
"""
from __future__ import annotations

import os
from pathlib import Path

_MISSING = object()
# 目录项缺失能否直接判死：只有区分大小写的 POSIX 文件系统才与 stat 结果一致。
MISSING_MEANS_DEAD = os.name == 'posix'


def _escaping(rel_path):
    """绝对路径、越界路径、空值或非常规写法都交给原函数判定。"""
    if not rel_path or rel_path.startswith(('/', '\\')):
        return True
    if '\\' in rel_path or '//' in rel_path or rel_path.startswith('./'):
        return True
    if '..' in rel_path:
        return '..' in rel_path.split('/')
    return False


def _listing(state, parent):
    """父目录的 {名字: True 普通文件 / False 确定非普通文件 / None 需回退}。

    parent 是相对根目录的父目录（'' 表示根目录本身）。
    无法列举时返回 None，调用方必须回退 safe_replay_file。
    """
    cached = state['dirs'].get(parent, _MISSING)
    if cached is not _MISSING:
        return cached
    entries = None
    path = str(state['root_resolved']) if not parent else str(state['root_resolved'] / parent)
    try:
        with os.scandir(path) as iterator:
            entries = {}
            for entry in iterator:
                try:
                    if entry.is_symlink():
                        entries[entry.name] = None
                    elif entry.is_file(follow_symlinks=False):
                        entries[entry.name] = True
                    else:
                        entries[entry.name] = False
                except OSError:
                    entries[entry.name] = None
    except OSError:
        entries = None
    state['dirs'][parent] = entries
    return entries


def _fast_verdict(state, rel_path):
    """True/False 表示已确定，None 表示必须回退 safe_replay_file。"""
    if _escaping(rel_path):
        return None
    parent, _, name = rel_path.rpartition('/')
    entries = state['dirs'].get(parent, _MISSING)
    if entries is _MISSING:
        entries = _listing(state, parent)
    if entries is None:
        return None
    verdict = entries.get(name, _MISSING)
    if verdict is True:
        return True
    if verdict is False:
        return False
    if verdict is _MISSING and MISSING_MEANS_DEAD:
        return False
    return None


def new_state(root):
    """一次请求/一次批处理共用的状态（父目录列举结果）；不跨请求保存。"""
    return dict(root_resolved=Path(root).resolve(), dirs={}, dir_cache={})


def path_alive(root, rel_path, state=None):
    """单个 rel_path 的存活判定；布尔结果与 safe_replay_file 完全一致。"""
    from .share import safe_replay_file
    if state is None:
        state = new_state(root)
    verdict = _fast_verdict(state, rel_path)
    if verdict is not None:
        return verdict
    return bool(safe_replay_file(root, rel_path, state['root_resolved'], state['dir_cache']))


def _file_paths(db, digests=None):
    """digest -> rel_path 列表；digests 为 None 时返回全表。

    候选很多（>800）时先整表读一次再在内存里过滤：比几十条 IN 查询更便宜，
    结果与逐条 IN 相同（同一快照、同一张表）。
    """
    sql = 'SELECT f.digest,r.rel_path FROM stat_files f JOIN replays r ON r.id=f.replay_id'
    result = {}
    if digests is None:
        for digest, rel_path in db.execute(sql):
            result.setdefault(digest, []).append(rel_path)
        return result
    wanted = list(dict.fromkeys(digests))
    if len(wanted) > 800:
        targets = set(wanted)
        for digest, rel_path in db.execute(sql):
            if digest in targets:
                result.setdefault(digest, []).append(rel_path)
        return result
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        for digest, rel_path in db.execute(sql + ' WHERE f.digest IN (' + marks + ')', chunk):
            result.setdefault(digest, []).append(rel_path)
    return result


def alive_map(db, root, digests=None):
    """digest -> 是否仍有可读原始录像；与逐条 safe_replay_file 判定等价。

    digests 为 None 时读取全部 stat_files（调用方只关心其中一部分也不会错）。
    给出 digests 时只判定这些 digest：查询调用方通常只关心本次候选视角，
    未给出的 digest 不会得到结论（也不会被当成存活）。
    没有任何文件的 digest 视为不存活，与原实现 `any(())` 一致。
    """
    from .share import safe_replay_file
    state = new_state(root)
    wanted = None if digests is None else list(dict.fromkeys(digests))
    paths = _file_paths(db, wanted)
    root_resolved = state['root_resolved']
    dir_cache = state['dir_cache']
    result = {}
    for digest, rel_paths in paths.items():
        alive = False
        for rel_path in rel_paths:
            verdict = _fast_verdict(state, rel_path)
            if verdict is True:
                alive = True
                break
            if verdict is False:
                continue
            if safe_replay_file(root, rel_path, root_resolved, dir_cache):
                alive = True
                break
        result[digest] = alive
    if wanted is not None:
        for digest in wanted:
            result.setdefault(digest, False)
    return result
