"""卡组与含系列分组：按人最新观测和按小局玩家视角分别计数。"""
from .statistics_dashboard import population


def query(db,replay_root,now):
    basis,latest,people=population(db,replay_root,now)
    groups={'deck':{},'series':{}}
    def get(kind,name):
        return groups[kind].setdefault(name,dict(name=name,players=0,highest_rank=None,
            player_percentage=None,small_game_perspectives=0,unique_small_games=0,
            outcomes=dict(win=0,loss=0,draw=0,unknown=0),members=[],_digests=set()))
    for person in people:
        if not person['observed']:continue
        for kind,names in (('deck',[person['deck_type']]),('series',person['series'])):
            for name in set(names):
                group=get(kind,name);group['players']+=1;group['members'].append(person)
                group['highest_rank']=person['rank'] if group['highest_rank'] is None else min(group['highest_rank'],person['rank'])
    for row in basis['views']:
        for kind,names in (('deck',[row['deck_type']]),('series',row['series'])):
            for name in set(names):
                group=get(kind,name);group['small_game_perspectives']+=1;group['_digests'].add(row['digest'])
                outcome=row['outcome'] if row['outcome'] in group['outcomes'] else 'unknown'
                group['outcomes'][outcome]+=1
    denominator=len(latest)
    for rows in groups.values():
        for group in rows.values():
            group['unique_small_games']=len(group.pop('_digests'))
            group['player_percentage']=100*group['players']/denominator if denominator else None
            group['members'].sort(key=lambda p:p['rank'])
    sort=lambda g:(-g['players'],g['highest_rank'] or 101,g['name'] or '')
    return dict(generated_at=now,roster={k:v for k,v in basis['roster'].items() if k!='members'},
        current_players=len(people),covered_players=denominator,missing_players=len(people)-denominator,
        unknown_deck_players=sum(p['observed'] and p['deck_type'] is None for p in people),
        unknown_series_players=sum(p['observed'] and p['series_status'] in ('unknown','partial') for p in people),
        deck_groups=sorted(groups['deck'].values(),key=sort),series_groups=sorted(groups['series'].values(),key=sort),
        count_semantics=dict(players='latest_valid_observation_per_current_player_before_grouping',
            small_games='all_valid_player_small_game_perspectives_with_actual_build',
            series='overlapping_contains_series_labels_not_exclusive_archetypes',
            denominator='covered_current_players_unknown_decks_included'),
        data_quality=basis['diagnostics'],classification_revision=basis['classification_revision'],series_reference=basis['series_reference'])


def source_file(db,replay_root,now,player,digest):
    """只允许当前该玩家最新有效构筑的源录像，资格／到期／排除每次重新检查。"""
    from .share import safe_replay_file
    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):return None
    _,latest,_=population(db,replay_root,now)
    row=latest.get(player)
    if row is None or row['digest']!=digest:return None
    for file in db.execute('SELECT r.rel_path FROM stat_files f JOIN replays r ON r.id=f.replay_id WHERE f.digest=? ORDER BY r.id',(digest,)):
        path=safe_replay_file(replay_root,file['rel_path'])
        if path is not None:return path
    return None
