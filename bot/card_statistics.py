"""当前前100的单卡小局指标；另含“双方前100、使用后”的对手卡组明细。"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass,asdict
from pathlib import Path

MATCHUP_POPULATION = 'self_and_opponent_current_top100'
MATCHUP_MIN_GAMES = 10
MATCHUP_FALLBACK_GAMES = 5
MATCHUP_LIMIT = 5


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
    deck_contains:bool=False


def deck_matches(name, terms, contains=False):
    """Exact choices remain supported; web text filters may match name fragments."""
    if not terms:
        return True
    if name in terms:
        return True
    return bool(contains and isinstance(name, str) and any(
        isinstance(term, str) and term.strip()
        and term not in ('__unknown__', '__no_selection__')
        and term.strip().casefold() in name.casefold() for term in terms))


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
    result['evidence']=dict(carried_samples=len(carried),complete_usage_samples=len(process),
        confirmed_used_samples=sum(r.get('used')==1 for r in carried),
        used_draw_unknown_samples=sum(r.get('used')==1 and r.get('drawn') is None for r in carried),
        used_not_drawn_samples=sum(r.get('used')==1 and r.get('drawn')==0 for r in carried))
    if partition=='extra':
        result.update(appearance_rate=occurrence(process,'appeared'),
                      appeared_win_rate=win_rate([r for r in carried if r.get('appeared')==1]))
        result['appearance_rate']['incomplete_carried_samples']=len(carried)-len(process)
        result['appearance_rate']['unknown_samples']=len(carried)-result['appearance_rate']['denominator']
    else:
        draws=[r for r in carried if r['draw_complete']]
        result['evidence'].update(complete_draw_samples=len(draws),confirmed_drawn_samples=sum(r.get('drawn')==1 for r in carried))
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


def _interval(wins, games):
    """Wilson 95% 区间；只描述抽样不确定性，不表述为因果。"""
    if not games:
        return None
    z = 1.959963984540054
    z2 = z * z
    p = wins / games
    d = 1 + z2 / games
    mid = (p + z2 / (2 * games)) / d
    radius = z * math.sqrt(p * (1 - p) / games + z2 / (4 * games * games)) / d
    return [max(0.0, mid - radius), min(1.0, mid + radius)]


def _opponent_rows(db, digests):
    """按候选 digest 一次取回对手玩家与卡组；不扫全表。"""
    result = {}
    digests = sorted(set(digests))
    for start in range(0, len(digests), 400):
        chunk = digests[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        for row in db.execute(
                'SELECT v.digest,v.player,b.deck_type FROM stat_views v '
                'JOIN stat_builds b ON b.fingerprint=v.build '
                'WHERE v.digest IN (' + marks + ')', chunk):
            result.setdefault(row['digest'], []).append(dict(row))
    return result


def matchups(db, selected, facts, partition='main', limit=MATCHUP_LIMIT,
             min_games=MATCHUP_MIN_GAMES, fallback_games=MATCHUP_FALLBACK_GAMES,
             opponents=None):
    """单卡“使用后”对当前前100玩家卡组的胜率 Top N（只读，不写库）。

    口径：
    - 我方与对手都必须是当前前100玩家；对手不在榜、对手卡组未知、对手不唯一的
      小局只进总体分母，不进入单个卡组；
    - 主卡/Side 用 used=1；额外用 appeared=1；胜负平明确才计入，平局进分母；
    - 卡必须在该视角构筑的对应分区内（copies>0）；
    - 每行至少 min_games 局；不足 limit 行时用 fallback_games 以上的行补足，
      并标记 low_sample，仍不足则有几行显示几行。
    """
    field = 'appeared' if partition == 'extra' else 'used'
    top100 = {row[0] for row in db.execute('SELECT player FROM stat_current_top100')}
    if opponents is None:
        opponents = _opponent_rows(db, {row['digest'] for row in selected})
    groups = {}
    base = dict(games=0, wins=0, losses=0, draws=0, unknown_outcome=0)
    unknown_deck = 0
    outside_scope = 0
    ambiguous = 0
    for row in selected:
        if row['player'] not in top100:
            continue
        if int(row.get('copies') or 0) <= 0:
            continue
        fact = facts.get((row['digest'], row['player'])) or {}
        if fact.get(field) != 1:
            continue
        outcome = row.get('outcome')
        if outcome not in ('win', 'loss', 'draw'):
            base['unknown_outcome'] += 1
            continue
        other = [item for item in opponents.get(row['digest'], ())
                 if item['player'] != row['player']]
        if len(other) != 1:
            ambiguous += 1
            continue
        opponent = other[0]
        if opponent['player'] not in top100:
            outside_scope += 1
            continue
        base['games'] += 1
        base['wins'] += outcome == 'win'
        base['losses'] += outcome == 'loss'
        base['draws'] += outcome == 'draw'
        deck = opponent.get('deck_type')
        if not deck:
            unknown_deck += 1
            continue
        group = groups.setdefault(deck, dict(deck_type=deck, games=0, wins=0, losses=0, draws=0))
        group['games'] += 1
        group['wins'] += outcome == 'win'
        group['losses'] += outcome == 'loss'
        group['draws'] += outcome == 'draw'
    rows = []
    for group in groups.values():
        group['rate'] = 100 * group['wins'] / group['games'] if group['games'] else None
        group['interval'] = _interval(group['wins'], group['games'])
        group['low_sample'] = False
        rows.append(group)
    rows.sort(key=lambda item: (-(item['rate'] if item['rate'] is not None else -1),
                                -item['games'], item['deck_type']))
    qualified = [row for row in rows if row['games'] >= min_games]
    chosen = [dict(row) for row in qualified[:limit]]
    if len(chosen) < limit:
        used = {row['deck_type'] for row in chosen}
        for row in rows:
            if len(chosen) >= limit:
                break
            if row['deck_type'] in used or row['games'] < fallback_games:
                continue
            item = dict(row, low_sample=True)
            chosen.append(item)
            used.add(row['deck_type'])
    notes = []
    if not base['games']:
        notes.append('双方前100中没有“使用该卡且胜负明确”的小局。')
    elif not chosen:
        notes.append('没有达到 %d 局门槛的对手卡组。' % fallback_games)
    elif len(qualified) < limit:
        notes.append('达到 %d 局的对手卡组只有 %d 个；其余用 %d–%d 局补足并标注“样本少”。'
                     % (min_games, len(qualified), fallback_games, min_games - 1))
        if len(chosen) < limit:
            notes.append('符合条件的对手卡组不足 %d 个，本表只显示 %d 个。' % (limit, len(chosen)))
    if unknown_deck:
        notes.append('另有 %d 局对手卡组未识别，未计入分卡组。' % unknown_deck)
    if outside_scope:
        notes.append('另有 %d 局对手不在当前前100，未计入本表。' % outside_scope)
    if ambiguous:
        notes.append('另有 %d 局对手不唯一，未计入本表。' % ambiguous)
    return dict(
        version=1,
        unit='%s_small_game_perspective' % ('appeared' if partition == 'extra' else 'used'),
        population=MATCHUP_POPULATION,
        partition=partition,
        limit=limit,
        min_games=min_games,
        fallback_games=fallback_games,
        base=dict(games=base['games'], wins=base['wins'], losses=base['losses'],
                  draws=base['draws'], rate=100 * base['wins'] / base['games'] if base['games'] else None,
                  unknown_outcome=base['unknown_outcome']),
        rows=chosen,
        total_decks=len(rows),
        qualified_decks=len(qualified),
        notes=notes,
    )


def query(db,replay_root,code,partition,now,filters=None,matchups_enabled=False,
          matchup_limit=MATCHUP_LIMIT,matchup_min_games=MATCHUP_MIN_GAMES,
          matchup_fallback_games=MATCHUP_FALLBACK_GAMES):
    from .top100 import eligible_views
    if type(code) is not int or code<=0 or partition not in ('main','side','extra'):
        raise ValueError('invalid_card_or_partition')
    f=filters or Filters()
    from .card_identity import identity,facts as identity_facts
    from .card_series import reference as series_reference,annotate
    series_catalog=series_reference(db)
    card_identity=identity(db,code);codes=card_identity['codes']
    if f.first_player is not None and type(f.first_player) is not bool:raise ValueError('invalid_order_filter')
    basis=eligible_views(db,replay_root,now,f.internal_only,f.scope,series_catalog,
                         annotate_series=bool(f.series))
    candidates=basis['views']
    others = {}
    if f.opponent_types or f.opponent_series:
        digests = sorted({row['digest'] for row in candidates})
        for start in range(0, len(digests), 400):
            chunk = digests[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            for r in db.execute('SELECT v.digest,v.player,v.build,b.deck_type,b.classification_source,b.series_status,b.main_complete,b.extra_complete '
                                'FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build '
                                'WHERE v.digest IN (' + marks + ')', chunk):
                others[(r['digest'], r['player'])] = dict(r)
        if f.opponent_series:
            annotate(db, list(others.values()), series_catalog)
    opponents={}
    for row in others.values():opponents.setdefault(row['digest'],[]).append(row)
    placeholders=','.join('?' for _ in codes)
    selected=[]
    filter_exclusions={key:0 for key in ('deck_type','series','opponent_type','opponent_series','first_player','start_at','end_at')}
    for row in candidates:
        opponent=next((o for o in opponents.get(row['digest'],[]) if o['player']!=row['player']),{})
        tests=(('deck_type',not deck_matches(row['deck_type'],f.deck_types,f.deck_contains)),
            ('series',f.series and not set(f.series).intersection(row.get('series',()))),
            ('opponent_type',not deck_matches(opponent.get('deck_type'),f.opponent_types,f.deck_contains)),
            ('opponent_series',f.opponent_series and not set(f.opponent_series).intersection(opponent.get('series',[]))),
            ('first_player',f.first_player is not None and row['first_player']!=int(f.first_player)),
            ('start_at',f.start_at is not None and row['recorded_at']<f.start_at),
            ('end_at',f.end_at is not None and row['recorded_at']>=f.end_at))
        rejected=next((key for key,failed in tests if failed),None)
        if rejected:
            filter_exclusions[rejected]+=1;continue
        selected.append(dict(row))
    # 只读本次选中构筑的卡数与卡片事实：不再为整副卡扫全库，结果口径不变。
    builds=sorted({row['build'] for row in selected})
    copies_by_build={}
    for start in range(0,len(builds),400):
        chunk=builds[start:start+400]
        marks=','.join('?' for _ in chunk)
        for r in db.execute(
                f'SELECT build,SUM(copies) copies FROM stat_build_cards '
                f'WHERE code IN ({placeholders}) AND partition=? AND build IN ({marks}) '
                f'GROUP BY build',[*codes,partition,*chunk]):
            copies_by_build[r['build']]=r['copies']
    db.execute('DROP TABLE IF EXISTS temp._card_statistics_selected')
    db.execute('CREATE TEMP TABLE _card_statistics_selected(digest TEXT NOT NULL, player TEXT NOT NULL, '
               'PRIMARY KEY(digest,player))')
    db.executemany('INSERT OR IGNORE INTO _card_statistics_selected(digest,player) VALUES(?,?)',
                   [(row['digest'],row['player']) for row in selected])
    facts=identity_facts(db,codes,partition,'_card_statistics_selected')
    for row in selected:
        card=facts.get((row['digest'],row['player']),{})
        row.update({k:v for k,v in card.items() if k not in ('digest','player','copies')})
        row['copies']=copies_by_build.get(row['build'],0)
    groups={}
    for row in selected:groups.setdefault(row['deck_type'],[]).append(row)
    roster={k:v for k,v in basis['roster'].items() if k!='members'}
    roster['member_count']=len(basis['roster']['members'])
    result=dict(card=card_identity['canonical_card'],card_identity=card_identity,partition=partition,unit='player_small_game_perspective',filters=asdict(f),
        roster=roster,classification_revision=basis['classification_revision'],candidate_samples=len(candidates),
        series_reference=basis['series_reference'],
        data_quality=dict(basis['diagnostics'],filter_exclusions=filter_exclusions,
            filter_exclusion_unit='first_failing_filter_per_perspective',
            selected_unknown_outcome=sum(r['outcome']=='unknown' for r in selected),
            selected_unknown_series=sum(r['series_status'] in ('unknown','partial') for r in selected),
            carried_without_card_event_fact=sum(r['copies']>0 and (r['digest'],r['player']) not in facts for r in selected)),
        covered_current_players=len({r['player'] for r in candidates}),filtered_players=len({r['player'] for r in selected}),
        metrics=rates(selected,partition),
        first=rates([r for r in selected if r['first_player']==1],partition),
        second=rates([r for r in selected if r['first_player']==0],partition),
        unknown_first_samples=sum(r['first_player'] is None for r in selected),
        unknown_deck_samples=sum(r['deck_type'] is None for r in selected),
        by_deck=[dict(deck_type=label,metrics=rates(rows,partition)) for label,rows in sorted(groups.items(),key=lambda item:item[0] or '')],
        samples=[dict(digest=r['digest'],player=r['player'],build=r['build'],deck_type=r['deck_type'],outcome=r['outcome'],
                      first_player=r['first_player']) for r in selected])
    if matchups_enabled:
        if type(matchup_limit) is not int or not 1<=matchup_limit<=20:
            raise ValueError('matchup_limit_invalid')
        if type(matchup_min_games) is not int or not 1<=matchup_min_games<=1000:
            raise ValueError('matchup_min_games_invalid')
        if type(matchup_fallback_games) is not int or not 1<=matchup_fallback_games<=matchup_min_games:
            raise ValueError('matchup_fallback_games_invalid')
        result['matchups']=matchups(db,selected,facts,partition,matchup_limit,
                                    matchup_min_games,matchup_fallback_games)
    return result


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
