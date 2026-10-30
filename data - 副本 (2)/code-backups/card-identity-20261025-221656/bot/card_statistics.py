"""当前前100的单卡小局指标；按人投入分布与网页对阵矩阵尚属后续接口。"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass,asdict
from pathlib import Path


@dataclass(frozen=True)
class Filters:
    deck_types:tuple=()
    opponent_types:tuple=()
    series:tuple=()
    opponent_series:tuple=()
    first_player:bool|None=None
    internal_only:bool=False
    scope:str='default'
    start_at:float|None=None
    end_at:float|None=None


def fraction(numerator,denominator,unknown=0):
    return dict(numerator=numerator,denominator=denominator,
                percentage=100*numerator/denominator if denominator else None,unknown_samples=unknown)


def win_rate(rows):
    known=[r for r in rows if r['outcome'] in ('win','loss','draw')]
    wins=sum(r['outcome']=='win' for r in known)
    return dict(fraction(wins,len(known),len(rows)-len(known)),wins=wins,
                losses=sum(r['outcome']=='loss' for r in known),draws=sum(r['outcome']=='draw' for r in known))


def rates(rows,partition):
    complete=[r for r in rows if r[partition+'_complete']]
    carried=[r for r in rows if r['copies']>0]
    result=dict(player_samples=len(rows),small_games=len({r['digest'] for r in rows}),
        carrying=fraction(sum(r['copies']>0 for r in complete),len(complete),len(rows)-len(complete)),
        scope_win_rate=win_rate(rows),carried_win_rate=win_rate(carried))
    def occurrence(pool,field):
        known=[r for r in pool if r.get(field) in (0,1)]
        return fraction(sum(r[field]==1 for r in known),len(known),len(pool)-len(known))
    process=[r for r in carried if r['usage_complete']]
    if partition=='extra':
        result.update(appearance_rate=occurrence(process,'appeared'),
                      appeared_win_rate=win_rate([r for r in carried if r.get('appeared')==1]))
        result['appearance_rate']['incomplete_carried_samples']=len(carried)-len(process)
        result['appearance_rate']['unknown_samples']=len(carried)-result['appearance_rate']['denominator']
    else:
        draws=[r for r in carried if r['draw_complete']]
        result.update(draw_rate=occurrence(draws,'drawn'),use_rate=occurrence(process,'used'),
            drawn_win_rate=win_rate([r for r in carried if r.get('drawn')==1]),
            used_win_rate=win_rate([r for r in carried if r.get('used')==1]),
            use_after_draw_rate=occurrence([r for r in process if r.get('drawn')==1],'used_after_draw'))
        result['draw_rate']['incomplete_carried_samples']=len(carried)-len(draws)
        result['use_rate']['incomplete_carried_samples']=len(carried)-len(process)
        result['draw_rate']['unknown_samples']=len(carried)-result['draw_rate']['denominator']
        result['use_rate']['unknown_samples']=len(carried)-result['use_rate']['denominator']
        result['use_after_draw_rate']['unknown_samples']=sum(r.get('drawn')==1 for r in carried)-result['use_after_draw_rate']['denominator']
    return result


def query(db,replay_root,code,partition,now,filters=None):
    from .top100 import eligible_views
    if type(code) is not int or code<=0 or partition not in ('main','side','extra'):
        raise ValueError('invalid_card_or_partition')
    f=filters or Filters()
    if f.first_player is not None and type(f.first_player) is not bool:raise ValueError('invalid_order_filter')
    basis=eligible_views(db,replay_root,now,f.internal_only,f.scope)
    candidates=basis['views']
    others={(r['digest'],r['player']):dict(r) for r in db.execute('SELECT v.digest,v.player,v.build,b.deck_type FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build')}
    tags={}
    for row in db.execute('SELECT build,series FROM stat_build_series'):
        tags.setdefault(row['build'],set()).add(row['series'])
    opponents={}
    for row in others.values():opponents.setdefault(row['digest'],[]).append(row)
    facts={(r['digest'],r['player']):dict(r) for r in db.execute('SELECT * FROM stat_card_facts WHERE code=? AND partition=?',(code,partition))}
    copies_by_build={r['build']:r['copies'] for r in db.execute('SELECT build,copies FROM stat_build_cards WHERE code=? AND partition=?',(code,partition))}
    selected=[]
    filter_exclusions={key:0 for key in ('deck_type','series','opponent_type','opponent_series','first_player','start_at','end_at')}
    for row in candidates:
        opponent=next((o for o in opponents.get(row['digest'],[]) if o['player']!=row['player']),{})
        tests=(('deck_type',f.deck_types and row['deck_type'] not in f.deck_types),
            ('series',f.series and not set(f.series).intersection(row['series'])),
            ('opponent_type',f.opponent_types and opponent.get('deck_type') not in f.opponent_types),
            ('opponent_series',f.opponent_series and not set(f.opponent_series).intersection(tags.get(opponent.get('build'),set()))),
            ('first_player',f.first_player is not None and row['first_player']!=int(f.first_player)),
            ('start_at',f.start_at is not None and row['recorded_at']<f.start_at),
            ('end_at',f.end_at is not None and row['recorded_at']>=f.end_at))
        rejected=next((key for key,failed in tests if failed),None)
        if rejected:
            filter_exclusions[rejected]+=1;continue
        card=facts.get((row['digest'],row['player']),{})
        selected.append(dict(row,**{k:v for k,v in card.items() if k not in ('digest','player','copies')},copies=copies_by_build.get(row['build'],0)))
    groups={}
    for row in selected:groups.setdefault(row['deck_type'],[]).append(row)
    roster={k:v for k,v in basis['roster'].items() if k!='members'}
    roster['member_count']=len(basis['roster']['members'])
    return dict(card=code,partition=partition,unit='player_small_game_perspective',filters=asdict(f),
        roster=roster,classification_revision=basis['classification_revision'],candidate_samples=len(candidates),
        data_quality=dict(basis['diagnostics'],filter_exclusions=filter_exclusions,
            filter_exclusion_unit='first_failing_filter_per_perspective',
            selected_unknown_outcome=sum(r['outcome']=='unknown' for r in selected),
            selected_unknown_series=sum(r['series_status']=='unknown' for r in selected),
            carried_without_card_event_fact=sum(r['copies']>0 and (r['digest'],r['player']) not in facts for r in selected)),
        covered_current_players=len({r['player'] for r in candidates}),filtered_players=len({r['player'] for r in selected}),
        metrics=rates(selected,partition),
        first=rates([r for r in selected if r['first_player']==1],partition),
        second=rates([r for r in selected if r['first_player']==0],partition),
        unknown_first_samples=sum(r['first_player'] is None for r in selected),
        unknown_deck_samples=sum(r['deck_type'] is None for r in selected),
        by_deck=[dict(deck_type=label,metrics=rates(rows,partition)) for label,rows in sorted(groups.items(),key=lambda item:item[0] or '')],
        samples=[dict(digest=r['digest'],player=r['player'],build=r['build'],outcome=r['outcome'],
                      first_player=r['first_player']) for r in selected])


def main():
    from .store import Store
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('card',type=int);p.add_argument('--partition',choices=['main','side','extra'],default='main')
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--deck',action='append',default=[]);p.add_argument('--opponent',action='append',default=[])
    p.add_argument('--series',action='append',default=[]);p.add_argument('--opponent-series',action='append',default=[])
    p.add_argument('--internal',action='store_true');p.add_argument('--first',choices=['first','second','all'],default='all')
    args=p.parse_args();root=args.root.resolve()
    cfg=json.loads((root/'config.json').read_text(encoding='utf-8-sig'));data=Path(cfg.get('data_dir') or root/'data')
    store=Store(data/'bot.sqlite3')
    try:
        filters=Filters(tuple(args.deck),tuple(args.opponent),tuple(args.series),tuple(args.opponent_series),
                        None if args.first=='all' else args.first=='first',args.internal)
        print(json.dumps(store.card_statistics(Path(cfg.get('replay_dir') or data/'replays'),args.card,args.partition,time.time(),filters),ensure_ascii=False))
    finally:store.close()


if __name__=='__main__':main()
