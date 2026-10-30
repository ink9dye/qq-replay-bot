"""行卡组玩家视角对列卡组；小局胜负与实际先后攻，不使用整场结果。"""
from .top100 import eligible_views
from .card_statistics import win_rate


def query(db,replay_root,now,rows=(),columns=(),first_player=None,internal_only=False):
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
        sample=dict(digest=row['digest'],player=row['player'],outcome=row['outcome'],first_player=row['first_player'],recorded_at=row['recorded_at'])
        groups.setdefault((label,column),[]).append(sample);selected.append(sample)
    cells=[]
    for (label,column),samples in groups.items():
        cells.append(dict(row=label,column=column,metrics=win_rate(samples),perspectives=len(samples),small_games=len({s['digest'] for s in samples}),
            first=win_rate([s for s in samples if s['first_player']==1]),second=win_rate([s for s in samples if s['first_player']==0]),
            unknown_first=sum(s['first_player'] is None for s in samples),samples=sorted(samples,key=lambda s:s['recorded_at'],reverse=True)))
    order=lambda x:x or ''
    return dict(generated_at=now,roster={k:v for k,v in basis['roster'].items() if k!='members'},
        unit='row_player_small_game_perspective_against_column_deck',draws_in_denominator=True,
        rows=sorted({c['row'] for c in cells},key=order),columns=sorted({c['column'] for c in cells},key=order),cells=cells,
        total=win_rate(selected),perspectives=len(selected),small_games=len({s['digest'] for s in selected}),
        filters=dict(rows=list(rows),columns=list(columns),first_player=first_player,internal_only=internal_only),
        data_quality=basis['diagnostics'],classification_revision=basis['classification_revision'])
