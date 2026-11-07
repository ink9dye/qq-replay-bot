"""只读的当前榜单与每人最新有效构筑；不生成长期事实副本。"""
from pathlib import Path
import time
from .top100 import eligible_views
from .statistics_match_identity import identity_metadata

RANK_MAX_AGE_SECONDS = 3600


def _data_dir(db):
    return Path(db.execute('PRAGMA database_list').fetchone()[2]).parent


def resolve_ranks(db, people, now):
    """给缺失名次的玩家查官方名次；先用本地缓存（1 小时内），再走有界查询。"""
    missing = [p for p in people if not (isinstance(p.get('rank'), int) and p['rank'] > 0)]
    if not missing:
        return
    from .statistics_rank_cache import load as cache_load, update as cache_update
    from .statistics_rank_scope import ranks_for
    cached = cache_load(_data_dir(db), now)
    need = []
    for person in missing:
        name = str(person['player'])
        item = cached.get(name) or cached.get(name.strip())
        if item and 0 <= float(now) - item[1] <= RANK_MAX_AGE_SECONDS and item[0] > 0:
            person['rank'] = item[0]
        else:
            need.append(name)
    if not need:
        return
    fresh = ranks_for(sorted(set(need)), now)
    if fresh:
        cache_update(_data_dir(db), fresh, now)
        for person in missing:
            value = fresh.get(str(person['player']))
            if value and value > 0:
                person['rank'] = value


def population(db, replay_root, now, player=None, allowed_digests=None, resolve_rank=True):
    basis=eligible_views(db,replay_root,now,player=player,check_files=False)
    if allowed_digests is not None:basis['views']=[r for r in basis['views'] if r['digest'] in allowed_digests]
    from .statistics_population import latest_views
    # Naming uses trimmed display/lookup keys, while SQL and evidence keep the
    # original player spelling. Apply the same boundary normalization here.
    latest={name.strip():row for name,row in latest_views(basis).items()}
    metadata=identity_metadata(db,{r['digest'] for r in latest.values()})
    people=[]
    for member in basis['roster']['members']:
        name=member['player'].strip()
        row=latest.get(name);person=dict(player=name,rank=member['rank'],points=member['points'],observed=False)
        if row:
            meta=metadata.get(row['digest'])
            person.update(observed=True,deck_type=row['deck_type'],classification_status=row['classification_status'],
                label_source=row.get('label_source'),label_score=row.get('label_score'),
                series=row['series'],series_status=row['series_status'],recorded_at=row['recorded_at'],expires_at=row['expires_at'],
                observation_kind='confirmed_first_game' if meta and meta[1:]==(1,True) else 'post_side' if meta and meta[1]>1 else 'initial_status_unknown')
        people.append(person)
    if player is not None:player=player.strip()
    if player is not None and player not in {p['player'] for p in people} and player in latest:
        row=latest[player];meta=metadata.get(row['digest'])
        people.append(dict(player=player,rank=None,points=None,observed=True,deck_type=row['deck_type'],
            classification_status=row['classification_status'],series=row['series'],series_status=row['series_status'],
            recorded_at=row['recorded_at'],expires_at=row['expires_at'],
            observation_kind='confirmed_first_game' if meta and meta[1:]==(1,True) else 'post_side' if meta and meta[1]>1 else 'initial_status_unknown'))
    if resolve_rank:
        resolve_ranks(db, people, now)
    return basis,latest,people


def search_players(db,replay_root,now,text):
    if not text or len(text)>128:raise ValueError('invalid_search')
    needle=text.casefold()
    names={r[0]for r in db.execute('SELECT player FROM stat_current_top100 UNION SELECT player FROM subscriptions UNION SELECT DISTINCT player FROM stat_views')if needle in r[0].casefold()}
    result=[]
    for name in sorted(names)[:30]:
        _,latest,people=population(db,replay_root,now,name,resolve_rank=False)
        person=next((p for p in people if p['player']==name.strip()),dict(player=name.strip(),rank=None,points=None,observed=False))
        result.append(person)
    resolve_ranks(db,result,now)
    return dict(people=result,limited=len(names)>30)


_DECK_IMAGE_CACHE = {}   # deck_type -> (代表卡 code, 计算时间)
_DECK_IMAGE_TTL = 1800
_DECK_IMAGE_LOADED = False


def _deck_image_file(db):
    try:
        return _data_dir(db) / 'cache' / 'deck-images.json'
    except Exception:
        return None


def _load_deck_images(db):
    global _DECK_IMAGE_LOADED
    if _DECK_IMAGE_LOADED:
        return
    _DECK_IMAGE_LOADED = True
    path = _deck_image_file(db)
    if path is None or not path.is_file():
        return
    try:
        import json
        payload = json.loads(path.read_text(encoding='utf-8'))
        for label, item in (payload.get('labels') or {}).items():
            if isinstance(item, list) and len(item) == 2:
                _DECK_IMAGE_CACHE[str(label)] = (int(item[0]), float(item[1]))
    except (OSError, ValueError, TypeError):
        _DECK_IMAGE_CACHE.clear()


def _save_deck_images(db):
    path = _deck_image_file(db)
    if path is None:
        return
    try:
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        labels = {name: [item[0], item[1]] for name, item in _DECK_IMAGE_CACHE.items()
                  if now - item[1] <= _DECK_IMAGE_TTL * 2}
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(saved=now, labels=labels), ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
    except OSError:
        pass


def deck_images(db, names):
    """每个精确卡组名选一张代表卡：该卡组构筑里总投入最多的主卡。

    按卡组名逐项缓存（30 分钟）：新出现的卡组只补算缺失项，
    避免每次刷新都重跑全表的 15s 聚合。展示用，不参与任何资格判断。
    """
    wanted = {str(name) for name in names}
    if not wanted:
        return {}
    _load_deck_images(db)
    now = time.time()
    missing = sorted(label for label in wanted
                     if label not in _DECK_IMAGE_CACHE
                     or now - _DECK_IMAGE_CACHE[label][1] > _DECK_IMAGE_TTL)
    if missing:
        clause = 'b.deck_type IN (' + ','.join('?' for _ in missing) + ')'
        if '未识别' in missing:
            clause += " OR b.deck_type IS NULL OR b.deck_type=''"
        best = {}
        rows = db.execute(
            "SELECT COALESCE(b.deck_type,'') AS deck_type, c.code, SUM(c.copies) AS copies "
            "FROM stat_builds b JOIN stat_build_cards c ON c.build=b.fingerprint "
            "WHERE c.partition='main' AND (" + clause + ") GROUP BY deck_type, c.code",
            missing)
        for deck_type, code, copies in rows:
            key = '未识别' if not deck_type else str(deck_type)
            old = best.get(key)
            if old is None or copies > old[1] or (copies == old[1] and code < old[0]):
                best[key] = (int(code), int(copies))
        for label, item in best.items():
            _DECK_IMAGE_CACHE[label] = (item[0], now)
        _save_deck_images(db)
    return {name: _DECK_IMAGE_CACHE[name][0] for name in wanted if name in _DECK_IMAGE_CACHE}


def query(db, replay_root, now, player=None):
    # 单玩家页只需要该玩家的视角；旧实现每次都算完整名册（100 人 + 名次核验），
    # 单页 2-18s。这里按需缩小，展示与资格规则不变。
    if player is not None:
        basis,latest,people=population(db,replay_root,now,player=player,resolve_rank=False)
    else:
        basis,latest,people=population(db,replay_root,now)
    if player is not None:
        player=player.strip()
        row=latest.get(player)
        if row is None or row.get('rank') is None:return None
        # 与旧实现一致：只有当前榜单内的玩家才有构筑页（非榜单玩家仍返回空/404）。
        cards=[dict(r) for r in db.execute('SELECT partition,code,copies FROM stat_build_cards WHERE build=? ORDER BY partition,code',(row['build'],))]
        person=next(p for p in people if p['player']==player)
        if not (isinstance(person.get('rank'),int) and person['rank']>0):
            resolve_ranks(db,[person],now)
        return dict(person=person,cards=cards,
            completeness={p:bool(row[p+'_complete']) for p in ('main','extra','side')},
            source=dict(digest=row['digest'],recorded_at=row['recorded_at'],expires_at=row['expires_at'],
                outcome=row['outcome'],first_player=row['first_player'],
                players=[r[0] for r in db.execute('SELECT player FROM stat_views WHERE digest=? ORDER BY player_index',(row['digest'],))]),
            roster_version=basis['roster']['version'])
    types={}
    for person in people:
        if person['observed']:
            name=person['deck_type'] or '未识别'
            types[name]=types.get(name,0)+1
    distribution = [dict(name=k, players=v) for k, v in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))]
    # 每个卡组名里各字段实际投入的卡数（主卡+额外）：嵌套小类的代表卡按“卡数最多的字段”取。
    deck_fields = {}
    for row in latest.values():
        deck = row.get('deck_type') or '未识别'
        counts = deck_fields.setdefault(deck, {})
        for field, item in (row.get('series_membership') or {}).items():
            counts[field] = counts.get(field, 0) + int(item.get('main_copies') or 0) + int(item.get('extra_copies') or 0)
    try:
        from .deck_pie import from_data_dir, single_from_data_dir
        images = deck_images(db, types)
        deck_pie = from_data_dir(distribution, _data_dir(db), deck_images=images, deck_fields=deck_fields)
        deck_single = single_from_data_dir(distribution, _data_dir(db), deck_images=images, deck_fields=deck_fields)
    except Exception:
        deck_pie = None
        deck_single = None
    return dict(generated_at=now,roster={k:v for k,v in basis['roster'].items() if k!='members'},
        current_players=len(people),covered_players=len(latest),missing_players=len(people)-len(latest),
        unidentified_players=sum(p['observed'] and not p['deck_type'] for p in people),people=people,
        deck_distribution=distribution,deck_pie=deck_pie,deck_single=deck_single,
        data_quality=basis['diagnostics'],series_reference=basis['series_reference'],classification_revision=basis['classification_revision'])
