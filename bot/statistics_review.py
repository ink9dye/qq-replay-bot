"""按当前玩家最新实际构筑人工复核；只写分类元数据，不改原事实和期限。"""
import hashlib,json
from .statistics_dashboard import population
from .build_classification import assign


class Conflict(ValueError):pass


def signature(db,build):
    row=db.execute('SELECT fingerprint,deck_type,classification_version,classification_source,classification_status,series_status,classification_score FROM stat_builds WHERE fingerprint=?',(build,)).fetchone()
    tags=[r[0] for r in db.execute('SELECT series FROM stat_build_series WHERE build=? ORDER BY series',(build,))]
    return hashlib.sha256(json.dumps([list(row),tags],ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def query(db,replay_root,now,include_recognized=False):
    basis,latest,people=population(db,replay_root,now);groups={}
    for player,row in latest.items():
        if not include_recognized and row['deck_type'] is not None:continue
        item=groups.setdefault(row['build'],dict(build=row['build'],deck_type=row['deck_type'],series=row['series'],series_status=row['series_status'],
            signature=signature(db,row['build']),classification_source=row['classification_source'],people=[],highest_rank=row['rank']))
        item['people'].append(dict(player=player,rank=row['rank'],digest=row['digest'],recorded_at=row['recorded_at'],outcome=row['outcome'],first_player=row['first_player']))
        item['highest_rank']=min(item['highest_rank'],row['rank'])
    items=sorted(groups.values(),key=lambda i:(i['deck_type'] is not None,-len(i['people']),i['highest_rank']))
    return dict(generated_at=now,roster={k:v for k,v in basis['roster'].items() if k!='members'},
        covered_players=len(latest),unknown_players=sum(row['deck_type'] is None for row in latest.values()),
        items=items,include_recognized=include_recognized,classification_revision=basis['classification_revision'])


def apply(db,replay_root,now,build,expected_signature,deck_type):
    if not isinstance(build,str) or not isinstance(expected_signature,str) or not isinstance(deck_type,str) or not deck_type.strip() or len(deck_type)>120:raise ValueError('invalid_classification')
    current=query(db,replay_root,now,True)
    item=next((i for i in current['items'] if i['build']==build),None)
    if item is None or item['signature']!=expected_signature:raise Conflict('classification_changed_or_source_no_longer_current')
    changed=assign(db,build,deck_type.strip(),item['series'],'owner-web-review-v1','manual')
    # 仅修改类型；系列沿用本次已核验的实际卡片标签和完整度，已知空集不降级为未知。
    db.execute('UPDATE stat_builds SET series_status=? WHERE fingerprint=?',(item['series_status'],build))
    affected=db.execute('SELECT COUNT(*) FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE v.build=? AND g.expires_at>?',(build,now)).fetchone()[0]
    return dict(ok=True,changed=changed,affected_unexpired_views=affected,classification_revision=db.execute('SELECT revision FROM stat_classification_state WHERE singleton=1').fetchone()[0])
