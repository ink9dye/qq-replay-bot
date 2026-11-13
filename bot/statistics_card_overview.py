"""每位当前玩家最新构筑的整批单卡投入表，与单卡按人查询共用选源。"""
from pathlib import Path
from .player_statistics import query as player_query
from .card_identity import reference
from .statistics_card_names import index
from .card_search import card_name


def choices(db,replay_root,now):
    """Exact default overview row projection, without facts or win-rate tables."""
    from .top100 import eligible_views
    from .statistics_population import latest_views
    basis=eligible_views(db,replay_root,now,annotate_series=False)
    latest=latest_views(basis)
    builds={row['build'] for row in basis['views']}
    mapping,_,_=reference(db)
    cards={}
    # Stream the covering index once; avoid thousands of random table lookups.
    # The schema guarantees copies>0, and this projection only needs presence.
    for row in db.execute('SELECT build,partition,code FROM stat_build_cards'):
        if row['build'] not in builds:continue
        code=mapping.get(row['code'],(row['code'],None))[0]
        cards[(row['build'],row['partition'],code)]=1
    codes={key[2] for key in cards};names=index(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent)
    by_build={}
    for (build,part,code),copies in cards.items():
        by_build.setdefault(build,{}).setdefault(code,{})[part]=copies
    totals={};extras={}
    for row in latest.values():
        for code,parts in by_build.get(row['build'],{}).items():
            info=names.get(str(code),{});kind=info.get('card_type')
            base='extra' if type(kind) is int and kind & (0x40|0x2000|0x800000|0x4000000) else 'main'
            if parts.get(base,0)+parts.get('side',0)>0:totals[code]=totals.get(code,0)+1
            if parts.get('extra',0)>0:extras[code]=extras.get(code,0)+1
    result=[]
    for code in sorted(codes,key=lambda c:(-totals.get(c,0),-extras.get(c,0),c)):
        info=names.get(str(code));kind=(info or {}).get('card_type')
        result.append(dict(code=code,name=card_name(info) if info else '卡名暂缺',
            base_partition='extra' if type(kind) is int and kind & (0x40|0x2000|0x800000|0x4000000) else 'main'))
    return dict(rows=result)


def query(db,replay_root,now,basis='observed',deck_types=()):
    if basis not in ('observed','initial'):raise ValueError('invalid_basis')
    seed=player_query(db,replay_root,1,now,deck_types)
    selected=[]
    for person in seed['people']:
        digest=person['digest'] if basis=='observed' else person['initial_source_digest']
        if digest is None:continue
        row=db.execute('SELECT v.build,b.main_complete,b.side_complete,b.extra_complete FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build WHERE v.digest=? AND v.player=?',(digest,person['player'])).fetchone()
        selected.append((person['player'],dict(row)))
    mapping,identity_version,_=reference(db)
    builds={row['build'] for _,row in selected};cards={}
    for row in db.execute('SELECT build,partition,code,copies FROM stat_build_cards'):
        if row['build'] not in builds:continue
        canonical=mapping.get(row['code'],(row['code'],None))[0]
        key=(row['build'],row['partition'],canonical);cards[key]=cards.get(key,0)+row['copies']
    from .statistics_recording_filters import card_sample_statistics
    samples=card_sample_statistics(db,replay_root,now,deck_types,include_carried=True)
    rankings=samples['draw_rankings']
    game_counts=samples['carried_game_counts']
    codes={key[2] for key in cards}|set(rankings)|set(samples['carried_rankings']);names=index(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent)
    rows=[];population=len(seed['people'])
    for code in codes:
        info=names.get(str(code),{});kind=info.get('card_type')
        base='extra' if type(kind) is int and kind & (0x40|0x2000|0x800000|0x4000000) else 'main'
        partitions={};counts={}
        for part in ('main','side','extra'):
            counts[part]=[(cards.get((row['build'],part,code),0),bool(row[part+'_complete'])) for _,row in selected]
        counts['total']=[(a+b,ac and bc) for (a,ac),(b,bc) in zip(counts[base],counts['side'])]
        for part,values in counts.items():
            bins={};confirmed=0;unknown_presence=population-len(values);known=0
            for copies,complete in values:
                if copies>0:confirmed+=1
                if not complete and copies==0:unknown_presence+=1
                if complete:
                    known+=1;bins[copies]=bins.get(copies,0)+1
            partitions[part]=dict(confirmed_players=confirmed,unknown_presence_players=unknown_presence,known_copy_players=known,
                unknown_copy_players=population-known,bins={str(n):bins.get(n,0) for n in (0,1,2,3)},four_plus=sum(v for n,v in bins.items() if n>=4))
        info=names.get(str(code));aliases=mapping.get(code,(code,[code]))[1] or [code]
        rows.append(dict(code=code,name=card_name(info) if info else '卡名暂缺',codes=aliases,base_partition=base,partitions=partitions,draw_rankings=rankings.get(code,{}),carried_rankings=samples['carried_rankings'].get(code,{}),carried_game_count=game_counts.get(code,0)))
    rows.sort(key=lambda r:(-r['partitions']['total']['confirmed_players'],-r['partitions']['extra']['confirmed_players'],r['code']))
    return dict(generated_at=now,roster=seed['roster'],basis=basis,deck_filter_version=2,deck_choices=samples['deck_choices'],filters=dict(deck_types=list(deck_types)),current_players=seed['current_players'],covered_players=seed['covered_players'],
        selected_players=population,known_basis_players=len(selected),unknown_basis_players=population-len(selected),
        partition_known_players={p:sum(bool(row[p+'_complete']) for _,row in selected) for p in ('main','side','extra')},
        missing_players=seed['missing_players'],identity_version=identity_version,rows=rows,
        sample_baselines=samples['sample_baselines'],
        semantics='one_latest_observation_per_player_initial_from_same_latest_match_when_proven_no_unknown_side_zero')
