"""经过实际卡名和效果核对的异画映射；原始事实不改写。"""
import json
from pathlib import Path

_cache={}


def identity(db,code):
    exact=dict(requested_card=code,canonical_card=code,codes=[code],status='exact',reference_version=None)
    database=next((r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'),None)
    if not database:return exact
    path=Path(database).parent/'card-equivalence.json'
    if not path.is_file():return exact
    try:
        stamp=(path.stat().st_mtime_ns,path.stat().st_size)
        key=str(path.resolve())
        if key not in _cache or _cache[key][0]!=stamp:
            value=json.loads(path.read_text(encoding='utf-8'))
            if value['version']!=1:raise ValueError('unsupported_identity_reference')
            index={}
            for group in value['groups']:
                canonical=group['canonical'];codes=group['codes']
                if type(canonical) is not int or canonical not in codes or not codes or len(codes)>1000:
                    raise ValueError('invalid_identity_group')
                for member in codes:
                    if type(member) is not int or member<=0 or member in index:raise ValueError('overlapping_identity_group')
                    index[member]=(canonical,sorted(codes))
            _cache[key]=(stamp,index,value.get('reference_version'))
        _,index,version=_cache[key]
        if code in index:
            canonical,codes=index[code]
            return dict(requested_card=code,canonical_card=canonical,codes=codes,status='verified_same_name_effect',reference_version=version)
        return dict(exact,reference_version=version)
    except (OSError,ValueError,KeyError,TypeError):return dict(exact,status='invalid_reference_exact_only')


def facts(db,codes,partition):
    placeholders=','.join('?' for _ in codes)
    groups={}
    for row in db.execute(f'SELECT * FROM stat_card_facts WHERE code IN ({placeholders}) AND partition=?',[*codes,partition]):
        groups.setdefault((row['digest'],row['player']),[]).append(dict(row))
    result={}
    for key,rows in groups.items():
        merged={k:rows[0][k] for k in ('digest','player','partition')}
        for field in ('drawn','used','used_after_draw','appeared'):
            values=[r[field] for r in rows]
            merged[field]=1 if 1 in values else 0 if all(v==0 for v in values) else None
        for field in ('draw_count','initial_count','use_count','appearance_count'):
            values=[r[field] for r in rows]
            merged[field]=sum(values) if all(v is not None for v in values) else None
        # 两种异画之间的抽到／使用先后，旧原子字段不足以证明跨卡号顺序。
        if len(rows)>1 and merged['drawn']==1 and merged['used']==1 and merged['used_after_draw']!=1:
            merged['used_after_draw']=None
        result[key]=merged
    return result
