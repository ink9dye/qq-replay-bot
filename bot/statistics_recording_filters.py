"""Read-only recording filters and batch draw-win rankings share eligible facts."""
from .top100 import eligible_views
from .card_identity import reference
from .card_statistics import win_rate, deck_matches
from datetime import datetime


def _chunks(values, size=400):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def snapshot(db,root,now,include_opponents=False,annotate_series=True,with_facts=True,row_filter=None):
    """Shared eligible-view snapshot.

    with_facts=False 时跳过全部卡牌事实（stat_card_facts）与卡表聚合：只有带卡片
    条件的查询需要它们，录像筛选默认页不需要。口径不变，只是少读两张表。
    """
    basis=eligible_views(db,root,now,annotate_series=False)
    rows=basis['views']
    if row_filter is not None:
        rows=[row for row in rows if row_filter(row)]
    ids={(r['digest'],r['player']):r for r in rows}
    if include_opponents:
        digests={r['digest'] for r in rows}
        # Opponent context of an eligible game never expands the candidate pool.
        for chunk in _chunks(digests):
            marks=','.join('?' for _ in chunk)
            sql=("SELECT v.* FROM stat_views v JOIN stat_games g ON g.digest=v.digest "
                 f"WHERE g.expires_at>? AND v.digest IN ({marks}) "
                 "AND NOT EXISTS(SELECT 1 FROM stat_exclusions e WHERE e.digest=v.digest "
                 "AND (e.player='' OR e.player=v.player) AND e.scope='default')")
            for r in db.execute(sql,(now,*chunk)):
                ids[(r['digest'],r['player'])]=dict(r)
        basis['_opponent_views']=ids
    if annotate_series:
        from .card_series import reference as series_reference,annotate as annotate_series
        annotate_series(db,list(ids.values()),series_reference(db))
    mapping,_,_=reference(db); canonical=lambda c:mapping.get(c,(c,))[0]
    carried={};all_carried={};draws={};used={}
    digests={r['digest'] for r in ids.values()}
    if with_facts:
        builds={r['build'] for r in ids.values()}
        for chunk in _chunks(builds):
            marks=','.join('?' for _ in chunk)
            # copies>0 是表级 CHECK，过滤它只会迫使每行回表；去掉后走覆盖索引。
            for r in db.execute("SELECT build,partition,code FROM stat_build_cards "
                                f"WHERE build IN ({marks})",chunk):
                code=canonical(r['code']);all_carried.setdefault(r['build'],set()).add(code)
                if r['partition']=='main':carried.setdefault(r['build'],set()).add(code)
        draws={k:set() for k in ids};used={k:set() for k in ids}
        facts_present=set()
        for chunk in _chunks(digests):
            marks=','.join('?' for _ in chunk)
            for f in db.execute('SELECT digest,player,code,drawn,used FROM stat_card_facts '
                                f'WHERE digest IN ({marks})',chunk):
                code=canonical(f['code']);facts_present.add((f['digest'],f['player'],code))
                key=(f['digest'],f['player']);row=ids.get(key)
                if row is None:continue
                if f['drawn']==1 and code in carried.get(row['build'],set()):draws[key].add(code)
                if f['used']==1 and code in all_carried.get(row['build'],set()):used[key].add(code)
        basis['_all_carried']=all_carried
        basis['_facts_present']=facts_present
    opponents={}
    for chunk in _chunks(digests):
        marks=','.join('?' for _ in chunk)
        for r in db.execute('SELECT v.digest,v.player,b.deck_type FROM stat_views v '
                            'JOIN stat_builds b ON b.fingerprint=v.build '
                            f'WHERE v.digest IN ({marks})',chunk):
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


def query(db,root,now,args,limit=2000):
    """录像筛选入口：我方/对手排名范围（前100、前200、全部）在这里生效。

    默认我方前100、对手全部，和本页改造前的口径一致；旧链接里的 internal=1 仍然等价于双方前100。
    返回里带 rank_scope（范围、未知/核验中人数），页面用它说明"为什么少了/为什么还在核验"。
    """
    from .statistics_rank_scope import parse_ranges,population_scope
    ranges=parse_ranges(args)
    with population_scope(*ranges) as scope:
        result=_query(db,root,now,args,limit,ranges)
    result['recording_filters_version']=4
    result['rank_scope']=dict(self_range=ranges[0],opponent_range=ranges[1],
        unknown_rank_players=len(scope['unknown']),pending_rank_players=len(scope['pending']),
        complete=not scope['unknown'],rank_source='official_top100_and_per_player',rank_max_age_seconds=120)
    return result


def _query(db,root,now,args,limit=2000,ranges=None):
    """limit=None 表示返回全部命中（给打包下载用）；默认仍只回 2000 条给页面。"""
    # Match metadata is resolved by eligible_views before this filter, preserving
    # first-game identity. Expensive facts/opponent data only cover survivors.
    early_start = _time_arg(args, 'start'); early_end = _time_arg(args, 'end')
    if early_start is not None and early_end is not None and early_start >= early_end:
        raise ValueError('开始时间必须早于结束时间。')
    early_outcomes = _list_arg(args, 'outcome', length=32, label='胜负')
    if any(x not in ('win','loss','draw','unknown') for x in early_outcomes):
        raise ValueError('胜负条件无效。')
    early_firsts = _list_arg(args, 'first', length=8, label='先后攻')
    if any(x not in ('1','0','None') for x in early_firsts):
        raise ValueError('先后攻条件无效。')
    def row_filter(row):
        return ((early_start is None or row['recorded_at'] >= early_start)
                and (early_end is None or row['recorded_at'] < early_end)
                and (not early_outcomes or row['outcome'] in early_outcomes)
                and (not early_firsts or str(row['first_player']) in early_firsts))
    needs_facts=any(args.get(key) for key in ('card','opponent_card','not_card','opponent_not_card'))
    basis,rows,draws,used,carried,canonical=snapshot(db,root,now,include_opponents=True,
        annotate_series=bool(args.get('series')or args.get('opponent_series')),with_facts=needs_facts,row_filter=row_filter)
    from .statistics_recording_names import decorate
    decorate(db,rows,root,now)
    if args.get('phase_none',['0'])[0]=='1':
        rows=[]
    # 前200/全部时，自己可能不在当前前100名单里：用范围筛选给出的名次补显示字段。
    for row in rows:
        if not row.get('current_rank') and row.get('rank'):
            row['current_rank']=row['rank']
    if ranges and ranges[1]!='all':
        missing={row['opponent'] for row in rows
                 if row.get('opponent') and not row.get('opponent_current_rank')}
        if missing:
            from .statistics_rank_scope import ranks_for
            extra=ranks_for(missing,float(now))
            for row in rows:
                if not row.get('opponent_current_rank') and row.get('opponent') in extra:
                    row['opponent_current_rank']=extra[row['opponent']]
    for row in rows:
        row['deck_type']=row.get('match_deck_type')or row['deck_type']
        row['opponent_deck_type']=row.get('opponent_match_deck_type')or row['opponent_deck_type']
    # 局面多选：主卡局/Side局 × 先攻/后攻（另含先后攻未知）。
    phases=list(dict.fromkeys(v for v in args.get('phase',[]) if v))
    if phases:
        allowed={'main_first','main_second','side_first','side_second'}
        if any(v not in allowed for v in phases):raise ValueError('局面条件无效。')
        kept=[]
        for row in rows:
            first=row.get('first_player')
            order='first' if first==1 else ('second' if first==0 else None)
            if order is None:
                continue
            game=row.get('observed_game_no')
            kind='main' if game==1 else ('side' if isinstance(game,int) and game>1 else None)
            if kind and kind+'_'+order in phases:kept.append(row)
        rows=kept
    values=lambda name:args.get(name,[])
    mode=_scalar_arg(args,'mode',('drawn','not_drawn','used'),'drawn')
    match=_scalar_arg(args,'match',('any','all'),'any')
    not_match=_scalar_arg(args,'not_match',('any','all'),'all')
    opponent_mode=_scalar_arg(args,'opponent_mode',('drawn','not_drawn','used'),'drawn')
    opponent_match=_scalar_arg(args,'opponent_match',('any','all'),'any')
    opponent_not_match=_scalar_arg(args,'opponent_not_match',('any','all'),'all')
    deck_values=_list_arg(args,'deck',label='我方卡组')
    opponent_values=_list_arg(args,'opponent',label='对手卡组')
    contains=args.get('deck_match',['contains'])[0]!='exact'
    series=_list_arg(args,'series',label='我方系列')
    opponent_series=_list_arg(args,'opponent_series',label='对手系列')
    start=_time_arg(args,'start');end=_time_arg(args,'end')
    if start is not None and end is not None and start>=end:raise ValueError('开始时间必须早于结束时间。')
    outcomes=_list_arg(args,'outcome',length=32,label='胜负')
    if any(v not in ('win','loss','draw','unknown')for v in outcomes):raise ValueError('胜负条件无效。')
    firsts=list(dict.fromkeys(_list_arg(args,'first',length=8,label='先后攻')))
    if any(v not in ('1','0','None')for v in firsts):raise ValueError('先后攻条件无效。')
    outcomes=list(dict.fromkeys(outcomes))
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
    all_carried={};facts_present=set()
    if conditions:
        all_carried=basis.get('_all_carried')
        if all_carried is None:
            all_carried={}
            builds={row['build'] for row in rows}
            for chunk in _chunks(builds):
                marks=','.join('?' for _ in chunk)
                for card in db.execute('SELECT build,code FROM stat_build_cards '
                                       f'WHERE build IN ({marks})',chunk):
                    all_carried.setdefault(card['build'],set()).add(canonical(card['code']))
        facts_present=basis.get('_facts_present')
        if facts_present is None:
            facts_present=set()
            for chunk in _chunks({row['digest'] for row in rows}):
                marks=','.join('?' for _ in chunk)
                for fact in db.execute('SELECT digest,player,code FROM stat_card_facts '
                                       f'WHERE digest IN ({marks})',chunk):
                    facts_present.add((fact['digest'],fact['player'],canonical(fact['code'])))
    reasons={'opponent_evidence_missing':0,'self_evidence_missing':0,
             'draw_process_incomplete':0,'card_fact_missing':0,'condition_not_met':0}
    result=[]
    for row in rows:
        if any(args.get(key+'_none')==['1']for key in none_keys):continue
        if start is not None and row['recorded_at']<start:continue
        if end is not None and row['recorded_at']>=end:continue
        if series and not (set(row.get('series')or ())& set(series)):continue
        if opponent_series:
            other=basis['_opponent_views'].get((row['digest'],row['opponent']))
            if other is None or not (set(other.get('series')or ())& set(opponent_series)):continue
        if not deck_matches(row['deck_type']or '__unknown__',deck_values,contains):continue
        if not deck_matches(row['opponent_deck_type']or '__unknown__',opponent_values,contains):continue
        if values('first')and str(row['first_player'])not in values('first'):continue
        if values('outcome')and row['outcome']not in values('outcome'):continue
        # P16：卡片条件失败时区分"证据不足"与"确实不满足"（每行只记第一个原因）。
        failure=None
        for prefix,codes,mode,match in conditions:
            if not codes:continue
            opponent_side=prefix.startswith('opponent')
            subject=basis['_opponent_views'].get((row['digest'],row['opponent'])) if opponent_side else row
            if subject is None:
                failure='opponent_evidence_missing' if opponent_side else 'self_evidence_missing';break
            key=(subject['digest'],subject['player'])
            observed=(used if mode=='used'else draws).get(key,set())
            if mode=='not_drawn':
                if not subject['draw_complete']:
                    failure='draw_process_incomplete';break
                observed=carried.get(subject['build'],set())-observed
            elif codes:
                deck_cards=carried.get(subject['build'],set()) if mode=='drawn' else all_carried.get(subject['build'],set())
                missing=[code for code in codes if code in deck_cards and (subject['digest'],subject['player'],code)not in facts_present]
                if missing and (match=='all' or len(missing)==len(codes)):
                    failure='card_fact_missing';break
            if not (codes<=observed if match=='all'else bool(codes&observed)):
                failure='condition_not_met';break
        if failure:
            reasons[failure]+=1;continue
        result.append({k:row.get(k)for k in ('digest','player','deck_type','opponent','opponent_deck_type','recorded_at','expires_at','first_player','outcome','recording_title','download_filename','observed_date','observed_day','observed_match_no','observed_game_no','observed_match_id','current_rank','opponent_current_rank')})
    result.sort(key=lambda r:r['recorded_at'],reverse=True)
    unique_recordings=len({r['digest'] for r in result})
    samples=result if limit is None else result[:limit]
    return dict(recording_filters_version=3,card_filters_version=2,
                samples=samples,total=len(result),limited=(limit is not None and len(result)>limit),
                unique_recordings=unique_recordings,roster=basis['roster'],metrics=win_rate(result),
                filter_reasons=reasons if conditions else {},
                filter_reasons_scope='rows_that_passed_non_card_filters')
