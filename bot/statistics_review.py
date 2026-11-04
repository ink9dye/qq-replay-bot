"""按当前玩家最新实际构筑只读核对；不再支持人工改名。"""
import hashlib,json
from .statistics_dashboard import population


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


def apply(*args, **kwargs):
    """历史复核入口已退役；分类只由字段规则生成。"""
    raise ValueError('manual_naming_removed')
