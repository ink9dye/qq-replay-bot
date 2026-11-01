"""只读的当前榜单与每人最新有效构筑；不生成长期事实副本。"""
from pathlib import Path
from .top100 import eligible_views
from .player_statistics import capture_metadata

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
    basis=eligible_views(db,replay_root,now,player=player)
    if allowed_digests is not None:basis['views']=[r for r in basis['views'] if r['digest'] in allowed_digests]
    from .statistics_population import latest_views
    latest=latest_views(basis)
    # 与卡组命名页同一套“卡组名字库”相似度；构筑自身 manual 仍优先。
    from .statistics_deck_labels import labels_for_builds
    similar=labels_for_builds(db,[r['build'] for r in latest.values()])
    for row in latest.values():
        if row.get('classification_source')=='manual':continue
        found=similar.get(row['build'])
        if found:
            row['deck_type']=found[0]
            row['classification_status']='recognized'
            row['label_source']='manual_similarity'
            row['label_score']=round(found[1],3)
    metadata=capture_metadata(db,{r['digest'] for r in latest.values()})
    people=[]
    for member in basis['roster']['members']:
        row=latest.get(member['player']);person=dict(player=member['player'],rank=member['rank'],points=member['points'],observed=False)
        if row:
            meta=metadata.get(row['digest'])
            person.update(observed=True,deck_type=row['deck_type'],classification_status=row['classification_status'],
                label_source=row.get('label_source'),label_score=row.get('label_score'),
                series=row['series'],series_status=row['series_status'],recorded_at=row['recorded_at'],expires_at=row['expires_at'],
                observation_kind='confirmed_first_game' if meta and meta[1:]==(1,True) else 'post_side' if meta and meta[1]>1 else 'initial_status_unknown')
        people.append(person)
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
        person=next((p for p in people if p['player']==name),dict(player=name,rank=None,points=None,observed=False))
        result.append(person)
    resolve_ranks(db,result,now)
    return dict(people=result,limited=len(names)>30)


def query(db, replay_root, now, player=None):
    basis,latest,people=population(db,replay_root,now)
    if player is not None:
        row=latest.get(player)
        if row is None:return None
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
    return dict(generated_at=now,roster={k:v for k,v in basis['roster'].items() if k!='members'},
        current_players=len(people),covered_players=len(latest),missing_players=len(people)-len(latest),
        unidentified_players=sum(p['observed'] and not p['deck_type'] for p in people),people=people,
        deck_distribution=[dict(name=k,players=v) for k,v in sorted(types.items(),key=lambda kv:(-kv[1],kv[0]))],
        data_quality=basis['diagnostics'],series_reference=basis['series_reference'],classification_revision=basis['classification_revision'])
