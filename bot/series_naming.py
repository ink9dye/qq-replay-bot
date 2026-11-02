# -*- coding: utf-8 -*-
"""按主卡组字段（系列命名组）张数自动命名构筑；人工命名永不被覆盖。

规则（阈值、命名组、例外全部可配置，配置文件 data/series-naming.json，缺省用内置默认）：
1. 只看主卡组 partition='main'、copies>0 的卡，同一字段按张数（copies）累计，不按种类。
2. 字段优先取 card_series 的官方系列映射（data/card-series-reference.json）；
   卡号完全不在映射表里时，才按卡名包含的系列关键词兜底归类。
3. 最高字段张数 >= primary_threshold（默认 8）才作为卡组名。
4. 其余字段张数 >= secondary_threshold（默认 4）时按顺序追加；max_fields 默认 4（含主字段）。
5. 顺序：张数降序；张数相同按字段名长度降序（更长＝更具体的系列优先），再按字典序。
   主字段永远在最前；被更长字段名包含的短字段会合并掉（三幻魔 vs 幻魔、闪刀 vs 闪刀姬）。
6. 例外规则（混沌/黑魔导）不受阈值限制，按命名组成员（卡号清单/官方系列/关键词）标注；
   默认放在字段名之前（prefix），“混沌三幻魔”“黑魔导烙印”。
7. 只处理 classification_source != 'manual' 的构筑；默认也不覆盖已由 reference 规则明确识别的构筑。
8. 写库统一走 build_classification.assign(..., source='auto_series', score=...)，并更新
   stat_classification_state.revision。版本串包含阈值与配置摘要，配置变化后会自动重算。
9. dry-run 只计算和打印，不写库。

命名组注册表：每个字段名 → 组成它的卡号/关键词。默认由官方系列标签生成，
可在 data/series-naming.json 的 groups/exceptions 里改名、增删卡；网页卡组命名页
通过 describe_groups() 展示和编辑（见 statistics_deck_naming / statistics_portal）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

VERSION_PREFIX = 'series-fields-v2'
CONFIG_NAME = 'series-naming.json'

# 机制类/泛用分类标签：不作为卡组名字段（可配置覆盖）。
DEFAULT_IGNORE_FIELDS = [
    '融合', '同调', '超量', '灵摆', '连接', '仪式', '反转', '灵魂', '二重', '同盟',
    '恶魔', '战士', '剑士', '电子', '魔导', '帝王', '通常', '效果',
    '欢聚友伴',   # 泛用手坑包，人工卡组名基本不写
]

# 仅用于“卡号完全不在官方映射里”的新卡兜底，不覆盖已有官方标签的卡。
DEFAULT_KEYWORDS = {
    '混沌': ['混沌'],
    '黑魔导': ['黑魔导', '黑魔术'],
    '三幻魔': ['三幻魔'],
    '烙印': ['烙印'],
    '闪刀': ['闪刀'],
    '星辰': ['星辰'],
    '耀圣': ['耀圣'],
    '狱神': ['狱神'],
    '无垢大艺术': ['无垢大艺术'],
    '味美喵': ['味美喵'],
    '欢聚友伴': ['欢聚友伴'],
    '盈彩月夜': ['盈彩月夜'],
    '自奏圣乐': ['自奏圣乐'],
    '转生炎兽': ['转生炎兽'],
    '青眼': ['青眼'],
    '真红眼': ['真红眼'],
    '黑羽': ['黑羽'],
    '英雄': ['英雄'],
    '电子龙': ['电子龙'],
    '神艺': ['神艺'],
    '破械': ['破械'],
    '巳剑': ['巳剑'],
    '雷盟': ['雷盟'],
    '教导': ['教导'],
    '死狱乡': ['死狱乡'],
}

DEFAULT_EXCEPTIONS = []   # 例外默认关闭；混沌/黑魔导等由用户在网页“字段命名组”里按需添加
DEFAULT_EXCEPTION_ORDER = []

# 命名组由网页维护；这里只放空列表，不预置“谁包含谁”的业务内容。
DEFAULT_GROUPS = []

DEFAULT_CONFIG = {
    'primary_threshold': 8,
    'secondary_threshold': 4,
    'max_fields': 4,
    'cluster_specific_ratio': 0.6,   # 子系列张数 >= 父系列×该比例时优先用更具体的子系列名
    'ignore_fields': DEFAULT_IGNORE_FIELDS,
    'field_aliases': {},
    'keywords': DEFAULT_KEYWORDS,
    'groups': DEFAULT_GROUPS,     # 网页维护：额外的组、改名、增删卡、包含关系
    'exceptions': DEFAULT_EXCEPTIONS,
    'exception_position': 'prefix',   # prefix=例外在前；suffix=例外在后
    'exception_order': DEFAULT_EXCEPTION_ORDER,
    'require_primary': True,          # 没有达到主阈值的字段就不自动命名
    'overwrite_reference': False,     # reference 已明确识别的构筑默认不动
    'clear_stale_auto': True,         # 配置变化后不再满足规则的 auto_series 名字要清掉
    'respect_manual': False,          # 旧的人工命名全部作废：默认 auto 规则覆盖 manual
    'require_main_complete': False,   # 主卡不完整仍按已观测部分命名（记 partial 标记）
    'min_main_copies': 20,            # 主卡张数低于此值不命名
    'joiner': '',
}

GROUP_KEYS = ('name', 'sources', 'add', 'remove', 'keywords', 'enabled',
              'includes', 'keyword_scope')


class ConfigError(ValueError):
    pass


def _number(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError('%s 必须是 %d..%d 的整数' % (name, minimum, maximum))
    return value


def _group(item, index, require_keywords=True):
    """校验一个命名组定义，返回规范化副本。"""
    if not isinstance(item, dict):
        raise ConfigError('命名组 #%d 必须是对象' % index)
    unknown = set(item) - set(GROUP_KEYS)
    if unknown:
        raise ConfigError('命名组 #%d 含未知字段：%s' % (index, ','.join(sorted(unknown))))
    name = item.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise ConfigError('命名组 #%d 的 name 必须是非空、<=60 字符的字符串' % index)
    sources = item.get('sources') or []
    if not isinstance(sources, list) or any(not isinstance(tag, str) or not tag for tag in sources):
        raise ConfigError('命名组 %s 的 sources 必须是字符串列表' % name)
    codes = item.get('add') or []
    if not isinstance(codes, list) or any(type(code) is not int or not 0 < code <= 4294967295 for code in codes):
        raise ConfigError('命名组 %s 的 add 必须是正整数卡号列表' % name)
    remove = item.get('remove') or []
    if not isinstance(remove, list) or any(type(code) is not int or not 0 < code <= 4294967295 for code in remove):
        raise ConfigError('命名组 %s 的 remove 必须是正整数卡号列表' % name)
    keywords = item.get('keywords') or []
    if not isinstance(keywords, list) or any(not isinstance(word, str) or not word for word in keywords):
        raise ConfigError('命名组 %s 的 keywords 必须是字符串列表' % name)
    enabled = item.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ConfigError('命名组 %s 的 enabled 必须是布尔值' % name)
    includes = item.get('includes') or []
    if not isinstance(includes, list) or any(not isinstance(child, str) or not child for child in includes):
        raise ConfigError('命名组 %s 的 includes 必须是组名列表' % name)
    if name.strip() in includes:
        raise ConfigError('命名组 %s 不能包含自身' % name)
    keyword_scope = item.get('keyword_scope', 'unmapped')
    if keyword_scope not in ('unmapped', 'all'):
        raise ConfigError('命名组 %s 的 keyword_scope 只能是 unmapped 或 all' % name)
    return dict(name=name.strip(), sources=[tag.strip() for tag in sources],
                add=sorted(set(codes)), remove=sorted(set(remove)),
                keywords=[word.strip() for word in keywords if word.strip()], enabled=enabled,
                includes=[child.strip() for child in includes], keyword_scope=keyword_scope)


def normalize_config(raw=None):
    """合并默认值并校验；返回新 dict，附带 config_hash。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError('配置必须是 JSON 对象')
    config = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    order_provided = 'exception_order' in raw
    for key, value in raw.items():
        if key == 'config_hash' or key.startswith('_'):
            continue
        if key not in config:
            raise ConfigError('未知配置项：%s' % key)
        config[key] = value
    _number(config['primary_threshold'], 'primary_threshold', 1, 60)
    _number(config['secondary_threshold'], 'secondary_threshold', 1, 60)
    _number(config['max_fields'], 'max_fields', 1, 6)
    _number(config['min_main_copies'], 'min_main_copies', 1, 60)
    if (type(config['cluster_specific_ratio']) not in (int, float)
            or not 0 < float(config['cluster_specific_ratio']) <= 1):
        raise ConfigError('cluster_specific_ratio 必须是 (0,1] 的数字')
    config['cluster_specific_ratio'] = float(config['cluster_specific_ratio'])
    for key in ('require_primary', 'overwrite_reference', 'clear_stale_auto', 'respect_manual',
                'require_main_complete'):
        if not isinstance(config[key], bool):
            raise ConfigError('%s 必须是布尔值' % key)
    if config['exception_position'] not in ('prefix', 'suffix'):
        raise ConfigError('exception_position 只能是 prefix 或 suffix')
    if not isinstance(config['joiner'], str) or len(config['joiner']) > 4:
        raise ConfigError('joiner 必须是最多 4 个字符的字符串')
    if (not isinstance(config['ignore_fields'], list)
            or any(not isinstance(item, str) or not item for item in config['ignore_fields'])):
        raise ConfigError('ignore_fields 必须是字符串列表')
    if not isinstance(config['field_aliases'], dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in config['field_aliases'].items()):
        raise ConfigError('field_aliases 必须是 字段→字段 的字符串映射')
    if not isinstance(config['keywords'], dict) or any(
            not isinstance(key, str) or not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)
            for key, value in config['keywords'].items()):
        raise ConfigError('keywords 必须是 字段→关键词列表 的映射')
    if not isinstance(config['exception_order'], list) or any(not isinstance(item, str) for item in config['exception_order']):
        raise ConfigError('exception_order 必须是字符串列表')
    groups = []
    seen = set()
    for index, item in enumerate(config['groups'] or []):
        parsed = _group(item, index)
        if parsed['name'] in seen:
            raise ConfigError('命名组重名：%s' % parsed['name'])
        seen.add(parsed['name'])
        groups.append(parsed)
    config['groups'] = groups
    exceptions = []
    for index, item in enumerate(config['exceptions'] or []):
        parsed = _group(item, index)
        if parsed['name'] in seen:
            raise ConfigError('例外组与命名组重名：%s' % parsed['name'])
        seen.add(parsed['name'])
        exceptions.append(parsed)
    config['exceptions'] = exceptions
    all_names = {item['name'] for item in exceptions} | {item['name'] for item in groups}
    if order_provided:
        order = [name for name in config['exception_order'] if name in all_names]
        unknown = [name for name in config['exception_order'] if name not in all_names]
        if unknown:
            raise ConfigError('exception_order 含未定义的组：%s' % ','.join(unknown))
    else:
        # 没写 exception_order 时，配置里列出的 exceptions 全部按声明顺序启用。
        order = [item['name'] for item in exceptions]
    config['exception_order'] = order
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    config['config_hash'] = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return config


def load_config(data_dir):
    """读取 data_dir/series-naming.json；不存在时用默认配置。"""
    path = Path(data_dir) / CONFIG_NAME
    if not path.is_file():
        return normalize_config()
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ConfigError('读取 %s 失败：%s' % (path, exc))
    return normalize_config(raw)


def version_for(config):
    return '%s:%s' % (VERSION_PREFIX, config['config_hash'][:16])


def normalize_tag(tag):
    """strings.conf 里少量标签写成「水晶机巧|非「机巧」」，命名只取第一个名字。"""
    return tag.split('|', 1)[0].strip() or tag


def reference_catalog(path):
    """按 card_series 的语义读参考文件；失败时返回 status=unavailable/invalid。"""
    path = Path(path)
    if not path.is_file():
        return dict(status='unavailable', revision=None, cards={})
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
        if (data.get('version') != 1
                or data.get('semantics') != 'contains_official_series_in_observed_main_or_extra'
                or not isinstance(data.get('cards'), dict)):
            raise ValueError('invalid_reference')
        cards = {}
        for code, row in data['cards'].items():
            if not str(code).isdigit() or int(code) <= 0 or not isinstance(row, dict):
                raise ValueError('invalid_card')
            tags = row.get('tags')
            if (not isinstance(tags, list)
                    or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 80 for tag in tags)
                    or type(row.get('complete')) is not bool):
                raise ValueError('invalid_tags')
            cards[int(code)] = dict(tags=sorted(set(normalize_tag(tag) for tag in tags)),
                                   complete=row['complete'])
        return dict(status='ready', revision=hashlib.sha256(raw).hexdigest(), cards=cards)
    except (OSError, ValueError, TypeError):
        return dict(status='invalid_reference', revision=None, cards={})


def catalog_from_db(db, catalog=None):
    """优先用调用方给的参考；否则走 card_series.reference(db)（与统计页同源同缓存）。"""
    if catalog is not None:
        return catalog
    from .card_series import reference
    return reference(db)


def _name_text(names, code):
    entry = (names or {}).get(str(int(code))) or {}
    return entry.get('cn_name') or entry.get('name') or ''


def build_registry(catalog, config, names=None):
    """命名组注册表：字段名 → 成员卡号，外加 卡号 → 字段名 反查与关键词兜底表。

    支持两件由网站在配置里维护的事：
    - includes：大组合包含小组合（如 烙印 includes 阿不思）；小组合的卡同时计入大组合，
      每张卡对同一组只计一次，但会同时属于大组和小组，命名时只保留大组。
    - keyword_scope='all'：按卡名关键词建组（如“阿不思的落胤”没有官方系列标签也能归组）。
    """
    reference = catalog.get('cards') if isinstance(catalog, dict) else {}
    reference = reference or {}
    definitions = list(config['exceptions']) + list(config['groups'])
    claimed, groups = {}, {}
    for item in definitions:
        name = item['name']
        entry = groups.setdefault(name, dict(name=name, sources=set(), add=set(),
                                             remove=set(), keywords=[], enabled=True,
                                             includes=[], keyword_scope='unmapped',
                                             exception=name in config['exception_order']))
        entry['sources'].update(item['sources'])
        entry['add'].update(item['add'])
        entry['remove'].update(item['remove'])
        entry['keywords'].extend(item['keywords'])
        entry['includes'].extend(child for child in item['includes'] if child not in entry['includes'])
        if item.get('keyword_scope') == 'all':
            entry['keyword_scope'] = 'all'
        entry['enabled'] = entry['enabled'] and item['enabled']
        for tag in item['sources']:
            claimed[normalize_tag(tag)] = name
    for tag, target in config['field_aliases'].items():
        tag = normalize_tag(tag)
        claimed.setdefault(tag, target)
        groups.setdefault(target, dict(name=target, sources=set(), add=set(), remove=set(),
                                       keywords=[], enabled=True, includes=[],
                                       keyword_scope='unmapped',
                                       exception=target in config['exception_order']))
    members = defaultdict(set)
    for code, row in reference.items():
        for tag in row['tags']:
            name = claimed.get(tag) or tag
            if name not in groups:
                groups[name] = dict(name=name, sources={tag}, add=set(), remove=set(),
                                    keywords=[], enabled=True, includes=[],
                                    keyword_scope='unmapped',
                                    exception=name in config['exception_order'])
            if groups[name]['enabled']:
                members[name].add(code)
    # 按卡名关键词建组：只处理 keyword_scope='all' 的组，官方标签已覆盖的卡也算。
    if names:
        for name, entry in groups.items():
            if not entry['enabled'] or entry['keyword_scope'] != 'all' or not entry['keywords']:
                continue
            for code in reference:
                text = _name_text(names, code)
                if text and any(word in text for word in entry['keywords']):
                    members[name].add(code)
    for name, entry in groups.items():
        if not entry['enabled']:
            continue
        if entry['add']:
            members[name].update(entry['add'])
        if entry['remove']:
            members[name].difference_update(entry['remove'])
    missing = set()
    resolved = {}

    def resolve(name, stack):
        if name in resolved:
            return resolved[name]
        if name in stack:
            raise ConfigError('命名组 includes 存在循环：%s' % ' → '.join(stack + [name]))
        entry = groups.get(name)
        merged = set(members.get(name, ()))
        for child in (entry['includes'] if entry else ()):
            if child not in groups:
                missing.add(child)
                continue
            merged |= resolve(child, stack + [name])
        resolved[name] = merged
        return merged

    for name in list(groups):
        members[name] = resolve(name, [])
    ancestors = defaultdict(set)
    for name, entry in groups.items():
        for child in entry['includes']:
            ancestors[child].add(name)
    changed = True
    while changed:
        changed = False
        for child, parents in list(ancestors.items()):
            for parent in list(parents):
                extra = ancestors.get(parent, set()) - parents
                if extra:
                    parents |= extra
                    changed = True
    ignore = set(config['ignore_fields'])
    card_groups = defaultdict(set)
    for name, codes in members.items():
        if name in ignore:
            continue
        for code in codes:
            card_groups[code].add(name)
    keyword_map = []
    for name, words in config['keywords'].items():
        for word in words:
            keyword_map.append((name, word))
    for entry in groups.values():
        if not entry['enabled']:
            continue
        for word in entry['keywords']:
            keyword_map.append((entry['name'], word))
    keyword_map.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    return dict(groups=groups, members={name: sorted(codes) for name, codes in members.items()},
                card_groups={code: frozenset(names) for code, names in card_groups.items()},
                keywords=keyword_map, ignore=ignore,
                ancestors={child: frozenset(parents) for child, parents in ancestors.items()},
                missing_includes=sorted(missing))


def candidate_fields(code, catalog, registry, names, config):
    """返回某张卡命中的字段名集合（官方映射优先；完全没有映射时才按卡名关键词）。"""
    known = set(registry['card_groups'].get(int(code), ()))
    if known:
        return known
    reference = catalog.get('cards') if isinstance(catalog, dict) else None
    if reference is not None and int(code) in reference:
        return set()   # 官方映射里明确“不属于任何系列”的卡（泛用卡）不做关键词猜测
    name = (names or {}).get(str(int(code))) or {}
    text = name.get('cn_name') or name.get('name') or ''
    if not text:
        return set()
    for field, word in registry['keywords']:
        if word in text:
            return {field}
    return set()


def count_fields(cards, catalog, config, names=None, registry=None):
    """cards: [{code,copies}]；返回字段累计张数、未归类张数与未映射卡数。"""
    registry = registry or build_registry(catalog, config)
    counts = Counter()
    untagged = 0
    unknown_cards = 0
    total = 0
    for card in cards:
        code = int(card['code'])
        copies = int(card['copies'])
        if copies <= 0:
            continue
        total += copies
        fields = candidate_fields(code, catalog, registry, names, config)
        if fields:
            # 子组合的卡同时计入 includes 它的父组合（大组合包含小组合）。
            ancestors = registry.get('ancestors') or {}
            expanded = set(fields)
            for field in fields:
                expanded |= set(ancestors.get(field) or ())
            fields = expanded
        if not fields:
            untagged += copies
            reference = catalog.get('cards') if isinstance(catalog, dict) else None
            if reference is not None and code not in reference:
                unknown_cards += 1
            continue
        for field in fields:
            counts[field] += copies
    return dict(fields=counts, total=total, untagged=untagged, unknown_cards=unknown_cards)


def _clusters(names, registry=None):
    """把互相包含的字段名并成一组。

    两种包含关系都算：
    - 名字包含：三幻魔 vs 幻魔、闪刀 vs 闪刀姬；
    - 卡表包含 / includes：大组合的成员覆盖小组合（烙印 ⊃ 阿不思），即使名字不相含。
    """
    parent = {name: name for name in names}

    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    members = (registry or {}).get('members') or {}
    ancestors = (registry or {}).get('ancestors') or {}
    ordered = list(names)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            related = left in right or right in left
            if not related:
                a = set(members.get(left) or ())
                b = set(members.get(right) or ())
                related = bool(a and b and (a <= b or b <= a))
            if not related:
                related = (left in (ancestors.get(right) or ())
                           or right in (ancestors.get(left) or ()))
            if related:
                root_left, root_right = find(left), find(right)
                if root_left != root_right:
                    parent[root_right] = root_left
    groups = defaultdict(list)
    for name in ordered:
        groups[find(name)].append(name)
    return list(groups.values())


def select_fields(counts, config, registry=None):
    """嵌套系列合并 + 排序：同一父/子系列只保留一个代表字段。

    张数多的代表这个卡组的主系列；名字相含的子系列张数达到父系列的
    cluster_specific_ratio（默认 0.6）时优先用更具体的子系列名（命运英雄 优先于 英雄），
    否则用父系列（闪刀姬 远少于 闪刀 时仍叫闪刀）。includes 的大组合直接覆盖小组合。
    """
    filtered = {name: copies for name, copies in counts.items()
                if name not in config['ignore_fields'] and name not in config['exception_order']}
    ratio = float(config.get('cluster_specific_ratio', 0.6))
    members = (registry or {}).get('members') or {}
    representatives = []
    for cluster in _clusters(list(filtered), registry):
        top = max(filtered[name] for name in cluster)
        leaders = [name for name in cluster if filtered[name] == top]
        # 张数相同时取成员更少（更具体）的组：只带 4 张阿不思时叫阿不思，
        # 10 张烙印凑齐时烙印张数更多，自然用烙印。
        broad = min(leaders, key=lambda name: (len(members.get(name) or ()), -len(name), name))
        floor = filtered[broad] * ratio
        specific = [name for name in cluster
                    if len(name) > len(broad) and broad in name and filtered[name] >= floor]
        representatives.append(max(specific, key=lambda name: (len(name), filtered[name], name))
                               if specific else broad)
    representatives.sort(key=lambda name: (-filtered[name], -len(name), name))
    return [(name, filtered[name]) for name in representatives]


def compose_name(selected, exceptions, config):
    """按阈值挑主/次字段，再拼例外；返回 (name 或 None, 依据 dict)。"""
    primary = None
    secondaries = []
    if selected and selected[0][1] >= config['primary_threshold']:
        primary = selected[0]
        limit = config['max_fields'] - 1
        for name, copies in selected[1:]:
            if len(secondaries) >= limit:
                break
            if copies >= config['secondary_threshold']:
                secondaries.append((name, copies))
    fields = ([primary] if primary else []) + secondaries
    if primary is None and config['require_primary']:
        return None, dict(reason='below_primary_threshold', fields=fields,
                          primary=None, secondaries=[])
    # 命名顺序：张数少的系列在前、张数多的在后（“数量越多越在最后”）。
    ordered = sorted(fields, key=lambda item: (item[1], len(item[0]), item[0]))
    field_names = [name for name, _ in ordered]
    exception_names = [name for name in config['exception_order']
                       if copies_of(exceptions, name) > 0]
    if not field_names and not exception_names:
        return None, dict(reason='no_field', fields=fields, primary=None, secondaries=[])
    if config['exception_position'] == 'prefix':
        parts = exception_names + field_names
    else:
        parts = field_names + exception_names
    name = config['joiner'].join(parts)
    if not name or len(name) > 120:
        return None, dict(reason='name_too_long', fields=fields, primary=primary,
                          secondaries=secondaries, exceptions=exception_names)
    return name, dict(reason='ok', fields=ordered, primary=primary,
                      secondaries=secondaries, exceptions=exception_names)


def field_contributors(cards, catalog, config, names=None, registry=None):
    """某个构筑里每个字段由哪些卡组成（给网页“字段明细”用）。"""
    registry = registry or build_registry(catalog, config, names)
    ancestors = registry.get('ancestors') or {}
    per = defaultdict(list)
    for card in cards:
        code = int(card['code'])
        copies = int(card['copies'])
        if copies <= 0:
            continue
        fields = candidate_fields(code, catalog, registry, names, config)
        expanded = set(fields)
        for field in fields:
            expanded |= set(ancestors.get(field) or ())
        for field in expanded:
            per[field].append(dict(code=code, copies=copies))
    rows = []
    for field, items in per.items():
        rows.append(dict(field=field, copies=sum(item['copies'] for item in items), cards=items))
    rows.sort(key=lambda row: (-row['copies'], row['field']))
    return rows


def copies_of(counts, name):
    return int(counts.get(name) or 0)


def propose(cards, catalog, config, names=None, main_complete=True, registry=None):
    """单构筑命名建议；cards 为主卡组 [{code,copies}]。"""
    registry = registry or build_registry(catalog, config, names)
    counted = count_fields(cards, catalog, config, names, registry)
    exceptions = {name: copies_of(counted['fields'], name) for name in config['exception_order']}
    selected = select_fields(counted['fields'], config, registry)
    name, basis = compose_name(selected, exceptions, config)
    total = counted['total']
    coverage = (basis.get('primary')[1] / total) if basis.get('primary') and total else 0.0
    return dict(name=name, basis=basis,
                fields=[dict(name=field, copies=copies) for field, copies in selected[:8]],
                exceptions=[dict(name=name, copies=copies) for name, copies in exceptions.items() if copies],
                main_copies=total, untagged_copies=counted['untagged'],
                unknown_cards=counted['unknown_cards'], main_complete=bool(main_complete),
                score=round(coverage, 4) if name else None)


def _main_cards(db, builds):
    """一次取回这些 build 的主卡组；避免逐 build 查询。"""
    result = {}
    builds = list(builds)
    for start in range(0, len(builds), 400):
        chunk = builds[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        for row in db.execute("SELECT build,code,copies FROM stat_build_cards "
                              "WHERE partition='main' AND copies>0 AND build IN (%s)" % marks, chunk):
            result.setdefault(row['build'], []).append(dict(row))
    return result


def _build_rows(db, builds):
    result = {}
    builds = list(builds)
    for start in range(0, len(builds), 400):
        chunk = builds[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        for row in db.execute('SELECT fingerprint,deck_type,classification_source,'
                              'classification_status,classification_version,classification_score,'
                              'main_complete FROM stat_builds WHERE fingerprint IN (%s)' % marks, chunk):
            result[row['fingerprint']] = dict(row)
    return result


def active_builds(db, now, scope='active'):
    """最近14天有效视角里的构筑；scope=top100 时只取当前名单玩家。"""
    if scope == 'top100':
        rows = db.execute(
            'SELECT DISTINCT v.build FROM stat_views v '
            'JOIN stat_games g ON g.digest=v.digest '
            'JOIN stat_current_top100 t ON t.player=v.player '
            'WHERE g.expires_at>?', (float(now),))
    else:
        rows = db.execute(
            'SELECT DISTINCT v.build FROM stat_views v '
            'JOIN stat_games g ON g.digest=v.digest WHERE g.expires_at>?', (float(now),))
    return [row[0] for row in rows]


def pending_builds(db, now, version, scope='active', respect_manual=True):
    """还没按当前配置算过的构筑；后台自动刷新用。respect_manual=False 时人工构筑也要重算。"""
    manual = "" if not respect_manual else " AND b.classification_source!='manual'"
    if scope == 'top100':
        rows = db.execute(
            'SELECT DISTINCT v.build FROM stat_views v '
            'JOIN stat_games g ON g.digest=v.digest '
            'JOIN stat_current_top100 t ON t.player=v.player '
            'JOIN stat_builds b ON b.fingerprint=v.build '
            'WHERE g.expires_at>?' + manual +
            ' AND (b.classification_version IS NULL OR b.classification_version!=?)',
            (float(now), version))
    else:
        rows = db.execute(
            'SELECT DISTINCT v.build FROM stat_views v '
            'JOIN stat_games g ON g.digest=v.digest '
            'JOIN stat_builds b ON b.fingerprint=v.build '
            'WHERE g.expires_at>?' + manual +
            ' AND (b.classification_version IS NULL OR b.classification_version!=?)',
            (float(now), version))
    return [row[0] for row in rows]


def latest_builds(db, now, version=None, respect_manual=True):
    """每位玩家最新一局的有效构筑；后台自动命名只处理这一批。"""
    sql = ('WITH ranked AS ('
           ' SELECT v.player,v.build,ROW_NUMBER() OVER (PARTITION BY v.player '
           ' ORDER BY g.recorded_at DESC,v.digest DESC) rn'
           ' FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE g.expires_at>?)'
           ' SELECT r.build FROM ranked r JOIN stat_builds b ON b.fingerprint=r.build'
           ' WHERE r.rn=1')
    params = [float(now)]
    if respect_manual:
        sql += " AND b.classification_source!='manual'"
    if version is not None:
        sql += ' AND (b.classification_version IS NULL OR b.classification_version!=?)'
        params.append(version)
    return [row[0] for row in db.execute(sql, params)]


def all_builds(db):
    """全部构筑（一次性统一改名/迁移用）。"""
    return [row[0] for row in db.execute('SELECT fingerprint FROM stat_builds')]


def latest_main_builds(db, replay_root, now):
    """每人最新一场的 game1 构筑（与玩家页/饼图同一套选择）。"""
    from .statistics_dashboard import population
    _basis, latest, _people = population(db, Path(replay_root), float(now), resolve_rank=False)
    return [row['build'] for row in latest.values() if row.get('build')]


def _filter_pending(db, builds, version, respect_manual):
    pending = []
    for start in range(0, len(builds), 400):
        chunk = builds[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        for row in db.execute('SELECT fingerprint,classification_source,classification_version '
                              'FROM stat_builds WHERE fingerprint IN (%s)' % marks, chunk):
            if not respect_manual and row['classification_source'] == 'manual':
                pending.append(row['fingerprint'])
            elif row['classification_version'] != version:
                pending.append(row['fingerprint'])
    return pending


def pending_latest_main(db, replay_root, now, version, respect_manual=True):
    builds = (latest_main_builds(db, replay_root, now) if replay_root is not None
              else latest_builds(db, now))
    return _filter_pending(db, builds, version, respect_manual)


def apply_names(db, now, config, catalog=None, names=None, scope='latest',
                replay_root=None, only_pending=True, commit=True):
    """按当前配置计算并写入 auto_series；后台定时/新录像同步时调用。

    scope='latest' 时命名“每人最新一场的 game1 构筑”（与玩家页一致）；
    respect_manual=False（默认）时人工构筑也会按规则重算，保证全站名字统一。
    """
    catalog = catalog_from_db(db, catalog)
    version = version_for(config)
    respect = config['respect_manual']
    if only_pending and scope == 'everything':
        builds = all_builds(db)
    elif only_pending:
        if scope == 'latest':
            builds = (pending_latest_main(db, replay_root, now, version, respect)
                      if replay_root is not None
                      else _filter_pending(db, latest_builds(db, now), version, respect))
        elif scope == 'top100':
            builds = pending_builds(db, now, version, 'top100', respect)
        else:
            builds = pending_builds(db, now, version, 'active', respect)
    elif scope == 'latest':
        builds = (latest_main_builds(db, replay_root, now) if replay_root is not None
                  else latest_builds(db, now, None, respect))
    elif scope == 'everything':
        builds = all_builds(db)
    elif scope in ('top100', 'active'):
        builds = active_builds(db, now, scope)
    else:
        builds = None
    result = plan(db, now, config, catalog, names, builds=builds, scope=scope,
                  replay_root=replay_root)
    written = apply_plan(db, result['rows'], config, commit=commit)
    return dict(planned=result['summary'], written=written)


def scope_builds(db, now, scope='top100', replay_root=None):
    """要被自动命名的构筑：默认当前前100名单每人最新一局的有效构筑。"""
    from .statistics_deck_naming import _latest_views, _roster
    roster = _roster(db)
    views = _latest_views(db, now, replay_root,
                          only=set(roster) if scope == 'top100' else None)
    if scope == 'top100':
        ordered = [name for name, _row in sorted(roster.items(), key=lambda item: item[1]['rank'])]
        selected = [views.get(name) for name in ordered]
    else:
        selected = list(views.values())
    builds, seen = [], set()
    for view in selected:
        build = (view or {}).get('build')
        if build and build not in seen:
            seen.add(build)
            builds.append(build)
    return builds


def plan(db, now, config, catalog=None, names=None, builds=None, scope='top100',
         replay_root=None):
    """计算命名计划（不写库）。返回 summary + rows。"""
    catalog = catalog_from_db(db, catalog)
    registry = build_registry(catalog, config, names)
    if builds is None:
        if scope == 'latest':
            builds = (latest_main_builds(db, replay_root, now) if replay_root is not None
                      else latest_builds(db, now, None, config['respect_manual']))
        elif scope == 'everything':
            builds = all_builds(db)
        else:
            builds = scope_builds(db, now, scope, replay_root)
    builds = [build for build in builds if build]
    cards = _main_cards(db, builds)
    rows = _build_rows(db, builds)
    planned = []
    for build in builds:
        info = rows.get(build)
        if info is None:
            continue
        cards_for_build = cards.get(build, [])
        item = dict(build=build, current=info['deck_type'], source=info['classification_source'],
                    status=info['classification_status'], version=info['classification_version'],
                    main_complete=info['main_complete'])
        if config['respect_manual'] and info['classification_source'] == 'manual':
            item.update(action='skip', reason='manual', name=None)
            planned.append(item)
            continue
        if (not config['overwrite_reference'] and info['classification_source'] == 'reference'
                and info['classification_status'] == 'recognized'):
            item.update(action='skip', reason='reference_recognized', name=None)
            planned.append(item)
            continue
        if config['require_main_complete'] and not info['main_complete']:
            item.update(action='skip', reason='main_incomplete', name=None)
            planned.append(item)
            continue
        total = sum(int(card['copies']) for card in cards_for_build)
        if total < config['min_main_copies']:
            item.update(action='skip', reason='too_few_main_copies', name=None, main_copies=total)
            planned.append(item)
            continue
        proposal = propose(cards_for_build, catalog, config, names,
                           bool(info['main_complete']), registry)
        item.update(proposal)
        item['reason'] = proposal['basis'].get('reason')
        if proposal['name'] is None:
            stale = (config['clear_stale_auto']
                     and (info['classification_source'] == 'auto_series'
                          or (info['classification_source'] == 'manual'
                              and not config['respect_manual']))
                     and info['deck_type'])
            item['action'] = 'clear' if stale else 'skip'
        elif proposal['name'] == info['deck_type'] and info['classification_source'] == 'auto_series':
            item['action'] = 'unchanged'
        else:
            item['action'] = 'create'
        planned.append(item)
    summary = dict(catalog_status=catalog.get('status'), builds=len(planned),
                   create=sum(1 for row in planned if row['action'] == 'create'),
                   clear=sum(1 for row in planned if row['action'] == 'clear'),
                   unchanged=sum(1 for row in planned if row['action'] == 'unchanged'),
                   skipped=sum(1 for row in planned if row['action'] == 'skip'),
                   skipped_reasons=dict(Counter(row['reason'] for row in planned if row['action'] == 'skip')),
                   version=version_for(config), scope=scope)
    return dict(summary=summary, rows=planned)


def apply_plan(db, planned, config, commit=True):
    """把计划写成 auto_series；返回变化数。人工命名在 assign 前已排除。"""
    from .build_classification import assign
    version = version_for(config)
    changed = 0
    for row in planned:
        if row['action'] == 'clear':
            changed += int(bool(assign(db, row['build'], None, [], version,
                                       source='auto_series', score=None)))
            continue
        if row['action'] != 'create' or not row['name']:
            continue
        series = [name for name, _copies in row['basis'].get('fields') or []]
        series += [name for name in (row['basis'].get('exceptions') or [])]
        series = sorted({name for name in series if name})
        changed += int(bool(assign(db, row['build'], row['name'], series, version,
                                   source='auto_series', score=row.get('score'))))
    if commit:
        db.commit()
    revision = db.execute('SELECT revision FROM stat_classification_state WHERE singleton=1').fetchone()[0]
    return dict(changed=changed, version=version, revision=revision)


def describe_groups(catalog, config, names=None, limit=None):
    """给网页卡组命名页用的命名组清单：字段名、成员卡号、来源、包含关系、是否例外。"""
    registry = build_registry(catalog, config, names)
    groups = registry['groups']
    rows = []
    for name in sorted(groups):
        entry = groups[name]
        codes = registry['members'].get(name) or []
        rows.append(dict(name=name, exception=name in config['exception_order'],
                         enabled=bool(entry['enabled']) and name not in config['ignore_fields'],
                         ignored=name in config['ignore_fields'],
                         sources=sorted(entry['sources']), keywords=list(entry['keywords']),
                         keyword_scope=entry['keyword_scope'],
                         includes=list(entry['includes']),
                         included_by=sorted(registry['ancestors'].get(name) or ()),
                         added=len(entry['add']), removed=len(entry['remove']),
                         cards=codes if limit is None else codes[:limit], cards_total=len(codes)))
    return rows


def resolve_card_terms(data_dir, terms):
    """把“卡片统计”那种“卡名或卡片密码”输入解析成卡号。

    组合编辑的输入框沿用卡片统计：输入卡名/卡片密码 → 候选 → 选中加入组；
    这里只负责把文本解析成精确卡号，重名/异画返回 candidates 交给页面选。
    返回 (codes, problems)。
    """
    from .statistics_card_names import search
    codes, problems = set(), []
    for term in terms:
        text = str(term or '').strip()
        if not text:
            continue
        if text.isdigit() and 0 < int(text) <= 4294967295:
            codes.add(int(text))
            continue
        found = search(data_dir, text, limit=20)
        exact = [card for card in found if card.get('name') == text]
        if len(found) == 1 or len(exact) == 1:
            codes.add(int((exact or found)[0]['code']))
        elif found:
            problems.append(dict(term=text, reason='ambiguous', candidates=found[:10]))
        else:
            problems.append(dict(term=text, reason='not_found'))
    return sorted(codes), problems


MANUAL_DISCARD_VERSION = 'manual-discarded'


def reset_manual(db, commit=True):
    """作废全部旧人工命名：清空构筑名并删除“人工名库”（相似度标签来源）。

    只动分类数据，不改表结构；之后按字段规则重新命名，保证全站名字统一。
    """
    from .build_classification import assign
    builds = [row[0] for row in db.execute(
        "SELECT fingerprint FROM stat_builds WHERE classification_source='manual'")]
    changed = 0
    for build in builds:
        changed += int(bool(assign(db, build, None, [], MANUAL_DISCARD_VERSION,
                                   source='unclassified', score=None)))
    library = 0
    try:
        library = db.execute('SELECT count(*) FROM stat_manual_decks').fetchone()[0]
        db.execute('DELETE FROM stat_manual_decks')
    except sqlite3.Error:
        pass
    if commit:
        db.commit()
    revision = db.execute('SELECT revision FROM stat_classification_state WHERE singleton=1').fetchone()[0]
    return dict(manual_builds=len(builds), changed=changed, manual_library_removed=library,
                revision=revision)


def format_plan(result, names_by_build=None, limit=None):
    """dry-run 文本表格。"""
    lines = []
    summary = result['summary']
    lines.append('参考映射：%s；范围：%s；构筑：%d；将写入：%d；清理：%d；不变：%d；跳过：%d'
                 % (summary['catalog_status'], summary['scope'], summary['builds'],
                    summary['create'], summary['clear'], summary['unchanged'], summary['skipped']))
    reasons = summary.get('skipped_reasons') or {}
    if reasons:
        lines.append('跳过原因：' + '，'.join('%s=%d' % (key, value) for key, value in sorted(reasons.items())))
    lines.append('%-3s %-16s %-14s %-14s %-10s %s' % ('#', '玩家', '现名', '新名', '主字段', '字段top/例外'))
    shown = 0
    for row in result['rows']:
        if row['action'] == 'skip' and row.get('reason') == 'manual':
            continue
        if limit is not None and shown >= limit:
            break
        shown += 1
        player = (names_by_build or {}).get(row['build'], '')[:16]
        fields = ' '.join('%s×%d' % (item['name'], item['copies']) for item in (row.get('fields') or [])[:5])
        exceptions = row.get('exceptions') or []
        if exceptions:
            fields += ' | 例外:' + ','.join('%s×%d' % (item['name'], item['copies']) for item in exceptions)
        if row['action'] == 'skip':
            fields += ' | 跳过:%s' % row.get('reason')
        if row['action'] == 'clear':
            fields += ' | 将清理旧自动名'
        basis = row.get('basis') or {}
        primary = basis.get('primary') or (None, 0)
        lines.append('%-3s %-16s %-14s %-14s %-10s %s'
                     % ('', player, (row.get('current') or '未命名')[:12],
                        (row.get('name') or '-')[:12],
                        (primary[0] or '-')[:10], fields))
    return '\n'.join(lines)


def _default_paths(root):
    root = Path(root)
    config = json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))
    data = Path(config.get('data_dir') or root / 'data')
    return data, data / 'bot.sqlite3'


def main(argv=None):
    parser = argparse.ArgumentParser(description='按主卡组字段张数自动命名构筑（默认 dry-run）')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--scope', default='top100',
                        choices=('top100', 'latest', 'active', 'all', 'everything'))
    parser.add_argument('--apply', action='store_true', help='写入数据库（默认只打印）')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--primary', type=int)
    parser.add_argument('--secondary', type=int)
    parser.add_argument('--max-fields', type=int)
    parser.add_argument('--exception-position', choices=('prefix', 'suffix'))
    parser.add_argument('--no-replay-filter', action='store_true',
                        help='预览用：不按录像文件是否存在过滤视角（快照/离线库）')
    parser.add_argument('--reset-manual', action='store_true',
                        help='作废旧人工命名和人工名库（默认只打印数量，加 --apply 才执行）')
    parser.add_argument('--respect-manual', action='store_true',
                        help='本次保留人工命名（默认按配置覆盖人工，保证统一）')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    data, db_path = _default_paths(root)
    raw = {}
    config_path = data / CONFIG_NAME
    if config_path.is_file():
        raw = json.loads(config_path.read_text(encoding='utf-8-sig'))
    if args.primary is not None:
        raw['primary_threshold'] = args.primary
    if args.secondary is not None:
        raw['secondary_threshold'] = args.secondary
    if args.max_fields is not None:
        raw['max_fields'] = args.max_fields
    if args.exception_position:
        raw['exception_position'] = args.exception_position
    if args.respect_manual:
        raw['respect_manual'] = True
    config = normalize_config(raw)
    import time
    from .store import Store
    from .statistics_card_names import index
    store = Store(db_path)
    try:
        db = store._db
        now = time.time()
        if args.reset_manual:
            pending = db.execute("SELECT count(*) FROM stat_builds WHERE classification_source='manual'").fetchone()[0]
            if not args.apply:
                print('将作废 %d 个人工命名（预览；加 --apply 才执行）' % pending)
                return 0
            removed = reset_manual(db)
            print('已作废 %d 个人工命名，删除人工名库 %d 条；分类修订 %d'
                  % (removed['changed'], removed['manual_library_removed'], removed['revision']))
        catalog = catalog_from_db(db)
        names = index(data)
        replay_root = None
        if not args.no_replay_filter:
            replay_root = Path(json.loads((root / 'config.json').read_text(encoding='utf-8-sig')).get('replay_dir')
                               or data / 'replays')
        result = plan(db, now, config, catalog, names, scope=args.scope, replay_root=replay_root)
        try:
            from .statistics_deck_naming import _latest_views
            views = _latest_views(db, now, replay_root)
            names_by_build = {view['build']: name for name, view in views.items()}
        except Exception:
            names_by_build = {}
        if args.json:
            print(json.dumps(dict(summary=result['summary'], rows=result['rows']),
                             ensure_ascii=False, indent=1))
        else:
            print(format_plan(result, names_by_build, args.limit))
        if args.apply:
            written = apply_plan(db, result['rows'], config)
            print('已写入 %d 个构筑；版本 %s；分类修订 %d'
                  % (written['changed'], written['version'], written['revision']))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
