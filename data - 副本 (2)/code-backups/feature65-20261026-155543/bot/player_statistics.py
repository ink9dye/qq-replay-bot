"""先选每位当前玩家最新有效小局构筑，再作类型筛选及按人投入分布。"""
import json
from .top100 import eligible_views


def capture_metadata(db,digests):
    captures={}
    for row in db.execute('SELECT digest,payload_json FROM stat_evidence'):
        if row['digest'] not in digests:continue
        try:info=json.loads(row['payload_json']).get('capture',{})
        except (ValueError,AttributeError):continue
        if type(info.get('game_no')) is int and info['game_no']>0 and (info.get('session_id') is None or isinstance(info.get('session_id'),str)):
            captures.setdefault(row['digest'],set()).add((info.get('session_id'),info['game_no'],info.get('captured_from_start') is True))
    return {d:next(iter(rows)) for d,rows in captures.items() if len(rows)==1}


def distributions(people,key):
    result={}
    for partition in ('main','side','total','extra'):
        groups={}
        for person in people:
            count=person[key][partition]
            if count is not None:groups[count]=groups.get(count,0)+1
        known=sum(groups.values())
        result[partition]=dict(known_players=known,unknown_players=len(people)-known,
            bins=[dict(copies=n,players=groups.get(n,0),percentage=100*groups.get(n,0)/known if known else None)
                  for n in sorted(set(range(4))|set(groups))])
    return result


def query(db,replay_root,code,now,deck_types=(),series=()):
    if type(code) is not int or code<=0:raise ValueError('invalid_card')
    from .card_identity import identity
    card_identity=identity(db,code);codes=card_identity['codes']
    basis=eligible_views(db,replay_root,now)
    latest={}
    for row in basis['views']:
        old=latest.get(row['player'])
        if old is None or (row['recorded_at'],row['received_at'] if 'received_at' in row else 0,row['digest'])>(old['recorded_at'],old.get('received_at',0),old['digest']):
            latest[row['player']]=row
    selected=[r for r in latest.values() if (not deck_types or r['deck_type'] in deck_types)
              and (not series or set(series).intersection(r['series']))]
    placeholders=','.join('?' for _ in codes)
    copies={(r['build'],r['partition']):r['copies'] for r in db.execute(
        f'SELECT build,partition,SUM(copies) copies FROM stat_build_cards WHERE code IN ({placeholders}) GROUP BY build,partition',codes)}
    metadata=capture_metadata(db,{r['digest'] for r in basis['views']})
    def counts(row):
        values={p:copies.get((row['build'],p),0) if row[p+'_complete'] else None for p in ('main','side','extra')}
        values['total']=values['main']+values['side'] if values['main'] is not None and values['side'] is not None else None
        return values
    people=[]
    for row in sorted(selected,key=lambda r:r['rank']):
        meta=metadata.get(row['digest'])
        initial=None
        if meta:
            if meta[1:]==(1,True):initial=row
            elif meta[0]:
                same_match=[candidate for candidate in basis['views'] if candidate['player']==row['player']
                    and metadata.get(candidate['digest'])==(meta[0],1,True)]
                if len(same_match)==1:initial=same_match[0]
        people.append(dict(player=row['player'],rank=row['rank'],digest=row['digest'],build=row['build'],
            recorded_at=row['recorded_at'],deck_type=row['deck_type'],series=row['series'],copies=counts(row),
            series_status=row['series_status'],series_origin=row['series_origin'],series_membership=row['series_membership'],
            observation_kind='confirmed_first_game' if meta and meta[1:]==(1,True) else 'post_side' if meta and meta[1]>1 else 'initial_status_unknown',
            initial_source_digest=initial['digest'] if initial else None,
            initial_source_recorded_at=initial['recorded_at'] if initial else None,
            initial_copies=counts(initial) if initial else {p:None for p in ('main','side','total','extra')}))
    types={}
    for row in latest.values():
        label=row['deck_type']
        group=types.setdefault(label,dict(deck_type=label,players=0,highest_rank=row['rank']))
        group['players']+=1;group['highest_rank']=min(group['highest_rank'],row['rank'])
    roster={k:v for k,v in basis['roster'].items() if k!='members'}
    families={}
    for person in people:
        for label in person['series']:
            group=families.setdefault(label,dict(series=label,players=0,highest_rank=person['rank']))
            group['players']+=1;group['highest_rank']=min(group['highest_rank'],person['rank'])
    return dict(unit='current_player_latest_valid_build',card=card_identity['canonical_card'],card_identity=card_identity,roster=roster,
        current_players=len(basis['roster']['members']),covered_players=len(latest),
        missing_players=len(basis['roster']['members'])-len(latest),filtered_players=len(people),
        filters=dict(deck_types=list(deck_types),series=list(series)),
        distributions=distributions(people,'initial_copies'),observed_distributions=distributions(people,'copies'),people=people,
        distribution_basis='confirmed_first_game_of_latest_observed_match',
        observed_distribution_basis='latest_observed_build_may_be_post_side',
        data_quality=basis['diagnostics'],
        deck_types=sorted(types.values(),key=lambda r:(-r['players'],r['highest_rank'])),
        deck_type_distribution_scope='all_latest_players_before_filters',
        series_distribution=sorted(families.values(),key=lambda r:(-r['players'],r['highest_rank'],r['series'])),
        series_count_semantics='overlapping_contains_labels_per_filtered_latest_player',
        unknown_series_players=sum(p['series_status'] in ('unknown','partial') for p in people),
        series_reference=basis['series_reference'],
        classification_revision=basis['classification_revision'])


def main():
    import argparse,time
    from pathlib import Path
    from .store import Store
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('card',type=int)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--deck',action='append',default=[])
    parser.add_argument('--series',action='append',default=[])
    parser.add_argument('--people',action='store_true')
    args=parser.parse_args();root=args.root.resolve()
    cfg=json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
    data=Path(cfg.get('data_dir') or root/'data')
    store=Store(data/'bot.sqlite3')
    try:
        result=store.player_card_statistics(Path(cfg.get('replay_dir') or data/'replays'),args.card,time.time(),args.deck,args.series)
        if not args.people:result.pop('people')
        print(json.dumps(result,ensure_ascii=False))
    finally:store.close()


if __name__=='__main__':main()
