"""Private build page facts and explicitly inferred Side, from eligible original sources."""
from collections import Counter
import math
import hashlib
from .statistics_dashboard import population
from .statistics_match_identity import identity_metadata
from .card_statistics import win_rate



def validate_build(cards, inferred_cards=(), mapping=None, completeness=None):
    """Basic physical-copy/partition checks; never mutate recorded observations.

    Inferred Side overlaps direct Side rather than being another copy of it.
    This deliberately does not claim to validate a current or historical banlist.
    """
    mapping = mapping or {}
    completeness = completeness or {}
    counts = {p: Counter() for p in ('main', 'extra', 'side')}
    for card in cards:
        part = card['partition']
        if part in counts:
            code = mapping.get(card['code'], (card['code'],))[0]
            counts[part][code] += card['copies']
    inferred = Counter()
    for card in inferred_cards:
        code = mapping.get(card['code'], (card['code'],))[0]
        inferred[code] += card['copies']
    counts['side'] |= inferred
    issues = []
    for part, maximum in (('main', 60), ('extra', 15), ('side', 15)):
        total = sum(counts[part].values())
        if total > maximum:
            issues.append(dict(kind='partition_overflow', partition=part, copies=total, limit=maximum))
    if completeness.get('main') and sum(counts['main'].values()) < 40:
        issues.append(dict(kind='main_below_minimum', partition='main', copies=sum(counts['main'].values()), limit=40))
    total = counts['main'] + counts['extra'] + counts['side']
    for code, copies in sorted(total.items()):
        if copies > 3:
            issues.append(dict(kind='copy_overflow', code=code, copies=copies, limit=3,
                               partitions={p: counter[code] for p, counter in counts.items() if counter[code]}))
    labels = dict(main='主卡', extra='额外', side='Side')
    details = []
    for issue in issues:
        if issue['kind'] == 'copy_overflow':
            details.append('卡号%d在主卡、额外和Side合计%d张，超过基础上限%d张'
                           % (issue['code'], issue['copies'], issue['limit']))
        elif issue['kind'] == 'main_below_minimum':
            details.append('完整主卡记录仅%d张，少于%d张' % (issue['copies'], issue['limit']))
        else:
            details.append('%s共%d张，超过%d张'
                           % (labels.get(issue['partition'], issue['partition']), issue['copies'], issue['limit']))
    summary = ('构筑校验：' + '；'.join(details) + '。原始记录已保留。'
               if details else
               '实体张数检查通过（主卡＋额外＋Side 同一卡号合计≤3张，分区未超上限）。')
    # 禁限表按历史对局版本才能判断；本库没有对应版本时明确标为未校验。
    summary += '禁限表未校验（缺少对应版本）。'
    # 分区超量或同一实体超过 3 张才会阻止推测 Side 并入导出；
    # 「完整主卡不足 40 张」只作为来源记录告警，不改变可确认的 Side 证据。
    blocking = any(issue['kind'] in ('copy_overflow', 'partition_overflow') for issue in issues)
    return dict(status='conflict' if blocking else 'warning' if issues else 'no_basic_conflict',
                issues=issues, blocking=blocking,
                scope='physical_copy_and_partition_counts', banlist_checked=False,
                banlist_status='unverified_missing_version', summary=summary)


def query(db, replay_root, now, player, digest=None, scope='build',allowed_digests=None,history_digests=None):
    if not isinstance(player, str) or not player.strip() or len(player) > 128 or scope not in ('build', 'player'):
        raise ValueError('invalid_player_or_scope')
    # 玩家名允许首尾空格；统一按去边界空格后的名字查，显示也用同一份。
    player = player.strip()
    basis, latest, people = population(db, replay_root, now, player=player,allowed_digests=allowed_digests)
    rows = [r for r in basis['views'] if r['player'].strip() == player]
    selected = next((r for r in rows if r['digest'] == digest), None) if digest else latest.get(player)
    if selected is None:
        return None
    person = next(p for p in people if p['player'] == player)
    metadata = identity_metadata(db, {r['digest'] for r in rows})
    row_digests = tuple({r['digest'] for r in rows})
    # Shared resolver also handles upload provenance and quarantines old prefixes.
    builds = {r['build'] for r in rows}
    cards = {build: [] for build in builds}
    if builds:
        marks = ','.join('?' * len(builds))
        for r in db.execute('SELECT build,partition,code,copies FROM stat_build_cards '
                            'WHERE build IN (' + marks + ') ORDER BY partition,code', tuple(builds)):
            cards[r['build']].append(dict(partition=r['partition'], code=r['code'], copies=r['copies']))
    pairs = {}
    if row_digests:
        marks = ','.join('?' * len(row_digests))
        for opponent in db.execute('SELECT digest,player FROM stat_views WHERE digest IN (' + marks + ')', row_digests):
            pairs.setdefault(opponent['digest'], []).append(opponent['player'])
    for row in rows:
        row['_pair'] = tuple(sorted(pairs.get(row['digest'], [])))
    from .card_identity import reference
    mapping, _, _ = reference(db)
    def group_key(row):
        meta=metadata.get(row['digest'])
        return (meta[0],row['_pair']) if meta and meta[0] and len(row['_pair'])==2 else ('single:'+row['digest'],row['_pair'])
    def main_key(row):
        if not row['main_complete']:return ('incomplete',row['build'])
        counts=Counter()
        for card in cards[row['build']]:
            if card['partition']=='main':counts[mapping.get(card['code'],(card['code'],))[0]]+=card['copies']
        return tuple(sorted(counts.items()))
    selected_meta=metadata.get(selected['digest'])
    first_candidates=[r for r in rows if selected_meta and selected_meta[0] and r['_pair']==selected['_pair']
                      and metadata.get(r['digest'])==(selected_meta[0],1,True)]
    main_anchor=first_candidates[0]if selected_meta and selected_meta[1]>1 and len(first_candidates)==1 else selected
    selected_main=main_key(main_anchor)
    same_main={r['digest']for r in rows if main_key(r)==selected_main}
    relevant={group_key(r) for r in rows if r['digest']in same_main}
    ranks = {m['player']: m['rank'] for m in basis['roster']['members']}
    matches = []
    for row in sorted(rows, key=lambda r: (r['recorded_at'], r['digest']), reverse=True):
        if history_digests is not None and row['digest'] not in history_digests:continue
        if scope == 'build' and group_key(row) not in relevant:
            continue
        opponents = [dict(r) for r in db.execute('SELECT v.player,b.deck_type FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build WHERE v.digest=? AND v.player<>?', (row['digest'], player))]
        other = opponents[0] if len(opponents) == 1 else dict(player=None, deck_type=None)
        meta = metadata.get(row['digest'])
        matches.append(dict(player=player, digest=row['digest'], build=row['build'], deck_type=row['deck_type'],matches_selected_build=row['build']==selected['build'],matches_selected_main=row['digest']in same_main,
                            recorded_at=row['recorded_at'], expires_at=row['expires_at'], first_player=row['first_player'],
                            outcome=row['outcome'], current_rank=row['rank'], historical_rank=None,
                            opponent=other['player'], opponent_deck_type=other['deck_type'],
                            opponent_current_rank=ranks.get(other['player']), opponent_historical_rank=None,
                            game_no=meta[1] if meta and meta[1] > 0 else None,
                            session_id=meta[0] if meta else None))
    groups={}
    for match in matches:
        row=next(r for r in rows if r['digest']==match['digest'])
        key=group_key(row)
        groups.setdefault(key,[]).append(match)
    match_groups=[]
    rows_by_digest={r['digest']:r for r in rows}
    for key, games in groups.items():
        games.sort(key=lambda g:(g['game_no'] if g['game_no'] is not None else 10000,g['recorded_at'],g['digest']))
        match_deck_names(games)
        by_no={}
        for game in games:
            if game['game_no'] is not None:by_no.setdefault(game['game_no'],[]).append(game)
        for game in games:
            number=game['game_no']
            change=dict(status='unassociated',added=[],removed=[])
            if game['session_id'] and number is not None and metadata.get(game['digest'],(None,0,False))[2]:
                if number==1:change['status']='first_game'
                elif len(by_no.get(number,[]))!=1 or len(by_no.get(1,[]))>1:change['status']='ambiguous_sequence'
                elif 1 not in by_no:change['status']='missing_first_game'
                else:
                    # 所有换边局都对比第一局的基础主卡/额外，而不是逐局链式对比。
                    previous=by_no[1][0]
                    change=swap_changes(rows_by_digest[previous['digest']],rows_by_digest[game['digest']],cards,mapping)
                    change.update(from_game_no=1,to_game_no=number,previous_digest=previous['digest'])
            game['changes']=change
        numbers=set(by_no)
        missing=[n for n in range(1,max(numbers)+1) if n not in numbers] if numbers and max(numbers)<=20 else []
        match_groups.append(dict(id=hashlib.sha256(repr(key).encode()).hexdigest()[:16],associated=bool(games[0]['session_id']),games=games,
                                 missing_game_numbers=missing,sequence_ambiguous=any(len(v)>1 for v in by_no.values()),
                                 recorded_at=max(g['recorded_at'] for g in games)))
    match_groups.sort(key=lambda g:g['recorded_at'],reverse=True)
    from .statistics_recording_names import decorate
    decorate(db,matches)
    view_deck_types = {}
    if row_digests:
        marks = ','.join('?' * len(row_digests))
        for item in db.execute('SELECT v.digest, v.player, b.deck_type FROM stat_views v '
                               'JOIN stat_builds b ON b.fingerprint=v.build WHERE v.digest IN (' + marks + ')', row_digests):
            view_deck_types[(item['digest'], item['player'])] = item['deck_type']
    for row in rows:
        others = [p for p in row['_pair'] if p != row['player']]
        row['_opponent_deck_type'] = (view_deck_types.get((row['digest'], others[0]))
                                      if len(others) == 1 else None)
    inferred = infer_side(rows, selected, cards, metadata, mapping)
    contexts = {}
    for games in groups.values():
        for game in games:
            if group_key(rows_by_digest[game['digest']]) not in relevant: continue
            change = game['changes']
            if change['status'] not in ('known', 'partial'): continue
            for card in change['added']:
                contexts.setdefault(card['code'], []).append(dict(
                    digest=game['digest'], recorded_at=game['recorded_at'], expires_at=game['expires_at'],
                    game_no=game['game_no'], copies=card['copies'], partition=card['partition'],
                    opponent_deck_type=game.get('opponent_match_deck_type'), first_player=game['first_player'],
                    kind='same_match' if selected_meta and game['session_id']==selected_meta[0] else 'historical_same_build'))
    # Context is evidence of a positive adjacent-game change, never inferred just
    # because a card is listed in a directly recorded or candidate Side.
    for card in cards[selected['build']] + inferred['cards']:
        canonical=mapping.get(card['code'],(card['code'],))[0]
        card['swap_contexts']=contexts.get(canonical,[])
    stats_rows=rows if history_digests is None else [r for r in rows if r['digest'] in history_digests]
    from .deck_sort import load_card_data, sorted_cards
    from pathlib import Path
    data_dir = Path(db.execute('PRAGMA database_list').fetchone()[2]).parent
    selected_cards = cards[selected['build']]
    sort_data = load_card_data(data_dir, [c['code'] for c in selected_cards + inferred['cards']])
    selected_cards = sorted_cards(selected_cards, sort_data)
    inferred['cards'] = sorted_cards(inferred['cards'], sort_data)
    # YDK 导出时直接 Side 与推测 Side 会合并；这里先给出合并后的显示顺序。
    side_codes = sorted({card['code'] for card in selected_cards if card['partition'] == 'side'} |
                        {card['code'] for card in inferred['cards']})
    side_order = [card['code'] for card in sorted_cards(
        [dict(code=code, partition='side') for code in side_codes], sort_data)]
    validation = validate_build(selected_cards, inferred['cards'], mapping,
                                {p: bool(selected[p + '_complete']) for p in ('main', 'extra', 'side')})
    if validation.get('blocking'):
        # 基础张数冲突（分区超量/同一实体>3）不让推测 Side 并入导出；原始记录仍照常展示。
        inferred['conflict'] = True
    person_out=dict(person, deck_type=selected['deck_type'])
    return dict(generated_at=now, person=person_out,
                roster={k: v for k, v in basis['roster'].items() if k != 'members'},
                selected=dict(digest=selected['digest'], build=selected['build'], recorded_at=selected['recorded_at'],
                              expires_at=selected['expires_at'], observation_kind=person['observation_kind'] if selected['digest'] == latest[player]['digest'] else 'historical_observation'),
                cards=selected_cards, completeness={p: bool(selected[p + '_complete']) for p in ('main', 'extra', 'side')},
                side_order=side_order,
                inferred_side=inferred, build_validation=validation, matches=matches, match_groups=match_groups, scope=scope, scope_win_rate=win_rate(matches),
                exact_build_games=sum(r['build']==selected['build'] for r in stats_rows),
                same_main_games=sum(r['digest'] in same_main for r in stats_rows),match_main_source_digest=main_anchor['digest'],
                match_scope_basis='same_match_first_main'if main_anchor is not selected else'selected_main',
                exact_build_win_rate=win_rate([r for r in stats_rows if r['build']==selected['build']]),
                all_player_win_rate=win_rate(stats_rows), all_player_games=len(stats_rows),
                monthly_official=None, pt_history=[], pt_history_status='no_recorded_history',
                rank_semantics='current_roster_ranks_separate_from_unknown_historical_ranks',
                side_semantics='inferred_candidates_not_complete_or_statistical_facts')


def match_deck_names(games):
    """Match labels follow earliest identified deck per player; raw game labels stay intact."""
    names = {}
    for game in games:
        for person, field in ((game['player'], 'deck_type'), (game.get('opponent'), 'opponent_deck_type')):
            name = game.get(field)
            if person and name and name not in ('卡组未识别', '未知'):
                names.setdefault(person, name)
    for game in games:
        game['match_deck_type'] = names.get(game['player'])
        game['opponent_match_deck_type'] = names.get(game.get('opponent'))


def swap_changes(previous,current,cards,mapping):
    def counter(row,part):
        result=Counter()
        for c in cards[row['build']]:
            if c['partition']==part:result[mapping.get(c['code'],(c['code'],None))[0]]+=c['copies']
        return result
    added=[];removed=[];unknown=[]
    for part in ('main','extra'):
        if not previous[part+'_complete'] or not current[part+'_complete']:
            unknown.append(part);continue
        before,after=counter(previous,part),counter(current,part)
        added.extend(dict(partition=part,code=c,copies=n) for c,n in sorted((after-before).items()))
        removed.extend(dict(partition=part,code=c,copies=n) for c,n in sorted((before-after).items()))
    return dict(status='partial' if unknown else 'known',unknown_partitions=unknown,added=added,removed=removed)


def infer_side(rows, selected, cards, metadata, mapping=None):
    mapping = mapping or {}
    if not selected['main_complete'] or not selected['extra_complete']:
        return dict(cards=[], sources=[], complete=False, conflict=False, reason='selected_build_incomplete')
    if selected['side_complete']:
        return dict(cards=[], sources=[], complete=False, conflict=False, reason='direct_side_complete')
    def deck(row):
        result = Counter()
        for c in cards[row['build']]:
            if c['partition'] in ('main','extra'):
                code = mapping.get(c['code'], (c['code'], None))[0]
                result[(c['partition'], code)] += c['copies']
        return result
    def combined(row):
        result = Counter()
        for (part, code), count in deck(row).items():
            result[code] += count
        return result
    sessions = {}
    for row in rows:
        meta = metadata.get(row['digest'])
        if meta and meta[0] and meta[2] and row['main_complete'] and row['extra_complete']:
            sessions.setdefault(meta[0], []).append(row)
    chosen_meta = metadata.get(selected['digest'])
    chosen_session = chosen_meta[0] if chosen_meta else None
    own = sessions.get(chosen_session, [])
    own_first = [r for r in own if metadata[r['digest']][1] == 1]
    if len(own_first) != 1:
        # 没有唯一、可确认的首局就没有 Side 基准；不拿所选局或时间上相邻的局顶替。
        return dict(cards=[], sources=[], complete=False, conflict=False, reason='first_game_unavailable')
    anchor = own_first[0]
    inferred = Counter(); sources = []; per_card = {}
    session_sets = {}
    for session, group in sessions.items():
        by_no = {}
        for row in group:
            by_no.setdefault(metadata[row['digest']][1], []).append(row)
        if 1 not in by_no or any(len(v) != 1 for v in by_no.values()) or len(by_no) < 2:
            continue
        first = by_no[1][0]
        # A session must have one stable opponent pairing; never group across people/matches.
        if deck(first) != deck(anchor) or len({r['_pair'] for r in group}) != 1 or any(len(r['_pair']) != 2 for r in group):
            continue
        pool = Counter()
        for row in group:
            pool |= combined(row)
        # Match eligibility is anchored to game1; displayed Side belongs to the
        # selected build. Subtract its Main+Extra to avoid counting swapped-in
        # copies both in Main and in inferred Side on historical game2/3 pages.
        candidate = pool - combined(selected)
        if not candidate:
            continue
        kind = 'same_match' if session == chosen_session else 'historical_same_build'
        evidence = [dict(digest=r['digest'], recorded_at=r['recorded_at'], expires_at=r['expires_at'], game_no=metadata[r['digest']][1]) for r in sorted(group, key=lambda r: metadata[r['digest']][1])]
        sources.append(dict(kind=kind, session_id=session, evidence=evidence))
        for code, count in candidate.items():
            inferred[code] = max(inferred[code], count)
            per_card.setdefault(code, []).append(dict(kind=kind, evidence=evidence))
        session_sets[session] = dict(cards=dict(candidate),
                                     time=max(r['recorded_at'] for r in group),
                                     tag=first.get('_opponent_deck_type'),
                                     turn=first.get('first_player'))

    def weighted_score():
        """记忆检索：按时间衰减 + 对手 tag 一致性给每张卡打分。

        目标不是"最近窗口"，而是"当前 15 张 side deck 里最可能的卡"：
        - 时间衰减让旧配置降权，但保留为 latent；
        - 一张卡只在某个 tag 下出现，取该 tag 的加权和（×1.5），避免被全局频率埋没。
        """
        newest = max((info['time'] for info in session_sets.values()), default=0.0)
        tau = 30 * 86400.0
        total = Counter()
        tag_support = {}
        last_seen = {}
        for info in session_sets.values():
            weight = math.exp(-max(0.0, newest - info['time']) / tau)
            tag = info.get('tag')
            for code in info['cards']:
                total[code] += weight
                last_seen[code] = max(last_seen.get(code, 0.0), info['time'])
                if tag:
                    bucket = tag_support.setdefault(code, {})
                    bucket[tag] = bucket.get(tag, 0.0) + weight
        score = {}
        for code, weight in total.items():
            tag_max = max(tag_support.get(code, {}).values(), default=0.0)
            score[code] = max(weight, 1.5 * tag_max)
        return score, last_seen

    score, last_seen = weighted_score()
    observed_copies = sum(inferred.values())
    if observed_copies <= 15:
        # 观测并集还没超过 15，直接全保留；不足 15 也不猜。
        chosen = Counter(inferred)
        basis = 'full_union'
        used = list(session_sets)
    else:
        chosen = Counter()
        same_match_codes = {code for code, evidence in per_card.items()
                            if any(item['kind'] == 'same_match' for item in evidence)}
        for code, _ in sorted(score.items(), key=lambda kv: (kv[0] not in same_match_codes, -kv[1], -inferred[kv[0]], kv[0])):
            room = 15 - sum(chosen.values())
            if room <= 0:
                break
            take = min(int(inferred[code]), room)
            if take > 0:
                chosen[code] = take
        basis = 'support'
        used = [s for s, info in session_sets.items() if set(info['cards']) & set(chosen)]
    omitted = sorted(set(inferred) - set(chosen))
    latent = [dict(code=code, copies=inferred[code], score=round(score.get(code, 0.0), 4),
                   last_seen=last_seen.get(code), observed_sessions=len(per_card[code]))
              for code in omitted]
    direct = Counter()
    for c in cards[selected['build']]:
        if c['partition']=='side': direct[mapping.get(c['code'],(c['code'],None))[0]] += c['copies']
    observed_overflow = observed_copies > 15
    return dict(cards=[dict(code=code, copies=count, sources=per_card[code],
                            observed_sessions=len(per_card[code]),
                            score=round(score.get(code, 0.0), 4),
                            confidence=round(score.get(code, 0.0) / (score.get(code, 0.0) + 1), 3))
                       for code, count in sorted(chosen.items())],
                observed_cards=[dict(code=code, copies=count,
                                     observed_sessions=len(per_card[code]),
                                     score=round(score.get(code, 0.0), 4))
                                for code, count in sorted(inferred.items())],
                latent_cards=latent,
                omitted_codes=omitted,
                sources=sources, complete=False,
                conflict=(sum((chosen | direct).values()) > 15 or
                          any(sum(info['cards'].values()) > 15 for session, info in session_sets.items() if session == chosen_session)),
                observed_overflow=observed_overflow,
                selection_basis=basis,
                selected_sessions=len(used), observed_sessions=len(session_sets),
                basis='game1_delta', anchor_digest=anchor['digest'],
                score_tau_days=30,
                reason='observed_game1_delta_support_with_latent_not_full_side')


def _filename_part(value, fallback):
    text = str(value or '').strip()
    text = ''.join(ch if ch not in '\\/:*?"<>|' and ord(ch) >= 32 else '_' for ch in text)
    text = ' '.join(text.split())[:60].strip()
    return text or fallback


def ydk_filename(result, inferred=False):
    """返回 (UTF-8 文件名, ASCII 回退名)：玩家名 排名 卡组名.ydk。"""
    person = result.get('person') or {}
    digest = str((result.get('selected') or {}).get('digest') or '')
    player = _filename_part(person.get('player'), '未知玩家')
    rank = person.get('rank')
    rank_text = str(rank) if isinstance(rank, int) and rank > 0 else '排名未知'
    label = _filename_part(person.get('deck_type'), '未识别')
    fallback = 'build-' + digest[:16] + '.ydk'
    return f'{player} {rank_text} {label}.ydk', fallback


def ydk(result, include_inferred=False):
    partitions = {p: Counter() for p in ('main', 'extra', 'side')}
    for card in result['cards']:
        partitions[card['partition']][card['code']] += card['copies']
    if include_inferred:
        if result['inferred_side']['conflict']:
            raise ValueError('side_candidates_exceed_limit')
        for card in result['inferred_side']['cards']:
            partitions['side'][card['code']] = max(partitions['side'][card['code']], card['copies'])
        if sum(partitions['side'].values()) > 15:
            raise ValueError('side_candidates_exceed_limit')
    lines = ['#created by local statistics']
    side_rank = {code: index for index, code in enumerate(result.get('side_order') or [])}
    for part, label in (('main', '#main'), ('extra', '#extra'), ('side', '!side')):
        if part == 'side' and include_inferred:
            lines.append('# inferred Side candidates are included; not a complete side deck')
        lines.append(label)
        items = list(partitions[part].items())
        if part == 'side':
            items.sort(key=lambda item: (side_rank.get(item[0], len(side_rank)), item[0]))
        for code, copies in items:
            lines.extend([str(code)] * copies)
    return '\n'.join(lines) + '\n'
