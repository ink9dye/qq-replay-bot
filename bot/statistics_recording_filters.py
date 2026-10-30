"""Read-only recording filters and batch draw-win rankings share eligible facts."""
from .top100 import eligible_views
from .card_identity import reference
from .card_statistics import win_rate
from datetime import datetime


def snapshot(db,root,now,include_opponents=False):
    basis=eligible_views(db,root,now)
    rows=basis['views']; ids={(r['digest'],r['player']):r for r in rows}
    if include_opponents:
        digests={r['digest'] for r in rows}
        # Opponent context of an eligible game never expands the candidate pool.
        for r in db.execute("SELECT v.* FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE g.expires_at>? AND NOT EXISTS(SELECT 1 FROM stat_exclusions e WHERE e.digest=v.digest AND (e.player='' OR e.player=v.player) AND e.scope='default')",(now,)):
            if r['digest'] in digests:ids[(r['digest'],r['player'])]=dict(r)
        basis['_opponent_views']=ids
    from .card_series import reference as series_reference,annotate as annotate_series
    annotate_series(db,list(ids.values()),series_reference(db))
    mapping,_,_=reference(db); canonical=lambda c:mapping.get(c,(c,))[0]
    carried={};all_carried={}
    for r in db.execute("SELECT build,partition,code FROM stat_build_cards WHERE copies>0"):
        code=canonical(r['code']);all_carried.setdefault(r['build'],set()).add(code)
        if r['partition']=='main':carried.setdefault(r['build'],set()).add(code)
    draws={k:set() for k in ids};used={k:set() for k in ids}
    for f in db.execute('SELECT digest,player,code,drawn,used FROM stat_card_facts'):
        key=(f['digest'],f['player']);row=ids.get(key)
        if row is None:continue
        code=canonical(f['code'])
        if f['drawn']==1 and code in carried.get(row['build'],set()):draws[key].add(code)
        if f['used']==1 and code in all_carried.get(row['build'],set()):used[key].add(code)
    opponents={}
    for r in db.execute('SELECT v.digest,v.player,b.deck_type FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build'):
        opponents.setdefault(r['digest'],[]).append(dict(r))
    for row in rows:
        other=[r for r in opponents.get(row['digest'],[])if r['player']!=row['player']]
        row['opponent']=other[0]['player']if len(other)==1 else None
        row['opponent_deck_type']=other[0]['deck_type']if len(other)==1 else None
    return basis,rows,draws,used,carried,canonical


def _draw_rankings(context):
    _,rows,draws,_,_,_=context
    pools={}
    for row in rows:
        for code in draws[(row['digest'],row['player'])]:pools.setdefault(code,[]).append(row)
    return {code:dict(overall=win_rate(pool),first=win_rate([r for r in pool if r['first_player']==1]),
        second=win_rate([r for r in pool if r['first_player']==0]))for code,pool in pools.items()}


def _carried_game_counts(db,context):
    """Known-result player/game samples; repeated copies/partitions count once."""
    _,rows,_,_,_,canonical=context
    builds={}
    for card in db.execute('SELECT build,code FROM stat_build_cards WHERE copies>0'):
        builds.setdefault(card['build'],set()).add(canonical(card['code']))
    counts={}
    for row in rows:
        if row['outcome'] not in ('win','loss','draw'):continue
        for code in builds.get(row['build'],set()):counts[code]=counts.get(code,0)+1
    return counts


def _in_current_top100(value):
    try:return 0<int(value)<=100
    except (TypeError,ValueError):return False


def _sample_baselines(context):
    """Known-result player small-game views per ranking column (the N in n/N)."""
    _,rows,_,_,_,_=context
    known=[r for r in rows if r['outcome'] in ('win','loss','draw')]
    return dict(overall=len(known),
                first=sum(r['first_player']==1 for r in known),
                second=sum(r['first_player']==0 for r in known))


def draw_rankings(db,root,now):
    return _draw_rankings(snapshot(db,root,now))


def carried_game_counts(db,root,now):
    return _carried_game_counts(db,snapshot(db,root,now))


def sample_baselines(db,root,now):
    return _sample_baselines(snapshot(db,root,now))


def card_sample_statistics(db,root,now,deck_types=(),include_carried=False):
    """Share one eligible-fact snapshot within this request only; never cache it.

    Each later request recomputes expiry, roster, exclusions and source existence.
    """
    context=snapshot(db,root,now)
    deck_choices=sorted({row['deck_type'] for row in context[1] if row['deck_type']})
    if deck_types:
        context=(context[0],[row for row in context[1] if row['deck_type'] in deck_types],*context[2:])
    result=dict(draw_rankings=_draw_rankings(context),
                carried_game_counts=_carried_game_counts(db,context),
                sample_baselines=_sample_baselines(context))
    if include_carried:
        result['deck_choices']=deck_choices
        by_build={}
        for card in db.execute('SELECT build,partition,code FROM stat_build_cards WHERE copies>0'):
            by_build.setdefault(card['build'],set()).add((context[5](card['code']),card['partition']))
        pools={}
        for row in context[1]:
            for code,part in by_build.get(row['build'],()):pools.setdefault((code,part),[]).append(row)
        rankings={}
        for (code,part),pool in pools.items():
            rankings.setdefault(code,{})[part]=dict(overall=win_rate(pool),
                first=win_rate([r for r in pool if r['first_player']==1]),
                second=win_rate([r for r in pool if r['first_player']==0]))
        result['carried_rankings']=rankings
    return result


def _list_arg(args,name,limit=100,length=128,label='条件'):
    values=[v for v in args.get(name,[]) if v!='']
    if len(values)>limit:raise ValueError(f'{label}项过多。')
    if any(not isinstance(v,str)or not v.strip()or len(v)>length for v in values):raise ValueError(f'{label}无效。')
    return values


def _scalar_arg(args,name,allowed,default=None):
    raw=[v for v in args.get(name,[]) if v!='']
    if len(raw)>1:raise ValueError(f'{name} 参数重复，无法确定唯一值。')
    value=raw[0] if raw else default
    if value is None:return None
    if value not in allowed:raise ValueError(f'{name} 取值无效。')
    return value


def _time_arg(args,name):
    raw=[v for v in args.get(name,[]) if v!='']
    if len(raw)>1:raise ValueError('时间条件重复。')
    if not raw:return None
    try:value=datetime.fromisoformat(raw[0].strip())
    except ValueError:raise ValueError('时间格式应为带时区的 ISO 8601，例如 2026-10-28T00:00:00+08:00。')
    if value.tzinfo is None:raise ValueError('时间必须包含时区偏移，例如 +08:00。')
    return value.timestamp()


def query(db,root,now,args):
    basis,rows,draws,used,carried,canonical=snapshot(db,root,now,include_opponents=True)
    from .statistics_recording_names import decorate
    decorate(db,rows,root,now)
    for row in rows:
        row['deck_type']=row.get('match_deck_type')or row['deck_type']
        row['opponent_deck_type']=row.get('opponent_match_deck_type')or row['opponent_deck_type']
    values=lambda name:args.get(name,[])
    internal=_scalar_arg(args,'internal',('0','1'),'0')
    mode=_scalar_arg(args,'mode',('drawn','not_drawn','used'),'drawn')
    match=_scalar_arg(args,'match',('any','all'),'any')
    not_match=_scalar_arg(args,'not_match',('any','all'),'all')
    opponent_mode=_scalar_arg(args,'opponent_mode',('drawn','not_drawn','used'),'drawn')
    opponent_match=_scalar_arg(args,'opponent_match',('any','all'),'any')
    opponent_not_match=_scalar_arg(args,'opponent_not_match',('any','all'),'all')
    deck_values=_list_arg(args,'deck',label='我方卡组')
    opponent_values=_list_arg(args,'opponent',label='对手卡组')
    series=_list_arg(args,'series',label='我方系列')
    opponent_series=_list_arg(args,'opponent_series',label='对手系列')
    start=_time_arg(args,'start');end=_time_arg(args,'end')
    if start is not None and end is not None and start>=end:raise ValueError('开始时间必须早于结束时间。')
    outcomes=_list_arg(args,'outcome',length=32,label='胜负')
    if any(v not in ('win','loss','draw','unknown')for v in outcomes):raise ValueError('胜负条件无效。')
    firsts=list(dict.fromkeys(_list_arg(args,'first',length=8,label='先后攻')))
    if any(v not in ('1','0','None')for v in firsts):raise ValueError('先后攻条件无效。')
    outcomes=list(dict.fromkeys(outcomes))
    # 双方都要在当前前100名单内；榜单缺失或过期时名单已由 roster 标记，不把未知当上榜。
    both_top=internal=='1'
    conditions=[]
    # 每组卡片条件：我方/对手 × 命中列表/“必须未抽到”列表，两两独立，可同时成立。
    for prefix in ('','opponent_','not_','opponent_not_'):
        codes=values(prefix+'card')
        if len(codes)>2000 or any(not c.isdigit()or not 0<int(c)<=4294967295 for c in codes):raise ValueError('invalid_cards')
        if prefix=='not_':
            # 明确指定的“没抽到”：固定按未抽到判断。
            group_mode,group_match='not_drawn',not_match
        elif prefix=='opponent_not_':
            group_mode,group_match='not_drawn',opponent_not_match
        elif prefix=='opponent_':
            group_mode,group_match=opponent_mode,opponent_match
        else:
            group_mode,group_match=mode,match
        if codes:conditions.append((prefix,{canonical(int(c))for c in codes},group_mode,group_match))
    none_keys=('deck','opponent','card','opponent_card','not_card','opponent_not_card','first','outcome')
    result=[]
    for row in rows:
        if any(args.get(key+'_none')==['1']for key in none_keys):continue
        if both_top and not (_in_current_top100(row.get('current_rank'))and _in_current_top100(row.get('opponent_current_rank'))):continue
        if start is not None and row['recorded_at']<start:continue
        if end is not None and row['recorded_at']>=end:continue
        if series and not (set(row.get('series')or ())& set(series)):continue
        if opponent_series:
            other=basis['_opponent_views'].get((row['digest'],row['opponent']))
            if other is None or not (set(other.get('series')or ())& set(opponent_series)):continue
        if deck_values and (row['deck_type']or '__unknown__')not in deck_values:continue
        if opponent_values and (row['opponent_deck_type']or '__unknown__')not in opponent_values:continue
        if values('first')and str(row['first_player'])not in values('first'):continue
        if values('outcome')and row['outcome']not in values('outcome'):continue
        accepted=True
        for prefix,codes,mode,match in conditions:
            if not codes:continue
            opponent_side=prefix.startswith('opponent')
            subject=basis['_opponent_views'].get((row['digest'],row['opponent'])) if opponent_side else row
            if subject is None:accepted=False;break
            observed=(used if mode=='used'else draws).get((subject['digest'],subject['player']),set())
            if mode=='not_drawn':
                if not subject['draw_complete']:accepted=False;break
                observed=carried.get(subject['build'],set())-observed
            if not (codes<=observed if match=='all'else bool(codes&observed)):accepted=False;break
        if not accepted:continue
        result.append({k:row.get(k)for k in ('digest','player','deck_type','opponent','opponent_deck_type','recorded_at','expires_at','first_player','outcome','recording_title','download_filename','observed_date','observed_day','observed_match_no','observed_game_no','observed_match_id','current_rank','opponent_current_rank')})
    result.sort(key=lambda r:r['recorded_at'],reverse=True)
    return dict(recording_filters_version=3,card_filters_version=2,samples=result[:2000],total=len(result),limited=len(result)>2000,roster=basis['roster'],metrics=win_rate(result))
