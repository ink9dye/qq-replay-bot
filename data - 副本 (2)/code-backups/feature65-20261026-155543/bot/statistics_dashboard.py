"""只读的当前榜单与每人最新有效构筑；不生成长期事实副本。"""
from .top100 import eligible_views
from .player_statistics import capture_metadata


def population(db, replay_root, now):
    basis=eligible_views(db,replay_root,now)
    latest={}
    for row in basis['views']:
        old=latest.get(row['player'])
        if old is None or (row['recorded_at'],row['received_at'],row['digest'])>(old['recorded_at'],old['received_at'],old['digest']):
            latest[row['player']]=row
    metadata=capture_metadata(db,{r['digest'] for r in latest.values()})
    people=[]
    for member in basis['roster']['members']:
        row=latest.get(member['player']);person=dict(player=member['player'],rank=member['rank'],points=member['points'],observed=False)
        if row:
            meta=metadata.get(row['digest'])
            person.update(observed=True,deck_type=row['deck_type'],classification_status=row['classification_status'],
                series=row['series'],series_status=row['series_status'],recorded_at=row['recorded_at'],expires_at=row['expires_at'],
                observation_kind='confirmed_first_game' if meta and meta[1:]==(1,True) else 'post_side' if meta and meta[1]>1 else 'initial_status_unknown')
        people.append(person)
    return basis,latest,people


def query(db, replay_root, now, player=None):
    basis,latest,people=population(db,replay_root,now)
    if player is not None:
        row=latest.get(player)
        if row is None:return None
        cards=[dict(r) for r in db.execute('SELECT partition,code,copies FROM stat_build_cards WHERE build=? ORDER BY partition,code',(row['build'],))]
        return dict(person=next(p for p in people if p['player']==player),cards=cards,
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
