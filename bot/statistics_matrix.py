"""行卡组玩家视角对列卡组；小局胜负与实际先后攻，不使用整场结果。"""
from .top100 import eligible_views
from .card_statistics import win_rate


def deck_usage(db,replay_root,now,basis=None):
    """Distinct current-top100 players per deck type from the normalized latest view.

    people 已由 player_statistics 的最新有效视角与 inherit_first_game_types 归一；
    这里必须复用该 deck_type，不能回读 stat_builds 的原始分类覆盖可靠整场身份。
    缺失/未知构筑不进分母，同一玩家只计一次。
    basis 传入同一请求的 eligible_views 结果，避免重复全量查询（口径不变）。
    """
    if basis is None:
        from .player_statistics import query as player_query
        seed=player_query(db,replay_root,1,now)
        counts={}
        for person in seed['people']:
            label=person.get('deck_type')
            if not label:continue
            counts[label]=counts.get(label,0)+1
        return counts
    from .statistics_population import latest_views
    counts={}
    for row in latest_views(basis).values():
        label=row.get('deck_type')
        if not label:continue
        counts[label]=counts.get(label,0)+1
    return counts


def deck_order(labels,usage):
    """Usage desc, then name; unknown deck last (stable for ties)."""
    return sorted(labels,key=lambda label:(label is None,-usage.get(label,0),label or ''))


def query(db,replay_root,now,rows=(),columns=(),first_player=None,internal_only=False,include_samples=True):
    basis=eligible_views(db,replay_root,now,internal_only)
    opponents={}
    for row in db.execute('SELECT v.digest,v.player,b.deck_type FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build'):
        opponents.setdefault(row['digest'],[]).append(dict(row))
    groups={};selected=[]
    for row in basis['views']:
        other=[p for p in opponents.get(row['digest'],[]) if p['player']!=row['player']]
        if len(other)!=1:continue
        column=other[0]['deck_type'];label=row['deck_type']
        if rows and label not in rows or columns and column not in columns:continue
        if first_player is not None and row['first_player']!=int(first_player):continue
        sample=dict(digest=row['digest'],player=row['player'],deck_type=label,outcome=row['outcome'],first_player=row['first_player'],recorded_at=row['recorded_at'])
        groups.setdefault((label,column),[]).append(sample);selected.append(sample)
    cells=[]
    for (label,column),samples in groups.items():
        cell=dict(row=label,column=column,metrics=win_rate(samples),perspectives=len(samples),small_games=len({s['digest'] for s in samples}),
            first=win_rate([s for s in samples if s['first_player']==1]),second=win_rate([s for s in samples if s['first_player']==0]),
            unknown_first=sum(s['first_player'] is None for s in samples))
        if include_samples:
            cell['samples']=sorted(samples,key=lambda s:s['recorded_at'],reverse=True)
        else:
            # 页面只用 metrics/perspectives；逐格明细由 matrix-cell 按需返回。
            cell={k:cell[k] for k in ('row','column','metrics','perspectives')}
        cells.append(cell)
    usage=deck_usage(db,replay_root,now,basis)
    order=lambda label:(label is None,-usage.get(label,0),label or '')
    usage_rows=[dict(deck_type=label,players=count) for label,count in
                sorted(usage.items(),key=lambda item:(-item[1],item[0]))]
    return dict(generated_at=now,roster={k:v for k,v in basis['roster'].items() if k!='members'},
        unit='row_player_small_game_perspective_against_column_deck',draws_in_denominator=True,
        rows=sorted({c['row'] for c in cells},key=order),columns=sorted({c['column'] for c in cells},key=order),cells=cells,
        deck_usage=usage_rows,usage_basis='current_top100_latest_valid_build_distinct_player',
        total=win_rate(selected),perspectives=len(selected),small_games=len({s['digest'] for s in selected}),
        filters=dict(rows=list(rows),columns=list(columns),first_player=first_player,internal_only=internal_only),
        data_quality=basis['diagnostics'],classification_revision=basis['classification_revision'])
