# -*- coding: utf-8 -*-
"""前100卡组分布的三层数据：内环主大类、中环交集细分、外环具体卡组名。

口径（用户 2026-11-02 确认）：
- 每个卡组只归一个“主大类”，内环合计 100%；
- 某命名组覆盖的不同卡组名 >= threshold（默认 3）才升为“大类”；
- 主大类按“重复程度”选：覆盖不同卡组名更多的字段优先；同数再看覆盖人数、名字里出现的先后；
  所以 混沌烙印/混沌三幻魔/混沌影依 会归到“混沌”内环，而不是各自归到烙印/三幻魔/影依；
- 中环把内环再拆成“独占”和“∩其他大类”，仍合计 100%，不额外加总；
- 外环是精确卡组名，合计 100%。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

PALETTE = [
    '#0072B2', '#E69F00', '#009E73', '#D55E00', '#CC79A7', '#56B4E9',
    '#F0E442', '#4E79A7', '#8C564B', '#7F9C45', '#B04A6E', '#5C6B73',
]
UNKNOWN_COLOR = '#AAB5C4'
OTHER_COLOR = '#C9D2DA'
THRESHOLD = 3


def _hex_to_rgb(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return '#%02X%02X%02X' % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def _shade(color, lightness):
    """按 HLS 明度调整，保留色相/饱和度，用于同色系外环。"""
    import colorsys
    red, green, blue = _hex_to_rgb(color)
    hue, light, sat = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
    red, green, blue = colorsys.hls_to_rgb(hue, max(0.08, min(0.92, lightness)), sat)
    return _rgb_to_hex((red * 255, green * 255, blue * 255))


def _mix(left, right, ratio=0.5):
    a, b = _hex_to_rgb(left), _hex_to_rgb(right)
    return _rgb_to_hex(tuple(a[i] * (1 - ratio) + b[i] * ratio for i in range(3)))


_CACHE = {}
_FIRST_CACHE = {}


def _stamps(data_dir):
    reference = data_dir / 'card-series-reference.json'
    from . import series_naming
    config_path = data_dir / series_naming.CONFIG_NAME
    try:
        return tuple((path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
                     for path in (config_path, reference))
    except OSError:
        return None


def first_cards(data_dir):
    """每个命名组的第一张卡（组内卡号最小）作为代表图。"""
    from . import series_naming
    data_dir = Path(data_dir)
    stamp = _stamps(data_dir)
    if stamp is None:
        return {}
    key = str(data_dir.resolve())
    cached = _FIRST_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        config = series_naming.load_config(data_dir)
        catalog = series_naming.reference_catalog(data_dir / 'card-series-reference.json')
        registry = series_naming.build_registry(catalog, config)
    except Exception:
        return {}
    result = {}
    for name, codes in registry['members'].items():
        if not codes:
            continue
        chosen = (registry['groups'].get(name) or {}).get('representative')
        result[name] = chosen if chosen in codes else min(codes)
    _FIRST_CACHE[key] = (stamp, result)
    return result


def group_names(data_dir):
    """所有可当字段的命名组名（官方标签 + 网页里配置的组/例外），去掉忽略字段。"""
    from . import series_naming
    data_dir = Path(data_dir)
    reference = data_dir / 'card-series-reference.json'
    config_path = data_dir / series_naming.CONFIG_NAME
    try:
        stamp = tuple((path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
                      for path in (config_path, reference))
    except OSError:
        return []
    key = str(data_dir.resolve())
    cached = _CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        config = series_naming.load_config(data_dir)
        catalog = series_naming.reference_catalog(reference)
    except Exception:
        return []
    names = set(config.get('exception_order') or [])
    for item in config.get('groups') or []:
        if isinstance(item, dict) and item.get('name'):
            names.add(str(item['name']))
    for row in (catalog.get('cards') or {}).values():
        names.update(row.get('tags') or [])
    names -= set(config.get('ignore_fields') or [])
    result = sorted(name.strip() for name in names if len(name.strip()) >= 2)
    _CACHE[key] = (stamp, result)
    return result


def match_fields(deck_name, groups):
    """返回卡组名里命中的字段，按名字中出现的先后排序；三幻魔优先于幻魔。"""
    text = str(deck_name or '')
    hits = [(text.find(group), group) for group in groups if group in text]
    hits.sort(key=lambda item: (-len(item[1]), item[0]))
    kept = []
    for position, group in hits:
        if any(group in other for _pos, other in kept):
            continue
        kept.append((position, group))
    kept.sort(key=lambda item: item[0])
    return [group for _position, group in kept]


def _distribution_rows(distribution):
    rows = []
    for item in distribution or []:
        try:
            players = int(item.get('players') or 0)
        except (TypeError, ValueError):
            players = 0
        name = str(item.get('name') or '').strip()
        if not name or players <= 0:
            continue
        rows.append((name, players))
    rows.sort(key=lambda item: (-item[1], item[0]))
    return rows


def layers(distribution, groups, threshold=THRESHOLD, representatives=None, deck_images=None):
    """计算三层饼图数据；groups 由 group_names() 提供，representatives 为组→代表卡号。"""
    rows = _distribution_rows(distribution)
    total = sum(players for _name, players in rows)
    if not total:
        return dict(total=0, threshold=threshold, inner=[], middle=[], outer=[],
                    inner_percent=0.0, middle_percent=0.0, outer_percent=0.0, note='')
    matches = {name: match_fields(name, groups) for name, _players in rows}
    distinct = Counter()
    for name, _players in rows:
        for field in set(matches[name]):
            distinct[field] += 1
    qualified = {field for field, count in distinct.items() if count >= threshold}

    primaries = {}
    field_players = Counter()
    for name, players in rows:
        for field in set(matches[name]):
            field_players[field] += players
    for name, players in rows:
        fields = [field for field in matches[name] if field in qualified]
        if name == '未识别':
            primaries[name] = '未识别'
        elif fields:
            positions = {field: index for index, field in enumerate(matches[name])}
            # 主大类按“重复程度”：覆盖不同卡组名越多越优先；同数看覆盖人数、出现先后。
            primaries[name] = max(fields, key=lambda field: (
                distinct[field], field_players[field], -positions.get(field, 0), -len(field), field))
        else:
            primaries[name] = '其他'

    inner_players = Counter()
    middle = defaultdict(int)
    preliminary = Counter()
    for name, players in rows:
        preliminary[primaries[name]] += players
    for name in list(primaries):
        if primaries[name] not in ('其他', '未识别') and preliminary[primaries[name]] < 3:
            primaries[name] = '其他'
    for name, players in rows:
        primary = primaries[name]
        inner_players[primary] += players
        extras = [field for field in matches[name] if field in qualified and field != primary]
        middle[(primary, tuple(extras))] += players

    ordered = [name for name, _count in inner_players.most_common()]
    ordered.sort(key=lambda name: (-inner_players[name], name))
    color_map = {}
    palette_index = 0
    for name in ordered:
        if name == '未识别':
            color_map[name] = UNKNOWN_COLOR
        elif name == '其他':
            color_map[name] = OTHER_COLOR
        else:
            color_map[name] = PALETTE[palette_index % len(PALETTE)]
            palette_index += 1

    inner = [dict(name=name, players=inner_players[name],
                  percent=round(100 * inner_players[name] / total, 1), color=color_map[name],
                  image=(representatives or {}).get(name))
             for name in ordered]
    middle_rows = []
    for primary in ordered:
        entries = sorted(((key[1], count) for key, count in middle.items() if key[0] == primary),
                         key=lambda item: (-item[1], len(item[0]), item[0]))
        for extras, players in entries:
            label = '独占' if not extras else '∩' + '、'.join(extras)
            accent = color_map.get(extras[0]) if extras else None
            middle_rows.append(dict(primary=primary, label=label, extras=list(extras),
                                    players=players, percent=round(100 * players / total, 1),
                                    color=color_map[primary], accent=accent,
                                    image=(representatives or {}).get(extras[0] if extras else primary)
                                    or (representatives or {}).get(primary),
                                    mixed=_mix(color_map[primary], accent) if accent else None))
    outer = []
    for name, players in rows:
        primary = primaries[name]
        extras = [field for field in matches[name] if field in qualified and field != primary]
        secondary = [field for field in matches[name] if field != primary]
        ordered_fields = sorted(secondary, key=lambda field: (-field_players[field], matches[name].index(field)))
        siblings = sorted((item for item in rows if primaries[item[0]] == primary),
                          key=lambda item: (-item[1], item[0]))
        rank = next(index for index, item in enumerate(siblings) if item[0] == name)
        lightness = 62 - min(rank, 5) * 7
        color = color_map[primary]
        image = None
        for field in ordered_fields + [primary] + matches[name]:
            image = (representatives or {}).get(field)
            if image:
                break
        if not image:
            image = (deck_images or {}).get(name)
        outer.append(dict(name=name, players=players, percent=round(100 * players / total, 1),
                          primary=primary, extras=extras, shared=bool(extras),
                          color=_shade(color, lightness / 100),
                          accent=color_map.get(extras[0]) if extras else None,
                          image=image))
    outer.sort(key=lambda item: (-item['players'], item['name']))
    for cat in inner:
        images = {row.get('image') for row in outer
                  if row['primary'] == cat['name'] and row.get('image')}
        cat['band_image'] = next(iter(images)) if len(images) == 1 else None
    return dict(total=total, threshold=threshold,
                inner=inner, middle=middle_rows, outer=outer,
                inner_percent=sum(item['percent'] for item in inner),
                middle_percent=sum(item['percent'] for item in middle_rows),
                outer_percent=sum(item['percent'] for item in outer),
                qualified=sorted(qualified),
                note='内环按主大类各算一次、合计100%；中环是内环的交集细分；外环为具体卡组名。')


def from_data_dir(distribution, data_dir, threshold=THRESHOLD, representatives=None, deck_images=None):
    return layers(distribution, group_names(data_dir), threshold,
                  representatives if representatives is not None else first_cards(data_dir),
                  deck_images)
