"""Private build page facts and explicitly inferred Side, from eligible original sources."""
from collections import Counter
from .statistics_dashboard import population
from .player_statistics import capture_metadata
from .card_statistics import win_rate


def query(db, replay_root, now, player, digest=None, scope='build'):
    if not isinstance(player, str) or not player or len(player) > 128 or scope not in ('build', 'player'):
        raise ValueError('invalid_player_or_scope')
    basis, latest, people = population(db, replay_root, now)
    rows = [r for r in basis['views'] if r['player'] == player]
    selected = next((r for r in rows if r['digest'] == digest), None) if digest else latest.get(player)
    if selected is None:
        return None
    person = next(p for p in people if p['player'] == player)
    metadata = capture_metadata(db, {r['digest'] for r in rows})
    explicit_capture = set()
    import json
    for evidence in db.execute('SELECT digest,payload_json FROM stat_evidence'):
        try:
            capture = json.loads(evidence['payload_json']).get('capture')
            if isinstance(capture,dict) and ('session_id' in capture or 'game_no' in capture): explicit_capture.add(evidence['digest'])
        except (ValueError,AttributeError): pass
    # Reuse already-recorded legacy session associations, never infer grouping from time alone.
    legacy = {}
    for record in db.execute('SELECT f.digest,s.session_id,s.game_no FROM stat_files f JOIN replay_sessions s ON s.replay_id=f.replay_id JOIN replays r ON r.id=f.replay_id WHERE r.focus_player=?', (player,)):
        if isinstance(record['session_id'], str) and record['session_id'] and type(record['game_no']) is int and record['game_no'] > 0:
            legacy.setdefault(record['digest'], set()).add((record['session_id'], record['game_no'], True))
    for d, values in legacy.items():
        if d not in metadata and d not in explicit_capture and len(values) == 1:
            metadata[d] = next(iter(values))
    builds = {r['build'] for r in rows}
    cards = {build: [] for build in builds}
    for r in db.execute('SELECT build,partition,code,copies FROM stat_build_cards ORDER BY partition,code'):
        if r['build'] in cards:
            cards[r['build']].append(dict(partition=r['partition'], code=r['code'], copies=r['copies']))
    ranks = {m['player']: m['rank'] for m in basis['roster']['members']}
    matches = []
    for row in sorted(rows, key=lambda r: (r['recorded_at'], r['digest']), reverse=True):
        if scope == 'build' and row['build'] != selected['build']:
            continue
        opponents = [dict(r) for r in db.execute('SELECT v.player,b.deck_type FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build WHERE v.digest=? AND v.player<>?', (row['digest'], player))]
        other = opponents[0] if len(opponents) == 1 else dict(player=None, deck_type=None)
        meta = metadata.get(row['digest'])
        matches.append(dict(player=player, digest=row['digest'], build=row['build'], deck_type=row['deck_type'],
                            recorded_at=row['recorded_at'], expires_at=row['expires_at'], first_player=row['first_player'],
                            outcome=row['outcome'], current_rank=row['rank'], historical_rank=None,
                            opponent=other['player'], opponent_deck_type=other['deck_type'],
                            opponent_current_rank=ranks.get(other['player']), opponent_historical_rank=None,
                            game_no=meta[1] if meta else None, session_id=meta[0] if meta else None))
    pairs = {}
    for opponent in db.execute('SELECT digest,player FROM stat_views'):
        pairs.setdefault(opponent['digest'], []).append(opponent['player'])
    for row in rows:
        row['_pair'] = tuple(sorted(pairs.get(row['digest'], [])))
    from .card_identity import reference
    mapping, _, _ = reference(db)
    inferred = infer_side(rows, selected, cards, metadata, mapping)
    return dict(generated_at=now, person=dict(person, deck_type=selected['deck_type']),
                roster={k: v for k, v in basis['roster'].items() if k != 'members'},
                selected=dict(digest=selected['digest'], build=selected['build'], recorded_at=selected['recorded_at'],
                              expires_at=selected['expires_at'], observation_kind=person['observation_kind'] if selected['digest'] == latest[player]['digest'] else 'historical_observation'),
                cards=cards[selected['build']], completeness={p: bool(selected[p + '_complete']) for p in ('main', 'extra', 'side')},
                inferred_side=inferred, matches=matches, scope=scope, scope_win_rate=win_rate(matches),
                all_player_win_rate=win_rate(rows), all_player_games=len(rows),
                monthly_official=None, pt_history=[], pt_history_status='no_recorded_history',
                rank_semantics='current_roster_ranks_separate_from_unknown_historical_ranks',
                side_semantics='inferred_candidates_not_complete_or_statistical_facts')


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
    anchor = own_first[0] if len(own_first) == 1 else selected
    inferred = Counter(); sources = []; per_card = {}
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
        candidate = pool - combined(selected)
        if not candidate:
            continue
        kind = 'same_match' if session == chosen_session else 'historical_same_build'
        evidence = [dict(digest=r['digest'], recorded_at=r['recorded_at'], expires_at=r['expires_at'], game_no=metadata[r['digest']][1]) for r in sorted(group, key=lambda r: metadata[r['digest']][1])]
        sources.append(dict(kind=kind, session_id=session, evidence=evidence))
        for code, count in candidate.items():
            inferred[code] = max(inferred[code], count)
            per_card.setdefault(code, []).append(dict(kind=kind, evidence=evidence))
    direct = Counter()
    for c in cards[selected['build']]:
        if c['partition']=='side': direct[mapping.get(c['code'],(c['code'],None))[0]] += c['copies']
    return dict(cards=[dict(code=code, copies=count, sources=per_card[code]) for code, count in sorted(inferred.items())],
                sources=sources, complete=False, conflict=sum((inferred | direct).values()) > 15,
                reason='observed_maximum_pool_minus_selected_main_and_extra_not_full_side')


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
    lines = ['#created by local statistics' + ('; inferred Side candidates included, NOT complete' if include_inferred else '; directly recorded cards only')]
    for part, label in (('main', '#main'), ('extra', '#extra'), ('side', '!side')):
        lines.append(label)
        for code, copies in partitions[part].items():
            lines.extend([str(code)] * copies)
    return '\n'.join(lines) + '\n'
