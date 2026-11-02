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
                config_hash=config['config_hash'],
                version=series_naming.version_for(config))


def _catalog(data_dir):
    return series_naming.reference_catalog(Path(data_dir) / 'card-series-reference.json')


def _names(data_dir):
    try:
        from .statistics_card_names import index
        return index(data_dir)
    except Exception:
        return {}


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
    registry = series_naming.build_registry(catalog, config, names)
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
    cards = [dict(code=code, name=_card_name(names, code)) for code in codes]
    return dict(name=name,
                cards=cards if limit is None else cards[:limit],
                cards_total=len(cards),
                sources=sorted(entry['sources']),
                keywords=list(entry['keywords']),
                keyword_scope=entry['keyword_scope'],
                includes=list(entry['includes']),
                included_by=sorted(registry['ancestors'].get(name) or ()),
                exception=name in registry.get('exception_names', ()) or entry.get('exception', False),
                enabled=bool(entry['enabled']),
                ignored=name in (registry.get('ignore') or ()),
                used_builds=int(usage.get(name, 0)))


def list_groups(db, data_dir, now, query='', limit=50, offset=0):
    config = series_naming.load_config(data_dir)
    catalog = _catalog(data_dir)
    names = _names(data_dir)
    registry = series_naming.build_registry(catalog, config, names)
    registry['exception_names'] = set(config['exception_order'])
    usage = usage_counts(db, data_dir, now, config, catalog, names)
    rows = []
    needle = (query or '').strip().lower()
    for name in registry['groups']:
        entry = registry['groups'][name]
        row = _row(entry, registry, usage, names, limit=0)
        if needle and needle not in name.lower():
            continue
        rows.append(row)
    rows.sort(key=lambda item: (not item['enabled'], -item['used_builds'], -item['cards_total'], item['name']))
    total = len(rows)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    return dict(config=config_summary(config), total=total, limit=limit, offset=offset,
                enabled_total=sum(1 for row in rows if row['enabled']),
                exception_total=sum(1 for row in rows if row['exception']),
                rows=rows[offset:offset + limit], catalog_status=catalog.get('status'))


def group_detail(db, data_dir, now, name):
    name = (name or '').strip()
    if not name or len(name) > 60:
        raise ValueError('组名无效。')
    config = series_naming.load_config(data_dir)
    catalog = _catalog(data_dir)
    names = _names(data_dir)
    registry = series_naming.build_registry(catalog, config, names)
    registry['exception_names'] = set(config['exception_order'])
    entry = registry['groups'].get(name)
    if entry is None:
        raise LookupError('group_missing')
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
    if 'keyword_scope' in payload:
        if payload['keyword_scope'] not in ('unmapped', 'all'):
            raise ValueError('keyword_scope 只能是 unmapped 或 all')
        fields['keyword_scope'] = payload['keyword_scope']
    if 'enabled' in payload:
        if not isinstance(payload['enabled'], bool):
            raise ValueError('enabled 必须是布尔值')
        fields['enabled'] = payload['enabled']
    return fields


def _validate(config, catalog, names):
    try:
        registry = series_naming.build_registry(catalog, config, names)
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
        _validate(config, catalog, names)
        backup = save_raw(data_dir, raw)
        return dict(changed=True, backup=str(backup) if backup else None,
                    config=config_summary(config))
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
        registry = series_naming.build_registry(catalog, config, names)
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
        _validate(config, catalog, names)
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
        _validate(config, catalog, names)
        backup = save_raw(data_dir, raw)
        return dict(changed=True, name=new_name, backup=str(backup) if backup else None,
                    config=config_summary(config))
    kind, index = _find_group(raw, before)
    if op in ('create', 'save'):
        if op == 'create' and kind is not None:
            raise ValueError('组名已存在：%s' % name)
        if name != before:
            other, _ = _find_group(raw, name)
            if other is not None:
                raise ValueError('已存在同名组：%s' % name)
        if kind is not None:
            item = dict(raw[kind][index])
        else:
            # 由官方映射派生出来的组第一次修改：先把现有 sources 固化，避免丢成员。
            config = series_naming.load_config(data_dir)
            registry = series_naming.build_registry(catalog, config, names)
            entry = registry['groups'].get(before) or registry['groups'].get(name) or {}
            item = dict(name=name, sources=sorted(entry.get('sources') or []))
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
    elif op in ('add_cards', 'remove_cards'):
        codes = _resolve_terms(data_dir, payload.get('cards'))
        if kind is None:
            # 由官方映射派生出来的组第一次改动时，先把现有 sources 固化，避免丢成员。
            config = series_naming.load_config(data_dir)
            registry = series_naming.build_registry(catalog, config, names)
            entry = registry['groups'].get(before) or registry['groups'].get(name) or {}
            item = dict(name=name, sources=sorted(entry.get('sources') or []))
            raw.setdefault('groups', []).append(item)
            kind, index = 'groups', len(raw['groups']) - 1
        item = dict(raw[kind][index])
        add = set(item.get('add') or [])
        remove = set(item.get('remove') or [])
        if op == 'add_cards':
            for code in codes:
                if code in remove:
                    remove.discard(code)
                else:
                    add.add(code)
        else:
            for code in codes:
                if code in add:
                    add.discard(code)
                else:
                    remove.add(code)
        item['add'] = sorted(add)
        item['remove'] = sorted(remove)
        item['name'] = name
        raw[kind][index] = item
    else:
        raise ValueError('不支持的操作：%s' % op)
    config = series_naming.normalize_config(raw)
    _validate(config, catalog, names)
    backup = save_raw(data_dir, raw)
    return dict(changed=True, name=name, backup=str(backup) if backup else None,
                config=config_summary(config))
