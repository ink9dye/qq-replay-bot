# -*- coding: utf-8 -*-
"""卡组命名后台：前100 / 前200 / 全部已采集 + 搜索，查看某人最新构筑、写入 manual 分类。

只做三件事：读名单与最新构筑、读某个 build 的卡表、把一个 build 的卡组名写成 manual。
不碰对局事实、不改保留期；系列标签在只改名时原样保留。
前200名次用官方单人接口按需核验（有界等待、失败按未知处理），未知名次不冒充前200。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

TOP_N = 100
MAX_ROWS = 500
CST = timezone(timedelta(hours=8))


def _latest_views(db, now, replay_root=None, only=None):
    """每位玩家最新一条视角；前100按排名页同口径（来源有效、未排除、继承首局）。"""
    rows = db.execute('''WITH ranked AS (
        SELECT v.player, v.build, v.digest, g.recorded_at,
               b.deck_type, b.classification_status, b.classification_source,
               ROW_NUMBER() OVER (PARTITION BY v.player ORDER BY g.recorded_at DESC, v.digest DESC) rn
        FROM stat_views v
        JOIN stat_games g ON g.digest=v.digest
        JOIN stat_builds b ON b.fingerprint=v.build
        WHERE g.expires_at>?)
        SELECT player,build,digest,recorded_at,deck_type,classification_status,classification_source FROM ranked WHERE rn=1''',
                       (float(now),))
    views = {row['player'].strip(): dict(row) for row in rows}
    if not views:
        return views
    from .statistics_match_identity import inherit_first_game_types
    all_rows = [dict(row) for row in db.execute(
        'SELECT v.player,v.digest,v.build,b.deck_type,b.classification_status,b.classification_source '
        'FROM stat_views v JOIN stat_games g ON g.digest=v.digest '
        'JOIN stat_builds b ON b.fingerprint=v.build WHERE g.expires_at>?', (float(now),))]
    inherit_first_game_types(db, all_rows)
    effective = {row['digest']: row for row in all_rows}
    for view in views.values():
        row = effective.get(view['digest'])
        if row and not view['deck_type'] and row['deck_type']:
            view['deck_type'] = row['deck_type']
            view['classification_status'] = row.get('classification_status') or view['classification_status']
            view['label_source'] = 'same_match_first_game'
    if replay_root is not None:
        # 前100 与玩家排名页完全同口径：取排名页实际使用的有效视角（含首局继承）。
        from .top100 import eligible_views
        state = eligible_views(db, replay_root, now)
        eligible_rows = state.get('views') or []
        if eligible_rows:
            from .statistics_population import latest_views as common_latest
            selected = common_latest({'views': eligible_rows})
            latest = {}
            for row in selected.values():
                name = row['player'].strip()
                latest[name] = dict(player=name, source_player=row['player'], build=row['build'], digest=row['digest'],
                                    recorded_at=row['recorded_at'], deck_type=row['deck_type'],
                                    classification_status=row['classification_status'],
                                    classification_source=row.get('classification_source'),
                                    label_source='same_match_first_game'
                                    if row.get('classification_source') == 'same_match_first_game' else None)
            for name in _roster(db):
                if name in latest:
                    views[name] = latest[name]
                else:
                    views.pop(name, None)
    return _apply_similar_names(db, views, only)


def _apply_similar_names(db, views, only=None):
    """按“卡组构成”匹配人工命名；构筑自身 manual 命名的优先保留。

    only 为 None 时对所有玩家计算；传入集合时只算这些玩家（如 scope=100）。
    """
    from .statistics_deck_labels import labels_for_builds
    targets = [view for name, view in views.items() if only is None or name in only]
    labels = labels_for_builds(db, [v['build'] for v in targets])
    for view in targets:
        if view.get('classification_source') == 'manual':
            continue
        # 自动命名按组合规则产生，优先于旧人工名库的相似度标签，保证名字统一。
        if view.get('classification_source') == 'auto_series':
            continue
        found = labels.get(view['build'])
        if found:
            view['deck_type'] = found[0]
            view['classification_status'] = 'recognized'
            view['label_source'] = 'manual_similarity'
            view['label_score'] = round(found[1], 3)
    return views


def _roster(db):
    return {row['player'].strip(): dict(row) for row in db.execute(
        'SELECT rank,player,points FROM stat_current_top100 ORDER BY rank')}


def _chunks(values, size=400):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _valid_build_views(db, replay_root, views):
    """只保留“有效构筑”：未被排除、源录像仍在、主卡非空。"""
    if not views:
        return {}
    from .share import safe_replay_file
    files = {}
    for chunk in _chunks({view['digest'] for view in views.values()}):
        marks = ','.join('?' for _ in chunk)
        for row in db.execute('SELECT f.digest,r.rel_path FROM stat_files f JOIN replays r ON r.id=f.replay_id '
                              'WHERE f.digest IN (%s)' % marks, chunk):
            files.setdefault(row['digest'], []).append(row['rel_path'])
    excluded = set()
    for row in db.execute("SELECT digest,player FROM stat_exclusions WHERE scope='default'"):
        excluded.add((row['digest'], None if row['player'] == '' else row['player']))
    main_builds = set()
    for chunk in _chunks({view['build'] for view in views.values()}):
        marks = ','.join('?' for _ in chunk)
        for row in db.execute("SELECT DISTINCT build FROM stat_build_cards WHERE partition='main' "
                              "AND copies>0 AND build IN (%s)" % marks, chunk):
            main_builds.add(row[0])
    result = {}
    for name, view in views.items():
        digest = view['digest']
        if (digest, None) in excluded or (digest, name) in excluded:
            continue
        if not any(safe_replay_file(replay_root, path) for path in files.get(digest, ())):
            continue
        if view['build'] not in main_builds:
            continue
        result[name] = view
    return result


def _row(name, member, view, rank):
    return dict(rank=rank, player=name,
                points=member['points'] if member else None,
                observed=bool(view),
                deck_type=view['deck_type'] if view else None,
                build=view['build'] if view else None,
                recorded_at=view['recorded_at'] if view else None,
                classification_status=view['classification_status'] if view else None,
                label_source=view.get('label_source') if view else None,
                label_score=view.get('label_score') if view else None,
                in_top100=bool(member))


_LIST_CACHE = {}


def list_rows(db, now, scope='100', query='', replay_root=None, deck_query=''):
    """scope=100/200/all/unnamed；query/玩家名、deck_query/卡组名非空时跨全部已知玩家搜索。"""
    scope = str(scope or '100')
    if scope not in ('100', '200', 'all', 'unnamed'):
        raise ValueError('范围无效。')
    query = (query or '').strip()
    deck_query = (deck_query or '').strip()
    if len(query) > 128 or len(deck_query) > 128:
        raise ValueError('搜索词过长。')
    import os
    db_path = db.execute('PRAGMA database_list').fetchone()[2] or ''
    try:
        stamp = os.stat(db_path).st_mtime_ns
    except OSError:
        stamp = 0
    import time as _time
    cache_key = (scope, query, deck_query, str(replay_root))
    cached = _LIST_CACHE.get(cache_key)
    if cached is not None and _time.monotonic() - cached[0] <= 300:
        return cached[1]
    roster = _roster(db)
    views = _latest_views(db, now, replay_root if scope == 'unnamed' else None,
                          only=set(roster) if scope == '100' else None)
    names = set(roster) | set(views)
    searched = bool(query or deck_query)
    if deck_query:
        lowered_deck = deck_query.lower()
        names = {name for name in names
                 if lowered_deck in ((views.get(name) or {}).get('deck_type') or '').lower()}
        if scope == '100':
            names &= set(roster)
        if query:
            lowered = query.lower()
            names = {name for name in names if lowered in name.lower()}
    elif query:
        lowered = query.lower()
        names = {name for name in names if lowered in name.lower()}
    elif scope == '100':
        names = set(roster)
    elif scope == 'unnamed':
        # 所有对局里出现过、有“有效构筑”但还没有卡组名的玩家（不限于前100），按排名排序。
        if replay_root is None:
            raise ValueError('缺少录像根目录。')
        valid = _valid_build_views(db, replay_root,
                                   {name: view for name, view in views.items() if not view['deck_type']})
        names = set(valid)
    outside = sorted(name for name in names if name not in roster)
    extra = {}
    if outside and (searched or scope in ('200', 'unnamed')):
        from pathlib import Path
        from .statistics_rank_scope import ranks_for
        from .statistics_rank_cache import load as cache_load, update as cache_update
        data_dir = Path(db.execute('PRAGMA database_list').fetchone()[2]).parent
        lookup = {name: (views.get(name, {}).get('source_player')
                         or views.get(name, {}).get('player') or name)
                  for name in outside}
        cached = cache_load(data_dir, now)
        extra = {}
        for name, source in lookup.items():
            item = cached.get(source) or cached.get(source.strip()) or cached.get(name)
            if item:
                extra[name] = item[0]
        fresh = ranks_for(sorted(set(lookup.values())), now)
        if fresh:
            cache_update(data_dir, fresh, now)
            for name, source in lookup.items():
                if fresh.get(source):
                    extra[name] = fresh[source]
    if not searched and scope == '200':
        names = {name for name in names if name in roster or (extra.get(name) or 10 ** 6) <= 200}
    rows = []
    for name in names:
        member = roster.get(name)
        value = member['rank'] if member else extra.get(name)
        rank = value if value and value > 0 else None
        rows.append(_row(name, member, views.get(name), rank))
    if deck_query and scope == '200':
        rows = [row for row in rows if row['rank'] is not None and row['rank'] <= 200]
    rows.sort(key=lambda r: (r['rank'] is None, r['rank'] or 0, r['player']))
    total = len(rows)
    rows = rows[:MAX_ROWS]
    result = dict(version=2, scope='search' if searched else scope, query=query, deck_query=deck_query, total=total,
                  shown=len(rows), truncated=total > len(rows),
                  observed=sum(1 for r in rows if r['observed']),
                  recognized=sum(1 for r in rows if r['deck_type']),
                  rows=rows)
    _LIST_CACHE[cache_key] = (_time.monotonic(), result)
    if len(_LIST_CACHE) > 32:
        _LIST_CACHE.clear()
    return result


def _names(data_dir):
    try:
        from .statistics_card_names import index
        return index(data_dir)
    except Exception:
        return {}


def detail(db, data_dir, now, player, replay_root=None):
    """某个玩家最新有效构筑的卡表、当前标签与系列；榜外已采集玩家也可查。"""
    player = (player or '').strip()
    if not player:
        raise ValueError('player_required')
    views = _latest_views(db, now, None, only={player})
    view = views.get(player)
    member = db.execute('SELECT rank,player,points FROM stat_current_top100 WHERE player=?',
                        (player,)).fetchone()
    if view is None and member is None:
        raise LookupError('not_found')
    if member is not None:
        rank = member['rank']
        points = member['points']
    else:
        from .statistics_rank_scope import ranks_for
        rank = ranks_for([player], now).get(player)
        rank = rank if rank and rank > 0 else None
        points = None
    if view is None:
        return dict(player=player, rank=rank, points=points,
                    observed=False, deck_type=None, build=None,
                    recorded_at=None, cards={'main': [], 'extra': [], 'side': []}, series=[],
                    inferred_side={'cards': [], 'conflict': False, 'reason': None, 'fallback': False})
    names = _names(data_dir)
    cards = {'main': [], 'extra': [], 'side': []}
    for item in db.execute("SELECT code,copies,partition FROM stat_build_cards WHERE build=? "
                           "AND copies>0 ORDER BY partition,code", (view['build'],)):
        entry = names.get(str(item['code'])) or {}
        cards.setdefault(item['partition'], []).append(dict(
            code=item['code'], copies=item['copies'],
            name=entry.get('cn_name') or entry.get('name') or str(item['code'])))
    series = [r[0] for r in db.execute(
        'SELECT series FROM stat_build_series WHERE build=? ORDER BY series', (view['build'],))]
    field_breakdown = []
    try:
        from pathlib import Path as _Path
        from . import series_naming as _naming
        config = _naming.load_config(data_dir)
        catalog = _naming.reference_catalog(_Path(data_dir) / 'card-series-reference.json')
        registry = _naming.build_registry(catalog, config, names)
        rows = _naming.field_contributors(
            [dict(code=card['code'], copies=card['copies']) for card in cards['main']],
            catalog, config, names, registry)
        names_by_code = {card['code']: card['name'] for card in cards['main']}
        for row in rows:
            row['cards'] = [dict(item, name=names_by_code.get(item['code'], str(item['code'])))
                            for item in row['cards']]
            field_breakdown.append(row)
    except Exception:
        field_breakdown = []
    inferred = {'cards': [], 'conflict': False, 'reason': None, 'fallback': False}
    if replay_root is not None:
        try:
            from .statistics_build_detail import query as build_query
            info = build_query(db, replay_root, now, view['player'], view['digest'])
            side = (info or {}).get('inferred_side') or {}
            for card in side.get('cards') or []:
                entry = names.get(str(card['code'])) or {}
                inferred['cards'].append(dict(
                    code=card['code'], copies=card['copies'],
                    name=entry.get('cn_name') or entry.get('name') or str(card['code'])))
            inferred['conflict'] = bool(side.get('conflict'))
            inferred['reason'] = side.get('reason')
        except (ValueError, LookupError, OSError):
            pass
    return dict(player=view['player'], rank=rank, points=points, observed=True,
                deck_type=view['deck_type'], build=view['build'],
                recorded_at=view['recorded_at'],
                recorded_text=datetime.fromtimestamp(view['recorded_at'], CST).strftime('%m-%d %H:%M')
                if view.get('recorded_at') else None,
                classification_status=view['classification_status'],
                label_source=view.get('label_source'),
                label_score=view.get('label_score'),
                cards=cards, series=series, inferred_side=inferred,
                field_breakdown=field_breakdown)


def apply_name(db, build, deck, player=None):
    """把一个 build 的卡组名写成 manual；返回旧名、是否变化、系列与修订号。"""
    if not isinstance(build, str) or len(build) != 64 or any(c not in '0123456789abcdef' for c in build):
        raise ValueError('invalid_build')
    if not isinstance(deck, str) or not deck.strip() or len(deck.strip()) > 120:
        raise ValueError('invalid_deck')
    label = deck.strip()
    row = db.execute('SELECT deck_type,classification_version FROM stat_builds WHERE fingerprint=?',
                     (build,)).fetchone()
    if row is None:
        raise LookupError('build_missing')
    series = [r[0] for r in db.execute(
        'SELECT series FROM stat_build_series WHERE build=? ORDER BY series', (build,))]
    version = row['classification_version'] or 'manual'
    from .build_classification import assign
    changed = assign(db, build, label, series, version, source='manual')
    revision = db.execute('SELECT revision FROM stat_classification_state').fetchone()[0]
    from .statistics_deck_labels import remember
    remember(db, build, label)
    return dict(changed=bool(changed), old=row['deck_type'], deck=label,
                build=build, series=series, revision=revision)
