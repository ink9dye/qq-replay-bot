# -*- coding: utf-8 -*-
"""卡组命名页的“组合编辑”后端：命名组的读取、增删卡、改名、包含关系和阈值保存。

配置文件是 data/series-naming.json（不改表结构）；写入前备份、临时文件原子替换。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from . import series_naming

_LOCK = threading.Lock()
CONFIG_NAME = series_naming.CONFIG_NAME


def _config_path(data_dir):
    return Path(data_dir) / CONFIG_NAME


def _backup_dir(data_dir):
    return Path(data_dir) / 'config-backups'


def load_raw(data_dir):
    path = _config_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_raw(data_dir, raw):
    """备份后原子替换配置文件；返回备份路径。"""
    path = _config_path(data_dir)
    data_dir = Path(data_dir)
    backup = None
    with _LOCK:
        if path.exists():
            target = _backup_dir(data_dir)
            target.mkdir(parents=True, exist_ok=True)
            backup = target / ('series-naming.%s.json'
                               % datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3])
            shutil.copy2(path, backup)
        payload = dict(raw)
        payload.pop('config_hash', None)
        temp = path.with_name(path.name + '.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(temp, path)
    return backup


def config_summary(config):
    return dict(primary_threshold=config['primary_threshold'],
                secondary_threshold=config['secondary_threshold'],
                max_fields=config['max_fields'],
                cluster_specific_ratio=config['cluster_specific_ratio'],
                exception_order=list(config['exception_order']),
                exception_position=config['exception_position'],
                size_prefix_total=config.get('size_prefix_total'),
                size_prefix_label=config.get('size_prefix_label'),
                config_hash=config['config_hash'],
                version=series_naming.version_for(config))


_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE = {}


def _catalog(data_dir):
    """按文件指纹复用系列参考：每次请求重解析 685KB JSON 在线上是纯浪费。"""
    path = Path(data_dir) / 'card-series-reference.json'
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return series_naming.reference_catalog(path)
    with _CATALOG_LOCK:
        hit = _CATALOG_CACHE.get(str(path))
        if hit is not None and hit[0] == stamp:
            return hit[1]
    catalog = series_naming.reference_catalog(path)
    with _CATALOG_LOCK:
        _CATALOG_CACHE[str(path)] = (stamp, catalog)
        if len(_CATALOG_CACHE) > 8:
            _CATALOG_CACHE.pop(next(iter(_CATALOG_CACHE)))
    return catalog


def _names(data_dir):
    try:
        from .statistics_card_names import index
        return index(data_dir)
    except Exception:
        return {}


_REGISTRY_LOCK = threading.Lock()
_REGISTRY_CACHE = {}


def _file_stamp(path):
    try:
        stat = Path(path).stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _registry(data_dir, config=None, catalog=None, names=None):
    """按配置/参考/卡名文件指纹复用命名组注册表。

    构建一次是 609 组 × 全卡表关键词的遍历（线上秒级），而组合编辑每读一次列表、
    每点开一个组都要用同一个注册表；不缓存就等于每步都重算一遍。
    """
    data_dir = Path(data_dir)
    config = config if config is not None else series_naming.load_config(data_dir)
    catalog = catalog if catalog is not None else _catalog(data_dir)
    names = names if names is not None else _names(data_dir)
    key = (str(data_dir), config.get('config_hash'), (catalog or {}).get('revision'),
           _file_stamp(data_dir / CONFIG_NAME), _file_stamp(data_dir / 'cache' / 'cards.json'),
           _file_stamp(data_dir / 'card-equivalence.json'))
    with _REGISTRY_LOCK:
        hit = _REGISTRY_CACHE.get(key)
        if hit is not None:
            return dict(hit)
    registry = series_naming.build_registry(catalog, config, names)
    with _REGISTRY_LOCK:
        _REGISTRY_CACHE[key] = registry
        if len(_REGISTRY_CACHE) > 8:
            _REGISTRY_CACHE.pop(next(iter(_REGISTRY_CACHE)))
    return dict(registry)


def _card_name(names, code):
    entry = names.get(str(int(code))) or {}
    return entry.get('cn_name') or entry.get('name') or ''


def _latest_views(db, now, roster_only=True):
    """每人最新一局有效视角（不看录像文件，组合页面统计用）。"""
    join = ('JOIN stat_current_top100 t ON t.player=v.player ' if roster_only else '')
    order = 'SELECT v.player,v.build,ROW_NUMBER() OVER (PARTITION BY v.player ORDER BY g.recorded_at DESC,v.digest DESC) rn'
    rows = db.execute('WITH ranked AS ('
                      + order +
                      ' FROM stat_views v JOIN stat_games g ON g.digest=v.digest '
                      + join +
                      ' WHERE g.expires_at>?) SELECT player,build FROM ranked WHERE rn=1',
                      (float(now),))
    return [(row['player'], row['build']) for row in rows]


def usage_counts(db, data_dir, now, config=None, catalog=None, names=None, roster_only=True):
    """每个组被多少个“最新构筑”用到（按卡组数，不按张数）。"""
    config = config or series_naming.load_config(data_dir)
    catalog = catalog if catalog is not None else _catalog(data_dir)
    names = names if names is not None else _names(data_dir)
    registry = _registry(data_dir, config, catalog, names)
    views = _latest_views(db, now, roster_only=roster_only)
    builds = [build for _player, build in views if build]
    cards = series_naming._main_cards(db, builds)
    counter = {}
    for build in builds:
        counted = series_naming.count_fields(cards.get(build, []), catalog, config, names, registry)
        for field in counted['fields']:
            counter[field] = counter.get(field, 0) + 1
    return counter


def _row(entry, registry, usage, names, limit=None):
    name = entry['name']
    codes = registry['members'].get(name) or []
    core = set((registry.get('core_members') or {}).get(name) or ())
    optional_codes = set(entry.get('optional') or ())
    cards = [dict(code=code, name=_card_name(names, code)) for code in codes if code in core]
    optional_cards = [dict(code=code, name=_card_name(names, code)) for code in sorted(optional_codes)]
    return dict(name=name,
                small_name=entry.get('small_name'),
                cards=cards if limit is None else cards[:limit],
                optional_cards=optional_cards,
                cards_total=len(codes),
                sources=sorted(entry['sources']),
                keywords=list(entry['keywords']),
                keyword_scope=entry['keyword_scope'],
                includes=list(entry['includes']),
                included_by=sorted(registry['ancestors'].get(name) or ()),
                exception=name in registry.get('exception_names', ()) or entry.get('exception', False),
                enabled=bool(entry['enabled']),
                ignored=name in (registry.get('ignore') or ()),
                used_builds=int(usage.get(name, 0)))


def combo_stats(db, data_dir, now):
    """组合编辑的共享派生统计：每组命中多少个“最新构筑”，以及具体是哪几副。

    与 usage_counts + group_detail 各自重算的结果完全一致（同样按 _latest_views 的顺序、
    同样只把有份数的构筑记进明细、usage 仍按 usage_counts 的“字段出现即 +1”），
    区别只是“每构筑命中字段”只算一次，给所有组共用，供后台快照缓存。
    """
    config = series_naming.load_config(data_dir)
    catalog = _catalog(data_dir)
    names = _names(data_dir)
    registry = _registry(data_dir, config, catalog, names)
    views = _latest_views(db, now)
    builds = [build for _player, build in views if build]
    cards = series_naming._main_cards(db, builds)
    deck_types = {item['fingerprint']: item['deck_type'] for item in db.execute(
        'SELECT fingerprint,deck_type FROM stat_builds')}
    usage, used_by = {}, {}
    for player, build in views:
        if not build:
            continue
        fields = series_naming.count_fields(cards.get(build, []), catalog, config, names,
                                            registry)['fields']
        for field in fields:
            usage[field] = usage.get(field, 0) + 1
        for field, copies in fields.items():
            if not copies:
                continue
            used_by.setdefault(field, []).append(
                dict(player=player, build=build, copies=copies,
                     deck_type=deck_types.get(build)))
    return dict(version=series_naming.version_for(config), generated_at=float(now),
                views=len(views), builds=len(builds), usage=usage, used_by=used_by)


def list_groups(db, data_dir, now, query='', limit=50, offset=0, stats=None):
    config = series_naming.load_config(data_dir)
    catalog = _catalog(data_dir)
    names = _names(data_dir)
    registry = _registry(data_dir, config, catalog, names)
    registry['exception_names'] = set(config['exception_order'])
    usage = (stats['usage'] if stats is not None
             else usage_counts(db, data_dir, now, config, catalog, names))
    rows = []
    needle = (query or '').strip().lower()
    for name in registry['groups']:
        entry = registry['groups'][name]
        row = _row(entry, registry, usage, names, limit=0)
        if needle and needle not in name.lower():
            continue
        rows.append(row)
    rows = [item for item in rows if not item.get('ignored')]
    rows.sort(key=lambda item: (not item['enabled'], -item['used_builds'], -item['cards_total'], item['name']))
    total = len(rows)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    return dict(config=config_summary(config), total=total, limit=limit, offset=offset,
                enabled_total=sum(1 for row in rows if row['enabled']),
                exception_total=sum(1 for row in rows if row['exception']),
                rows=rows[offset:offset + limit], catalog_status=catalog.get('status'))


def group_detail(db, data_dir, now, name, stats=None):
    name = (name or '').strip()
    if not name or len(name) > 60:
        raise ValueError('组名无效。')
    config = series_naming.load_config(data_dir)
    if name in config['ignore_fields']:
        raise LookupError('group_missing')
    catalog = _catalog(data_dir)
    names = _names(data_dir)
    registry = _registry(data_dir, config, catalog, names)
    registry['exception_names'] = set(config['exception_order'])
    entry = registry['groups'].get(name)
    if entry is None:
        raise LookupError('group_missing')
    if stats is not None:
        # 明细来自后台快照：不再为“查看一个组”重算全部构筑。
        usage = stats.get('usage') or {}
        row = _row(entry, registry, usage, names)
        used = [dict(item) for item in (stats.get('used_by') or {}).get(name, ())]
        used.sort(key=lambda item: -item['copies'])
        row['builds'] = used
        row['builds_total'] = len(used)
        row['representative'] = entry.get('representative')
        row['config'] = config_summary(config)
        return row
    usage = usage_counts(db, data_dir, now, config, catalog, names)
    row = _row(entry, registry, usage, names)
    used = []
    views = _latest_views(db, now)
    builds = [build for _player, build in views if build]
    cards = series_naming._main_cards(db, builds)
    deck_types = {item['fingerprint']: item['deck_type'] for item in db.execute(
        'SELECT fingerprint,deck_type FROM stat_builds')}
    for player, build in views:
        if not build:
            continue
        counted = series_naming.count_fields(cards.get(build, []), catalog, config, names, registry)
        copies = counted['fields'].get(name, 0)
        if not copies:
            continue
        used.append(dict(player=player, build=build, copies=copies,
                         deck_type=deck_types.get(build)))
    used.sort(key=lambda item: -item['copies'])
    row['builds'] = used
    row['builds_total'] = len(used)
    row['representative'] = entry.get('representative')
    row['config'] = config_summary(config)
    return row


def _find_group(raw, name):
    for index, item in enumerate(raw.get('groups') or []):
        if isinstance(item, dict) and item.get('name') == name:
            return 'groups', index
    for index, item in enumerate(raw.get('exceptions') or []):
        if isinstance(item, dict) and item.get('name') == name:
            return 'exceptions', index
    return None, None


def _group_payload(payload):
    fields = {}
    for key in ('sources', 'keywords', 'includes'):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = [part.strip() for part in value.replace('，', ',').split(',') if part.strip()]
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError('%s 必须是字符串列表' % key)
        fields[key] = [item.strip() for item in value]
    if 'small_name' in payload:
        value = payload.get('small_name')
        fields['small_name'] = (value or '').strip() if isinstance(value, str) else value
    if 'keyword_scope' in payload:
        if payload['keyword_scope'] not in ('unmapped', 'all'):
            raise ValueError('keyword_scope 只能是 unmapped 或 all')
        fields['keyword_scope'] = payload['keyword_scope']
    if 'enabled' in payload:
        if not isinstance(payload['enabled'], bool):
            raise ValueError('enabled 必须是布尔值')
        fields['enabled'] = payload['enabled']
    return fields


def _validate(config, catalog, names, data_dir=None):
    try:
        # 用同一份（按指纹缓存的）注册表做校验：保存后紧接着的列表/详情直接复用，
        # 不必为一个“能不能保存”的判断再重建一次 609 组的注册表。
        registry = (_registry(data_dir, config, catalog, names) if data_dir is not None
                    else series_naming.build_registry(catalog, config, names))
    except series_naming.ConfigError as exc:
        raise ValueError(str(exc))
    return registry


def _resolve_terms(data_dir, terms):
    if not isinstance(terms, list) or not terms:
        raise ValueError('请提供卡号或卡名。')
    codes, problems = series_naming.resolve_card_terms(data_dir, terms)
    if problems:
        raise ValueError('无法确定卡片：' + '；'.join(
            '%s(%s)' % (item['term'], item['reason']) for item in problems[:5]))
    return codes


def _same_name_codes(data_dir, codes):
    """同一张卡的不同印刷/异画卡号：加回或移除时一起处理。

    实际对局里同一张卡会出现不同卡号（如 88570003 与 88570004），只按一个卡号
    增删会让“移除”残留另一个卡号，名字统计少算。
    """
    codes = {int(code) for code in codes}
    try:
        names = _names(data_dir)
    except Exception:
        return codes
    wanted = set()
    for code in codes:
        text = ((names.get(str(code)) or {}).get('cn_name') or '').strip()
        if text:
            wanted.add(text)
    if not wanted:
        return codes
    for key, entry in names.items():
        text = (entry.get('cn_name') or '').strip()
        if text in wanted and str(key).isdigit():
            codes.add(int(key))
    return codes


def _db_path(db):
    try:
        row = db.execute('PRAGMA database_list').fetchone()
        return Path(row[2])
    except (sqlite3.Error, TypeError, IndexError):
        return None


def _rename_log(data_dir, text):
    path = Path(data_dir) / 'logs' / 'naming-rename.log'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('[%s] %s\n' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text))
    except OSError:
        pass


def _rename_worker(db_path, data_dir, old_name, new_name):
    """后台改名：前端页面在展示的构筑立刻改；历史构筑进慢速队列慢慢追。"""
    connection = None
    try:
        connection = sqlite3.connect(str(db_path), timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=60000')
        config = series_naming.load_config(data_dir)
        replays = Path(data_dir)/'replays'
        result = series_naming.rename_builds(
            connection, old_name, new_name, config,
            replay_root=replays if replays.is_dir() else None,
            progress=lambda state: _rename_log(data_dir, '改名重算 %d/%d 副'
                                               % (state['done'], state['total'])))
        _rename_log(data_dir, '改名完成 %s → %s：共 %d 副，前端页面立刻改 %d 副（实际改名 %d 副），'
                              '其余 %d 副进慢速队列由后台空闲时重算'
                    % (old_name, new_name, result['total'], result['front'],
                       result['changed'], result['queued']))
    except Exception as exc:  # 后台线程不能影响保存动作
        _rename_log(data_dir, '改名重算失败 %s → %s：%s' % (old_name, new_name, exc))
    finally:
        if connection is not None:
            connection.close()


def _queue_rename(db, data_dir, old_name, new_name):
    """改名保存后立刻排队：先给出影响面，再由后台线程逐个重算。"""
    path = _db_path(db)
    try:
        config = series_naming.load_config(data_dir)
        targets = series_naming.rename_targets(db, old_name, new_name, config)
    except (OSError, ValueError, sqlite3.Error):
        targets, config = [], None
    if not targets or path is None:
        return dict(total=len(targets), queued=False)
    worker = threading.Thread(target=_rename_worker,
                              args=(path, Path(data_dir), old_name, new_name),
                              name='naming-rename', daemon=True)
    worker.start()
    return dict(total=len(targets), queued=True)


def update_group(db, data_dir, now, payload):
    """组合编辑的保存入口；支持 save/create/rename/add_cards/remove_cards/thresholds。"""
    if not isinstance(payload, dict):
        raise ValueError('参数无效。')
    op = payload.get('op') or 'save'
    data_dir = Path(data_dir)
    raw = load_raw(data_dir)
    catalog = _catalog(data_dir)
    names = _names(data_dir)
    if op == 'thresholds':
        for key in ('primary_threshold', 'secondary_threshold', 'max_fields', 'cluster_specific_ratio'):
            if key in payload:
                value = payload[key]
                if isinstance(value, str) and value.strip().isdigit():
                    value = int(value.strip())
                raw[key] = value
        config = series_naming.normalize_config(raw)
        _validate(config, catalog, names, data_dir)
        backup = save_raw(data_dir, raw)
        return dict(changed=True, backup=str(backup) if backup else None,
                    config=config_summary(config))
    if op == 'delete':
        name = (payload.get('name') or '').strip()
        if not name or len(name) > 60:
            raise ValueError('组名无效。')
        kind, index = _find_group(raw, name)
        if kind is not None:
            raw[kind].pop(index)
        for key in ('groups', 'exceptions'):
            for item in raw.get(key) or []:
                if isinstance(item, dict) and isinstance(item.get('includes'), list):
                    item['includes'] = [child for child in item['includes'] if child != name]
        raw['exception_order'] = [value for value in (raw.get('exception_order') or []) if value != name]
        ignored = [value for value in (raw.get('ignore_fields') or []) if value != name]
        ignored.append(name)
        raw['ignore_fields'] = sorted(set(ignored))
        config = series_naming.normalize_config(raw)
        _validate(config, catalog, names, data_dir)
        backup = save_raw(data_dir, raw)
        return dict(changed=True, name=name, deleted=True,
                    backup=str(backup) if backup else None, config=config_summary(config))
    name = (payload.get('name') or '').strip()
    if not name or len(name) > 60:
        raise ValueError('组名无效。')
    before = (payload.get('before') or name).strip()
    fields = _group_payload(payload)
    if op == 'representative':
        code = payload.get('card')
        if code in (None, '', 0):
            code = None
        elif isinstance(code, str) and code.isdigit():
            code = int(code)
        if code is not None and (type(code) is not int or not 0 < code <= 4294967295):
            raise ValueError('代表卡卡号无效。')
        config = series_naming.load_config(data_dir)
        registry = _registry(data_dir, config, catalog, names)
        members = set(registry['members'].get(name) or [])
        if code is not None and code not in members:
            raise ValueError('代表卡必须是该组的成员卡。')
        kind, index = _find_group(raw, before)
        if kind is None:
            entry = registry['groups'].get(before) or registry['groups'].get(name) or {}
            item = dict(name=name, sources=sorted(entry.get('sources') or []))
            raw.setdefault('groups', []).append(item)
            kind, index = 'groups', len(raw['groups']) - 1
        item = dict(raw[kind][index])
        if code is None:
            item.pop('representative', None)
        else:
            item['representative'] = code
        item['name'] = name
        raw[kind][index] = item
        config = series_naming.normalize_config(raw)
        _validate(config, catalog, names, data_dir)
        backup = save_raw(data_dir, raw)
        return dict(changed=True, name=name, representative=code,
                    backup=str(backup) if backup else None, config=config_summary(config))
    if op == 'rename':
        new_name = (payload.get('new_name') or '').strip()
        if not new_name or len(new_name) > 60:
            raise ValueError('新组名无效。')
        if new_name != before:
            other, _ = _find_group(raw, new_name)
            if other is not None:
                raise ValueError('已存在同名组：%s' % new_name)
        kind, index = _find_group(raw, before)
        if kind is None:
            raise LookupError('group_missing')
        item = raw[kind][index]
        item['name'] = new_name
        raw[kind][index] = item
        if kind == 'exceptions' and isinstance(raw.get('exception_order'), list):
            raw['exception_order'] = [new_name if value == before else value
                                      for value in raw['exception_order']]
        config = series_naming.normalize_config(raw)
        _validate(config, catalog, names, data_dir)
        backup = save_raw(data_dir, raw)
        rename = _queue_rename(db, data_dir, before, new_name)
        return dict(changed=True, name=new_name, backup=str(backup) if backup else None,
                    config=config_summary(config), rename=rename)
    kind, index = _find_group(raw, before)
    if op in ('create', 'save'):
        if name != before:
            other, _ = _find_group(raw, name)
            if other is not None:
                raise ValueError('已存在同名组：%s' % name)
        if kind is not None:
            item = dict(raw[kind][index])
        else:
            # 由官方映射派生出来的组第一次修改：先把现有 sources 固化，避免丢成员。
            config = series_naming.load_config(data_dir)
            registry = _registry(data_dir, config, catalog, names)
            entry = registry['groups'].get(before) or registry['groups'].get(name) or {}
            item = dict(name=name, sources=sorted(entry.get('sources') or []))
        if op == 'create' and kind is not None:
            # 同名组按合并处理：保留原成员，再并入本次提交的列表。
            for key in ('sources', 'keywords', 'includes'):
                item[key] = sorted(set(item.get(key) or []) | set(fields.get(key) or []))
            for key in ('add', 'remove'):
                item[key] = sorted(set(item.get(key) or []) | set(fields.get(key) or []))
        else:
            item.update(fields)
        item['name'] = name
        toggle = payload.get('exception')
        if toggle is not None and not isinstance(toggle, bool):
            raise ValueError('exception 必须是布尔值')
        if toggle is None:
            target = 'exceptions' if kind == 'exceptions' else 'groups'
        else:
            target = 'exceptions' if toggle else 'groups'
        for key in ('groups', 'exceptions'):
            raw[key] = [value for value in (raw.get(key) or [])
                        if not (isinstance(value, dict) and value.get('name') in (name, before))]
        raw.setdefault(target, []).append(item)
        exception_names = [value['name'] for value in (raw.get('exceptions') or [])
                           if isinstance(value, dict) and value.get('name')]
        if 'exception_order' in raw or exception_names:
            old = [value for value in (raw.get('exception_order') or []) if value in exception_names]
            raw['exception_order'] = old + [value for value in exception_names if value not in old]
    elif op == 'move_card':
        code = payload.get('card')
        if isinstance(code, str) and code.isdigit():
            code = int(code)
        if type(code) is not int or not 0 < code <= 4294967295:
            raise ValueError('卡号无效。')
        to = payload.get('to')
        if to not in ('required', 'optional'):
            raise ValueError('目标必须是 required 或 optional。')
        if kind is None:
            config = series_naming.load_config(data_dir)
            registry = _registry(data_dir, config, catalog, names)
            entry = registry['groups'].get(before) or registry['groups'].get(name) or {}
            item = dict(name=name, sources=sorted(entry.get('sources') or []))
            raw.setdefault('groups', []).append(item)
            kind, index = 'groups', len(raw['groups']) - 1
        item = dict(raw[kind][index])
        add = set(item.get('add') or [])
        remove = set(item.get('remove') or [])
        optional = set(item.get('optional') or [])
        if to == 'optional':
            optional.add(code)
            remove.add(code)
            add.discard(code)
        else:
            optional.discard(code)
            remove.discard(code)
            add.add(code)
        item['add'] = sorted(add)
        item['remove'] = sorted(remove)
        item['optional'] = sorted(optional)
        item['name'] = name
        raw[kind][index] = item
    elif op in ('add_optional', 'remove_optional'):
        codes = _resolve_terms(data_dir, payload.get('cards'))
        if kind is None:
            config = series_naming.load_config(data_dir)
            registry = _registry(data_dir, config, catalog, names)
            entry = registry['groups'].get(before) or registry['groups'].get(name) or {}
            item = dict(name=name, sources=sorted(entry.get('sources') or []))
            raw.setdefault('groups', []).append(item)
            kind, index = 'groups', len(raw['groups']) - 1
        item = dict(raw[kind][index])
        optional = set(item.get('optional') or [])
        if op == 'add_optional':
            optional.update(codes)
        else:
            optional.difference_update(codes)
        item['optional'] = sorted(optional)
        item['name'] = name
        raw[kind][index] = item
    elif op in ('add_cards', 'remove_cards'):
        codes = _resolve_terms(data_dir, payload.get('cards'))
        group_codes = _same_name_codes(data_dir, codes)
        if kind is None:
            # 由官方映射派生出来的组第一次改动时，先把现有 sources 固化，避免丢成员。
            config = series_naming.load_config(data_dir)
            registry = _registry(data_dir, config, catalog, names)
            entry = registry['groups'].get(before) or registry['groups'].get(name) or {}
            item = dict(name=name, sources=sorted(entry.get('sources') or []))
            raw.setdefault('groups', []).append(item)
            kind, index = 'groups', len(raw['groups']) - 1
        item = dict(raw[kind][index])
        add = set(item.get('add') or [])
        remove = set(item.get('remove') or [])
        if op == 'add_cards':
            remove.difference_update(group_codes)
            for code in codes:
                add.add(code)
        else:
            for code in group_codes:
                add.discard(code)
                remove.add(code)
        item['add'] = sorted(add)
        item['remove'] = sorted(remove)
        item['name'] = name
        raw[kind][index] = item
    else:
        raise ValueError('不支持的操作：%s' % op)
    config = series_naming.normalize_config(raw)
    _validate(config, catalog, names, data_dir)
    backup = save_raw(data_dir, raw)
    return dict(changed=True, name=name, backup=str(backup) if backup else None,
                config=config_summary(config))
