"""有证据的 Side 换入/换出汇总；只供统计网页只读查询。

口径固定，不另造 Side 规则：
- 逐场证据直接复用 ``statistics_build_detail.swap_changes``：只比较同一场第 2/3 局
  与第 1 局的主卡+额外，每张卡取各局新增的**最大**张数，不跨局累加；
- 推断候选直接复用 ``statistics_build_detail.infer_side``（当前冻结的 Side 算法）；
- 本模块只做筛选、汇总和分母说明，不改变任何 Side/命名/排序规则。

未知与证据不足不进入分母，也不当作“没有换牌”：
缺唯一首局、局次冲突、场次未可靠关联、卡表不完整、身份不可靠的场次单独计数并展示。
"""
from collections import Counter
import sqlite3

from .statistics_build_detail import infer_side, swap_changes, query as build_detail_query
from .top100 import eligible_views
from .statistics_match_identity import identity_metadata
from .card_identity import reference as identity_reference
from .card_statistics import win_rate

VERSION = 1
OUTCOMES = ('win', 'loss', 'draw')
MAX_SESSIONS = 60
MAX_PLAYER_DISTRIBUTION = 20
MAX_DECK_DISTRIBUTION = 30
CHUNK = 400

STATUS_LABELS = {
    'observed': '有明确换边证据',
    'no_later_games': '只有首局，缺少换边局',
    'missing_first_game': '缺少可靠首局',
    'ambiguous_sequence': '局次关联冲突',
    'identity_unreliable': '场次身份不可靠',
    'partial_build': '卡表不完整',
    'missing_build': '缺少当场卡表',
    'pair_conflict': '双方玩家关联不唯一',
    'unassociated': '场次未可靠关联',
}

SEMANTICS = {
    'unit': 'session',
    'session': '同一场（同一 session_id + 同一双方玩家）',
    'dedup': '同一场多局同一张卡取各局新增的最大张数；不把两局换入相加成多张备牌',
    'frequency': '换入/换出频次 = 出现该方向的场次数 / 有效场次数',
    'copies': '单场张数分布单独展示，不与跨场频次相加',
    'denominator': '换入/换出证据按“唯一可靠首局 + 至少一个换边局 + 相关卡表完整”计场；投入率/使用率另有明确胜负分母',
    'investment': '投入率按场：分子为该卡被明确换入且有明确胜负的场次，分母为全部有明确胜负的换边有效场次；同场多局不重复计场',
    'usage_win_rate': '使用后胜率按换入小局：只算该卡相对首局换入且该小局实际使用、胜负明确的小局；平局进分母',
    'unknown': '缺首局、局次冲突、未关联、身份不可靠、卡表不完整均不计入分母，也不视为未换牌',
    'algorithm': '既有 infer_side 算法的推断候选，不是当场确定事实，也不等于完整 Side',
}


def _chunks(values, size=CHUNK):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _marks(values):
    return ','.join('?' for _ in values)


def _pairs(db, digests):
    """digest -> 该小局实际出现的双方玩家（用于识别一场的双方）。"""
    result = {}
    for chunk in _chunks(digests):
        for row in db.execute('SELECT digest,player FROM stat_views WHERE digest IN (%s)'
                              % _marks(chunk), chunk):
            result.setdefault(row['digest'], set()).add(row['player'])
    return {digest: tuple(sorted(players)) for digest, players in result.items()}


def _rows_with_decks(db, digests):
    """读取同一批 digest 的双方当场构筑标签；只用于场次级卡组方向与标签。

    玩家页的 eligible view 只含目标玩家，对手的场次级标签要单独读取；
    这里不参与资格判定，资格只认 eligible_views 的结果。
    """
    rows = []
    for chunk in _chunks(digests):
        rows.extend(dict(row) for row in db.execute(
            'SELECT v.digest,v.player,v.build,v.outcome,v.first_player,v.usage_complete,'
            'b.deck_type,b.main_complete,b.extra_complete,b.side_complete,g.recorded_at '
            'FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build '
            'JOIN stat_games g ON g.digest=v.digest WHERE v.digest IN (%s)'
            % _marks(chunk), chunk))
    return rows


def _all_session_digests(db, sample_digests):
    """样本场次的所有局 digest；只用于换边比较，不扩到别的场次。"""
    sample_digests = {digest for digest in sample_digests if digest}
    meta = identity_metadata(db, sample_digests)
    session_ids = {value[0] for value in meta.values()
                   if value and isinstance(value[0], str) and value[0]
                   and not value[0].startswith('unverified:')}
    if not session_ids:
        return set(sample_digests)
    result = set(sample_digests)
    ids = sorted(session_ids)
    for chunk in _chunks(ids):
        marks = _marks(chunk)
        try:
            result.update(row['digest'] for row in db.execute(
                "SELECT digest FROM stat_evidence WHERE "
                "json_extract(payload_json,'$.capture.session_id') IN (%s)" % marks,
                chunk))
        except sqlite3.DatabaseError:
            # JSON1 不可用时不把整库 payload 读进内存；保持样本 digest，
            # 由后续证据状态把缺局明确标成未计入。
            pass
        result.update(row['digest'] for row in db.execute(
            'SELECT f.digest FROM stat_files f JOIN replay_sessions s '
            'ON s.replay_id=f.replay_id WHERE s.session_id IN (%s)' % marks, chunk))
    return result


def _group_by_session(rows, metadata, pairs):
    """按 (session_id, 双方玩家) 分组；未关联或双方不唯一的视角单独返回。"""
    groups, unassociated = {}, []
    for row in rows:
        meta = metadata.get(row['digest'])
        pair = pairs.get(row['digest'], ())
        if meta and meta[0] and len(pair) == 2:
            groups.setdefault((meta[0], pair), []).append(row)
        else:
            unassociated.append(row)
    return groups, unassociated


def _session_labels(rows, metadata):
    """每位玩家在该场的卡组名：优先唯一可靠首局，否则按最早的已知局次。"""
    by_player = {}
    for row in rows:
        by_player.setdefault(row['player'], []).append(row)
    labels = {}
    for player, items in by_player.items():
        first = [row for row in items
                 if metadata.get(row['digest'], (None, 0, False))[1:] == (1, True)]
        if len(first) == 1:
            labels[player] = first[0].get('deck_type') or None
            continue
        known = [row for row in items if row.get('deck_type')]
        known.sort(key=lambda row: (metadata.get(row['digest'], (None, 99, False))[1] or 99,
                                    row.get('recorded_at') or 0))
        labels[player] = known[0].get('deck_type') if known else None
    return labels


def _cards_by_build(db, builds):
    result = {build: [] for build in builds}
    for chunk in _chunks(builds):
        for row in db.execute('SELECT build,partition,code,copies FROM stat_build_cards '
                              'WHERE build IN (%s)' % _marks(chunk), chunk):
            if row['build'] in result:
                result[row['build']].append(dict(partition=row['partition'],
                                                 code=row['code'], copies=row['copies']))
    return result


def _combine_change_rows(changes):
    """把逐局 change 汇总为每张卡的最大新增/减少张数，不跨局累加。"""
    added, removed = {}, {}
    for change in changes:
        for side, bucket in (('added', added), ('removed', removed)):
            totals, partitions = {}, {}
            for card in change.get(side, []):
                totals[card['code']] = totals.get(card['code'], 0) + card['copies']
                partitions.setdefault(card['code'], set()).add(card['partition'])
            for code, copies in totals.items():
                item = bucket.setdefault(code, dict(code=code, copies=0, partitions=set()))
                item['copies'] = max(item['copies'], copies)
                item['partitions'] |= partitions.get(code, set())
    return ([dict(code=item['code'], copies=item['copies'], partitions=sorted(item['partitions']))
             for item in sorted(added.values(), key=lambda value: value['code'])],
            [dict(code=item['code'], copies=item['copies'], partitions=sorted(item['partitions']))
             for item in sorted(removed.values(), key=lambda value: value['code'])])


def _combine_changes(first, later, cards, mapping):
    """同一场的多局都相对首局比较；每张卡保留最大新增/减少张数，不累加。"""
    return _combine_change_rows([swap_changes(first, row, cards, mapping)
                                 for row in later])


def _session_evidence(rows, target, metadata, labels, cards, mapping):
    """一场里某位玩家的逐局换边证据；返回状态、逐局差量与可展示说明。

    有效（observed）要求：唯一可靠首局、局次唯一、双方关联唯一，且首局与所有
    参与比较的换边局主卡+额外都完整。任一项不满足就单独标记，不进入分母。
    """
    own = [row for row in rows if row['player'] == target]
    opponent = next((row['player'] for row in rows if row['player'] != target), None)
    base = dict(player=target, opponent=opponent,
                player_deck=labels.get(target), opponent_deck=labels.get(opponent),
                status='unassociated', added=[], removed=[], games=[])
    if len({row['player'] for row in rows}) != 2 or opponent is None:
        base.update(status='pair_conflict')
        return base
    by_no, unreliable = {}, False
    for row in own:
        meta = metadata.get(row['digest'])
        game_no = meta[1] if meta else 0
        if not meta or not meta[0] or not meta[2] or game_no <= 0:
            unreliable = True
        by_no.setdefault(game_no, []).append(row)
    ordered = sorted(own, key=lambda item: (metadata.get(item['digest'], (None, 99, False))[1] or 99,
                                            item.get('recorded_at') or 0))
    base.update(recorded_at=max((row.get('recorded_at') or 0) for row in own),
                games=[dict(digest=row['digest'],
                            game_no=(metadata.get(row['digest']) or (None, 0, False))[1],
                            recorded_at=row.get('recorded_at'), deck_type=row.get('deck_type'),
                            outcome=row.get('outcome'),
                            usage_complete=bool(row.get('usage_complete')),
                            main_complete=bool(row.get('main_complete')),
                            extra_complete=bool(row.get('extra_complete')),
                            side_complete=bool(row.get('side_complete')))
                       for row in ordered])
    if unreliable:
        base.update(status='identity_unreliable')
        return base
    if 1 not in by_no:
        base.update(status='missing_first_game')
        return base
    if len(by_no[1]) != 1 or any(len(items) != 1 for number, items in by_no.items() if number > 1):
        base.update(status='ambiguous_sequence')
        return base
    first = by_no[1][0]
    later = [items[0] for number, items in sorted(by_no.items()) if number > 1]
    numbers = sorted(by_no)
    missing_games = [number for number in range(1, numbers[-1] + 1)
                     if number not in by_no] if numbers else []
    base.update(game_count=max(by_no), later_games=len(later), missing_games=missing_games,
                result=first.get('outcome'), outcome=first.get('outcome'),
                first_player=first.get('first_player'),
                first_build=first['build'], direct_side=bool(first.get('side_complete')))
    if not later:
        base.update(status='no_later_games')
        return base
    if not first.get('main_complete') or not first.get('extra_complete'):
        base.update(status='partial_build', unknown_partitions=['main', 'extra'])
        return base
    unknowns = []
    for row in later:
        for part in ('main', 'extra'):
            if not row.get(part + '_complete'):
                unknowns.append(part)
    if unknowns:
        # 完整分区的已知差异仍展示，便于人工核对；该场不进入汇总分母。
        known = [row for row in later if row.get('main_complete') and row.get('extra_complete')]
        added, removed = _combine_changes(first, known, cards, mapping) if known else ([], [])
        base.update(status='partial_build', unknown_partitions=sorted(set(unknowns)),
                    added=added, removed=removed)
        return base
    if any(row['build'] not in cards for row in [first] + later):
        base.update(status='missing_build')
        return base
    changes = [swap_changes(first, row, cards, mapping) for row in later]
    added, removed = _combine_change_rows(changes)
    base.update(status='observed', added=added, removed=removed,
                swap_games=[dict(digest=row['digest'],
                                 game_no=(metadata.get(row['digest']) or (None, 0, False))[1],
                                 outcome=row.get('outcome'),
                                 usage_complete=bool(row.get('usage_complete')),
                                 first_player=row.get('first_player'),
                                 added=list(change.get('added', [])),
                                 removed=list(change.get('removed', [])))
                            for row, change in zip(later, changes)])
    base['unknown_outcome_games'] = [game['game_no'] for game in base['swap_games']
                                     if game.get('outcome') not in OUTCOMES]
    return base


def _usage_facts(db, digests):
    """side-in 小局的 (digest,player)->[(raw_code,used)]；used=1 是正向证据。"""
    result = {}
    for chunk in _chunks(digests):
        for row in db.execute(
                'SELECT digest,player,code,used FROM stat_card_facts '
                'WHERE digest IN (%s)' % _marks(chunk), chunk):
            result.setdefault((row['digest'], row['player']), []).append(
                (row['code'], row['used']))
    return result


def _investment_and_usage(db, rate_sessions, added_cards, mapping):
    """按场计算投入率，按换入且使用的小局计算使用后胜率。

    rate_sessions 已按“首局可靠 + 换边比较完整 + 该换边小局胜负明确”筛过；
    每场至少一个有效换边小局。投入率分母是这些场次；使用后胜率只算其中
    “该卡换入且 used=1”的有效小局。未知/明确未使用另列，不混入正式指标。
    """
    def canonical(code):
        return mapping.get(code, (code,))[0]

    denomination = len(rate_sessions)
    investment, usage_games, game_by_key = {}, {}, {}
    for session in rate_sessions:
        for card in session['added']:
            item = investment.setdefault(card['code'], dict(
                sessions=0, copies=Counter(), players=set()))
            item['sessions'] += 1
            item['copies'][int(card['copies'])] += 1
            item['players'].add(session['player'])
        for game in session.get('valid_games', []):
            for card in game.get('added', []):
                key = (game['digest'], session['player'], card['code'])
                usage_games.setdefault(card['code'], set()).add(key)
                game_by_key[key] = game
    facts = _usage_facts(db, {key[0] for keys in usage_games.values() for key in keys})
    usage = {}
    for code, keys in usage_games.items():
        item = dict(side_in_games=0, used_games=0, used_known_games=0,
                    used_unknown_outcome_games=0, explicit_unused_games=0,
                    usage_unknown_games=0)
        used_rows = []
        for digest, player, _ in sorted(keys):
            game = game_by_key.get((digest, player, code))
            if game is None:
                continue
            item['side_in_games'] += 1
            rows = facts.get((digest, player), ())
            positive = any(used == 1 and canonical(raw) == code for raw, used in rows)
            if positive:
                item['used_games'] += 1
                if game.get('outcome') in OUTCOMES:
                    item['used_known_games'] += 1
                    used_rows.append(dict(outcome=game['outcome']))
                else:
                    item['used_unknown_outcome_games'] += 1
            elif game.get('usage_complete'):
                item['explicit_unused_games'] += 1
            else:
                item['usage_unknown_games'] += 1
        item['win_rate'] = win_rate(used_rows)
        item['rate_basis'] = 'used_side_in_small_games_with_known_outcome'
        usage[code] = item
    for card in added_cards:
        code = card['code']
        spec = investment.get(code, dict(sessions=0, copies=Counter(), players=set()))
        card['investment'] = dict(
            sessions=spec['sessions'], denominator=denomination,
            percentage=round(100 * spec['sessions'] / denomination, 1) if denomination else None,
            copies={str(key): value for key, value in sorted(spec['copies'].items())},
            players=len(spec['players']),
            copies_max=max(spec['copies']) if spec['copies'] else 0)
        card['usage'] = usage.get(code, dict(
            side_in_games=0, used_games=0, used_known_games=0,
            used_unknown_outcome_games=0, explicit_unused_games=0,
            usage_unknown_games=0, win_rate=win_rate([]),
            rate_basis='used_side_in_small_games_with_known_outcome'))
    return dict(rate_sessions=denomination)


def _aggregate_cards(valid):
    """把逐场差量汇总为“场次频次 + 单场张数分布”；同一场每卡只记一次。"""
    result = {}
    for session in valid:
        for direction in ('added', 'removed'):
            for card in session[direction]:
                item = result.setdefault((direction, card['code']), dict(
                    code=card['code'], sessions=0, copies=Counter(), partitions=Counter(),
                    players=set(), first_seen=None, last_seen=None, examples=[]))
                item['sessions'] += 1
                item['copies'][int(card['copies'])] += 1
                for part in card.get('partitions') or []:
                    item['partitions'][part] += 1
                item['players'].add(session['player'])
                stamp = session.get('recorded_at') or 0
                item['first_seen'] = stamp if item['first_seen'] is None else min(item['first_seen'], stamp)
                item['last_seen'] = stamp if item['last_seen'] is None else max(item['last_seen'], stamp)
                if len(item['examples']) < 3:
                    item['examples'].append(dict(digest=(session.get('games') or [{}])[0].get('digest'),
                                                 recorded_at=stamp,
                                                 opponent_deck=session.get('opponent_deck'),
                                                 game_no=session.get('game_count'),
                                                 copies=int(card['copies']),
                                                 partitions=list(card.get('partitions') or [])))
    output = {'added': [], 'removed': []}
    denominator = len(valid)
    for (direction, code), item in result.items():
        output[direction].append(dict(
            code=code, sessions=item['sessions'],
            percentage=round(100 * item['sessions'] / denominator, 1) if denominator else None,
            max_copies=max(item['copies']) if item['copies'] else 0,
            copies={str(key): value for key, value in sorted(item['copies'].items())},
            partitions={key: value for key, value in item['partitions'].items()},
            players=len(item['players']),
            first_seen=item['first_seen'], last_seen=item['last_seen'],
            examples=item['examples']))
    for direction in ('added', 'removed'):
        output[direction].sort(key=lambda card: (-card['sessions'], -card['max_copies'], card['code']))
    return output


def _direct_side(detail, cards):
    """直接记录的 Side 是独立证据，没有换入/换出方向，单独列出。"""
    result, recorded = {}, 0
    for session in detail:
        if not session.get('direct_side'):
            continue
        build = session.get('first_build')
        if not build or build not in cards:
            continue
        side = [card for card in cards[build] if card['partition'] == 'side' and card['copies'] > 0]
        if not side:
            continue
        recorded += 1
        for card in side:
            item = result.setdefault(card['code'], dict(code=card['code'], sessions=0, max_copies=0))
            item['sessions'] += 1
            item['max_copies'] = max(item['max_copies'], int(card['copies']))
    return dict(sessions=recorded,
                cards=sorted(result.values(), key=lambda card: (-card['sessions'],
                                                                -card['max_copies'], card['code'])))


def _empty_side(key, role, deck=None):
    return dict(key=key, role=role, deck=deck,
                sample=dict(unit='session', sessions_matched=0, valid_sessions=0,
                            valid_outcome_sessions=0, outcome_unknown_sessions=0,
                            sessions_with_changes=0, sessions_without_changes=0,
                            sessions_with_gaps=0, side_games=0, later_games=0,
                            game_counts={}, seat=dict(first=0, second=0, unknown=0),
                            seat_filtered=0,
                            players=0, opponents=0, excluded={}, unassociated_views=0,
                            by_player=[], by_opponent_deck=[], own_decks=[],
                            date_range=None),
                observed=dict(added=[], removed=[],
                              direct_side=dict(sessions=0, cards=[]),
                              unknown_outcome=dict(cards=[], sessions=0)),
                sessions=[], sessions_total=0, sessions_truncated=False,
                algorithm=dict(available=False, reason=None, players_total=0,
                               players_analyzed=0, players_skipped=0, cards=[],
                               latent_cards=[], observed_only=[], reason_counts={},
                               anchors=[], selection_basis={}, notes=[]),
                notes=[])


def _first_game_row(rows, player, metadata):
    """该玩家在该场唯一可靠的首局视角；没有唯一首局时返回 None。"""
    firsts = [row for row in rows if row['player'] == player and
              metadata.get(row['digest'], (None, 0, False))[1:] == (1, True)]
    return firsts[0] if len(firsts) == 1 else None


def _unknown_outcome_evidence(valid):
    """胜负未知的换边小局里明确换入的卡；只作单列说明，不进入正式指标。"""
    result = {}
    session_ids = set()
    for session in valid:
        for game in session.get('swap_games', []):
            if game.get('outcome') in OUTCOMES:
                continue
            for card in game.get('added', []):
                item = result.setdefault(card['code'], dict(
                    code=card['code'], sessions=set(), players=set(),
                    copies=Counter(), examples=[]))
                item['sessions'].add(session['session_id'])
                item['players'].add(session['player'])
                item['copies'][int(card['copies'])] += 1
                session_ids.add(session['session_id'])
                if len(item['examples']) < 3:
                    item['examples'].append(dict(session_id=session['session_id'],
                                                 game_no=game.get('game_no'),
                                                 outcome=game.get('outcome')))
    cards = [dict(code=item['code'], sessions=len(item['sessions']),
                  players=len(item['players']),
                  copies={str(key): value for key, value in sorted(item['copies'].items())},
                  max_copies=max(item['copies']) if item['copies'] else 0,
                  examples=item['examples'])
             for item in result.values()]
    cards.sort(key=lambda card: (-card['sessions'], -card['max_copies'], card['code']))
    return dict(cards=cards, sessions=len(session_ids))


def _side_payload(db, key, role, deck, spec, groups, metadata, cards, mapping,
                  unassociated_views, first=None):
    """汇总一侧的逐场证据；返回 (side, 算法目标信息, 观测到的换入卡)。"""
    side = _empty_side(key, role, deck)
    detail, valid, excluded = [], [], Counter()
    target_matched, target_valid = Counter(), Counter()
    all_opponents, own_decks, opponent_decks = set(), Counter(), Counter()
    seats, game_counts = Counter(), Counter()
    side_games, later_games, matched_sessions = 0, 0, set()
    observed_codes, anchors = {}, {}
    seat_filtered = 0

    for (session_id, pair), rows in groups.items():
        labels = _session_labels(rows, metadata)
        targets = []
        if spec['mode'] == 'player':
            target = spec['player']
            if target not in pair:
                continue
            opponent = next((name for name in pair if name != target), None)
            if opponent is None:
                continue
            if spec['opponent_deck'] and labels.get(opponent) != spec['opponent_deck']:
                continue
            targets.append((target, opponent))
        else:
            # 矩阵：以“本格里的实际视角”为准（行视角的卡组+对手当场卡组），
            # 不再用场次级标签重新筛一遍；同一场的其它局只用于换边比较。
            group_pairs = {(row['player'], row['digest']) for row in rows}
            matched_players = {player for player, digest in spec.get('matched', ())
                               if (player, digest) in group_pairs}
            for target in pair:
                if target not in matched_players:
                    continue
                opponent = next((name for name in pair if name != target), None)
                if opponent is not None:
                    targets.append((target, opponent))
        if not targets:
            continue
        if first is not None and not spec.get('mirror'):
            # 与矩阵格“实际先后攻”一致：矩阵行我方=A；我方侧看 A，对手侧也看行玩家 A。
            kept = []
            for target, opponent in targets:
                seat_player = opponent if (spec['mode'] == 'matrix'
                                           and spec['key'] == 'opponent') else target
                seat_row = _first_game_row(rows, seat_player, metadata)
                wanted = 1 if first else 0
                if (seat_row is None or seat_row.get('first_player') not in (0, 1)
                        or seat_row.get('first_player') != wanted):
                    seat_filtered += 1
                    continue
                kept.append((target, opponent))
            targets = kept
            if not targets:
                continue
        matched_sessions.add(session_id)
        for target, opponent in targets:
            evidence = _session_evidence(rows, target, metadata, labels, cards, mapping)
            evidence['session_id'] = session_id
            if spec['mode'] == 'matrix':
                evidence['player_deck'] = spec['deck']
                evidence['opponent_deck'] = (spec['opponent_deck']
                                             or labels.get(opponent))
            target_matched[target] += 1
            if opponent:
                all_opponents.add(opponent)
            own_label = spec['deck'] if spec['mode'] == 'matrix' else labels.get(target)
            opp_label = (spec['opponent_deck'] or labels.get(opponent)
                         if spec['mode'] == 'matrix' else labels.get(opponent))
            own_decks[own_label or '未识别'] += 1
            opponent_decks[opp_label or '未识别'] += 1
            # 锚点：该玩家在这个方向上最近的唯一可靠首局（不要求有换边局）。
            own_rows = [row for row in rows if row['player'] == target]
            firsts = [row for row in own_rows
                      if metadata.get(row['digest'], (None, 0, False))[1:] == (1, True)]
            if len(firsts) == 1 and (target not in anchors or
                                     (firsts[0].get('recorded_at') or 0) >
                                     (anchors[target].get('recorded_at') or 0)):
                anchors[target] = firsts[0]
            if evidence['status'] == 'observed':
                valid.append(evidence)
                target_valid[target] += 1
                side_games += max(0, (evidence.get('game_count') or 1) - 1)
                later_games += evidence.get('later_games') or 0
                if evidence.get('game_count'):
                    game_counts[str(evidence['game_count'])] += 1
                seats['first' if evidence.get('first_player') == 1 else
                      'second' if evidence.get('first_player') == 0 else 'unknown'] += 1
                for card in evidence['added']:
                    observed_codes.setdefault(card['code'], set()).add(target)
            else:
                excluded[evidence['status']] += 1
            detail.append(evidence)

    unit = 'player_side' if spec.get('mirror') else 'session'
    denominator = len(valid)
    side['sample'].update(
        unit=unit, sessions_matched=len(matched_sessions), valid_sessions=denominator,
        sessions_with_changes=sum(1 for item in valid if item['added'] or item['removed']),
        sessions_without_changes=sum(1 for item in valid if not item['added'] and not item['removed']),
        sessions_with_gaps=sum(1 for item in valid if item.get('missing_games')),
        side_games=side_games, later_games=later_games,
        game_counts={key: value for key, value in sorted(game_counts.items())},
        seat=dict(first=seats['first'], second=seats['second'], unknown=seats['unknown']),
        seat_filtered=seat_filtered,
        players=len(target_matched), opponents=len(all_opponents),
        excluded=dict(sorted(excluded.items())), unassociated_views=unassociated_views,
        by_player=[dict(player=name, sessions=target_matched[name],
                        valid_sessions=target_valid.get(name, 0))
                   for name in sorted(target_matched,
                                      key=lambda name: (-target_valid.get(name, 0), name))[:MAX_PLAYER_DISTRIBUTION]],
        by_opponent_deck=[dict(deck=name, sessions=count)
                          for name, count in sorted(opponent_decks.items(),
                                                    key=lambda item: (-item[1], item[0]))[:MAX_DECK_DISTRIBUTION]],
        own_decks=[dict(deck=name, sessions=count)
                   for name, count in sorted(own_decks.items(),
                                             key=lambda item: (-item[1], item[0]))[:MAX_DECK_DISTRIBUTION]])
    stamps = [item.get('recorded_at') for item in detail if item.get('recorded_at')]
    if stamps:
        side['sample']['date_range'] = [min(stamps), max(stamps)]
    rate_valid = []
    for session in valid:
        valid_games = [game for game in session.get('swap_games', [])
                       if game.get('outcome') in OUTCOMES]
        if not valid_games:
            continue
        added, removed = _combine_change_rows(
            [dict(added=game.get('added', []), removed=game.get('removed', []))
             for game in valid_games])
        rate_valid.append(dict(session, added=added, removed=removed,
                               valid_games=valid_games))
    aggregate = _aggregate_cards(rate_valid)
    metrics = _investment_and_usage(db, rate_valid, aggregate['added'], mapping)
    side['sample']['valid_outcome_sessions'] = metrics['rate_sessions']
    side['sample']['outcome_unknown_sessions'] = len(valid) - metrics['rate_sessions']
    side['sample']['sessions_with_changes'] = sum(
        1 for item in rate_valid if item['added'] or item['removed'])
    side['sample']['sessions_without_changes'] = sum(
        1 for item in rate_valid if not item['added'] and not item['removed'])
    side['observed'].update(added=aggregate['added'], removed=aggregate['removed'],
                            direct_side=_direct_side(detail, cards),
                            unknown_outcome=_unknown_outcome_evidence(valid))
    detail.sort(key=lambda item: (item.get('recorded_at') or 0, item.get('player') or ''),
                reverse=True)
    side['sessions_total'] = len(detail)
    for item in detail:
        item.pop('first_build', None)
        item['status_label'] = STATUS_LABELS.get(item.get('status'), '状态未知')
    side['sessions'] = detail[:MAX_SESSIONS]
    side['sessions_truncated'] = len(detail) > MAX_SESSIONS
    if not valid:
        side['notes'].append('没有满足“唯一可靠首局 + 换边局卡表完整”的场次；'
                             '不把未知当作没有换牌，换入/换出与频次均不计算。')
    if excluded:
        side['notes'].append('另有 %d 个场次因证据不足未计入分母，原因见样本分布；'
                             '这些场次不是“没有换牌”。' % sum(excluded.values()))
    if side['sessions_truncated']:
        side['notes'].append('逐场明细只显示最近 %d 场；频次与分母仍按全部匹配场次计算。' % MAX_SESSIONS)
    if side['sample']['sessions_with_gaps']:
        side['notes'].append('有 %d 个有效场次缺少中间局次；只按已观测到的局次汇总，'
                             '缺局表示该局没有来源，不代表没有换牌。'
                             % side['sample']['sessions_with_gaps'])
    if seat_filtered:
        side['notes'].append('按实际先后攻筛选，另有 %d 个方向匹配场次不在本次筛选内，'
                             '未计入分母。' % seat_filtered)
    if side['sample']['outcome_unknown_sessions']:
        side['notes'].append('有 %d 个场次换边证据完整但所有相关小局胜负均未知，'
                             '未计入投入率分子/分母；单列不计为负或未使用。'
                             % side['sample']['outcome_unknown_sessions'])
    if first is not None and spec.get('mirror'):
        side['notes'].append('内战不按实际先后攻再筛选；先后攻筛选只用于非内战格子。')
    target_info = dict(anchors=anchors, valid=target_valid, matched=target_matched)
    return side, target_info, observed_codes


def _collect_algorithm(rows_by_player, target_info, metadata, cards, mapping,
                       observed_codes):
    """每位玩家单独调用既有 infer_side；只汇总候选人数，不把推断当确定事实。

    矩阵入口不调用本函数；玩家页只有一位玩家，因此这里不设“取前 N 名”的上限。
    """
    ordered = sorted(target_info['matched'],
                     key=lambda name: (-target_info['valid'].get(name, 0), name))
    analyzed = ordered
    aggregate, latent_aggregate = {}, {}
    reason_counts, basis_counts = Counter(), Counter()
    anchors, notes = [], []
    results = 0
    for player in analyzed:
        anchor = target_info['anchors'].get(player)
        rows = rows_by_player.get(player, [])
        if anchor is None:
            reason_counts['no_unique_first_game_in_scope'] += 1
            continue
        result = infer_side(rows, anchor, cards, metadata, mapping)
        reason_counts[str(result.get('reason') or 'unknown')] += 1
        basis_counts[str(result.get('selection_basis') or 'none')] += 1
        if result.get('conflict'):
            reason_counts['conflict'] += 1
        results += 1
        anchors.append(dict(player=player, digest=anchor['digest'],
                            recorded_at=anchor.get('recorded_at'),
                            deck_type=anchor.get('deck_type'),
                            game_no=(metadata.get(anchor['digest']) or (None, 0, False))[1]))
        for card in result.get('cards', []):
            code = card['code']
            item = aggregate.setdefault(code, dict(code=code, players=0, copies_max=0,
                                                   score_max=0.0, latent_players=0,
                                                   observed_players=0))
            item['players'] += 1
            item['copies_max'] = max(item['copies_max'], int(card.get('copies') or 0))
            item['score_max'] = max(item['score_max'], float(card.get('score') or 0))
            if player in observed_codes.get(code, ()):
                item['observed_players'] += 1
        for card in result.get('latent_cards', []):
            code = card['code']
            item = latent_aggregate.setdefault(code, dict(code=code, players=0, copies_max=0,
                                                          score_max=0.0, observed_players=0))
            item['players'] += 1
            item['copies_max'] = max(item['copies_max'], int(card.get('copies') or 0))
            item['score_max'] = max(item['score_max'], float(card.get('score') or 0))
            if player in observed_codes.get(code, ()):
                item['observed_players'] += 1
    for code, item in latent_aggregate.items():
        if code in aggregate:
            aggregate[code]['latent_players'] = item['players']
    observed_only = sorted(code for code, players in observed_codes.items()
                           if code not in aggregate and (players & set(analyzed)))
    cards_out = sorted(aggregate.values(),
                       key=lambda item: (-item['players'], -item['observed_players'],
                                         -item['copies_max'], item['code']))
    latent_out = sorted((item for item in latent_aggregate.values()
                         if item['code'] not in aggregate),
                        key=lambda item: (-item['players'], -item['observed_players'], item['code']))
    skipped = len(ordered) - results
    reason = None
    if not results:
        reason = ('no_unique_first_game_in_scope'
                  if any(key == 'no_unique_first_game_in_scope' for key in reason_counts)
                  else 'no_unique_first_game')
    if skipped:
        notes.append('有 %d 名匹配玩家没有可用于推断的唯一可靠首局，未纳入历史候选汇总。'
                     % skipped)
    if not results:
        notes.append('本范围内没有可用于既有算法推断的唯一可靠首局；不生成候选，'
                     '也不当作没有 Side。')
    notes.append('候选为既有算法推断，不是当场确定事实；与逐场明确观测分开显示，不并入 YDK。')
    return dict(available=bool(results), reason=reason, players_total=len(ordered),
                players_analyzed=results, players_skipped=skipped,
                cards=cards_out, latent_cards=latent_out, observed_only=observed_only,
                reason_counts=dict(reason_counts), anchors=anchors[:6],
                selection_basis=dict(basis_counts), notes=notes)


def query(db, replay_root, now, player=None, deck=None, opponent_deck=None,
          mode='player', internal_only=False, algorithm=True, first=None,
          cell_token=None, digest=None, scope='build'):
    """玩家页与对战矩阵共用的只读查询。

    mode='player'：player 必填，按该玩家的场次汇总；opponent_deck 可选。
                   scope='build' 且给 digest 时，只统计该构筑（主卡相同）的场次；
                   scope='player' 时保持该玩家全部有效小局。
    mode='matrix'：deck（我方/行）必填，opponent_deck（对手/列）可选；
                   方向固定为 row -> column；内战按玩家-场次合并。
    first：matrix 可传 all/first/second，与矩阵格的“实际先后攻”一致。
    矩阵只汇总本格逐场观测；历史 infer_side 候选只在玩家页计算。
    """
    if mode not in ('player', 'matrix'):
        raise ValueError('分析入口无效。')
    if scope not in ('build', 'player'):
        raise ValueError('Side 范围无效。')
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                               or any(char not in '0123456789abcdef' for char in digest)):
        raise ValueError('来源编号无效。')
    if first not in (None, 'all', 'first', 'second'):
        raise ValueError('先后攻条件无效。')
    first = None if first in (None, 'all') else first == 'first'
    # 矩阵格的主要结论必须来自本格实际场次；玩家历史候选不作为矩阵结论。
    algorithm = bool(algorithm) and mode == 'player'
    if player is not None and (not isinstance(player, str) or not player.strip()
                               or len(player) > 128):
        raise ValueError('玩家名称无效。')
    if deck is not None and (not isinstance(deck, str) or not deck.strip() or len(deck) > 128):
        raise ValueError('卡组条件无效。')
    if opponent_deck is not None and (not isinstance(opponent_deck, str)
                                      or not opponent_deck.strip() or len(opponent_deck) > 128):
        raise ValueError('对手卡组条件无效。')
    player = player.strip() if isinstance(player, str) else None
    deck = deck.strip() if isinstance(deck, str) else None
    opponent_deck = opponent_deck.strip() if isinstance(opponent_deck, str) else None
    if mode == 'player' and not player:
        raise ValueError('需要玩家名称。')
    if mode == 'matrix' and not deck:
        raise ValueError('矩阵分析需要我方卡组。')

    # 玩家页沿用 build-detail 的事实集；矩阵格直接复用 statistics_matrix 的
    # 已筛候选场次，避免为 Side 再扫一遍全库/再做一轮排名核验。
    sample_set = None
    if mode == 'matrix':
        cached_samples = None
        if cell_token:
            from .statistics_matrix_cell import cached_cell_samples
            cached_samples = cached_cell_samples(cell_token)
        if cached_samples is not None:
            # 复用同一格刚算出的样本集合；不再扫全库/重复排名核验。
            samples = cached_samples
            roster, data_quality, classification_revision = {}, {}, None
        else:
            from .statistics_matrix import query as matrix_query
            matrix = matrix_query(
                db, replay_root, now, rows=(deck,),
                columns=(opponent_deck,) if opponent_deck else (),
                first_player=first, internal_only=internal_only)
            samples = [sample for cell in matrix['cells'] for sample in cell['samples']]
            roster = matrix['roster']
            data_quality = matrix.get('data_quality') or {}
            classification_revision = matrix.get('classification_revision')
        sample_set = {(sample['player'], sample['digest']) for sample in samples}
        sample_digests = _all_session_digests(
            db, {sample['digest'] for sample in samples})
        views = _rows_with_decks(db, sample_digests)
    else:
        basis = eligible_views(db, replay_root, now, internal_only,
                               check_files=False, player=player)
        views = basis['views']
        roster = basis['roster']
        data_quality = basis.get('diagnostics') or {}
        classification_revision = basis.get('classification_revision')
        # 构筑页的 Side 必须和页面展示的“当前构筑”同一范围：复用
        # build_detail 的 scope='build' 场次集合（主卡相同的整场，含换边局），
        # 不能把该玩家最近 14 天所有卡组的换边混在一起。
        if digest and scope == 'build':
            detail = build_detail_query(db, replay_root, now, player, digest, 'build')
            allowed = {match['digest'] for match in detail['matches']} if detail else set()
            views = [row for row in views if row['digest'] in allowed]
    result = dict(
        version=VERSION, mode=mode, generated_at=now,
        filters=dict(player=player, deck=deck, opponent_deck=opponent_deck,
                     first='all' if first is None else ('first' if first else 'second'),
                     internal_only=bool(internal_only)),
        semantics=SEMANTICS,
        roster={key: value for key, value in roster.items() if key != 'members'},
        classification_revision=classification_revision,
        data_quality=data_quality,
        sides=[], notes=[])

    if not views:
        if mode == 'player':
            result['sides'] = [_empty_side('player', 'player', player)]
        elif opponent_deck == deck:
            result['sides'] = [_empty_side('mirror', 'mirror', deck)]
        else:
            result['sides'] = [_empty_side('my', 'row', deck)]
            if opponent_deck:
                result['sides'].append(_empty_side('opponent', 'column', opponent_deck))
        result['notes'].append('当前范围内没有有效视角；没有数据不等于没有换牌。')
        return result

    # 先按卡组方向收窄候选视角，再只为这些场次读取身份/双方标签。
    specs = []
    if mode == 'player':
        specs.append(dict(mode='player', key='player', role='player', player=player,
                          deck=None, opponent_deck=opponent_deck, mirror=False,
                          candidate_rows=views))
    else:
        pairs = _pairs(db, sample_digests)
        my_rows = [row for row in views if (row['player'], row['digest']) in sample_set]
        specs.append(dict(mode='matrix', key='my', role='row', player=None,
                          deck=deck, opponent_deck=opponent_deck or None, mirror=False,
                          candidate_rows=my_rows))
        if opponent_deck and opponent_deck != deck:
            opponent_set = set()
            for sample in samples:
                pair = pairs.get(sample['digest'], ())
                other = next((name for name in pair if name != sample['player']), None)
                if other:
                    opponent_set.add((other, sample['digest']))
            specs.append(dict(mode='matrix', key='opponent', role='column', player=None,
                              deck=opponent_deck, opponent_deck=deck, mirror=False,
                              candidate_rows=[row for row in views
                                              if (row['player'], row['digest']) in opponent_set]))
        if opponent_deck == deck:
            specs = [dict(mode='matrix', key='mirror', role='mirror', player=None,
                          deck=deck, opponent_deck=deck, mirror=True,
                          candidate_rows=my_rows)]

    candidate_rows = [row for spec in specs for row in spec['candidate_rows']]
    candidate_digests = {row['digest'] for row in candidate_rows}
    pairs = _pairs(db, candidate_digests)
    raw_rows = _rows_with_decks(db, candidate_digests)
    raw_label = {(row['digest'], row['player']): row.get('deck_type') for row in raw_rows}
    label_rows = views if mode == 'matrix' else raw_rows
    eligible = None
    if mode == 'matrix':
        # 矩阵候选已由 statistics_matrix 完成双方资格/来源筛选；
        # 同场其它局只用于换边比较。
        eligible = {(row['digest'], row['player']) for row in views}
        label_rows = [row for row in label_rows if (row['digest'], row['player']) in eligible]
    all_digests = candidate_digests | {row['digest'] for row in label_rows}
    pairs = _pairs(db, all_digests)
    metadata = identity_metadata(db, all_digests)
    groups, _ = _group_by_session(label_rows, metadata, pairs)
    if mode == 'matrix':
        groups = {key: rows for key, rows in groups.items()
                  if len({row['player'] for row in rows}) == 2}
        # 与 statistics_matrix 的格子一致：行视角卡组 + 对手当场卡组，
        # 用于决定“这场是否属于本格”；不要求每一局都再匹配一次。
        for spec in specs:
            matched = set()
            for row in spec['candidate_rows']:
                pair = pairs.get(row['digest'], ())
                if len(pair) != 2 or not all((row['digest'], name) in eligible for name in pair):
                    continue
                opponent = next((name for name in pair if name != row['player']), None)
                if spec['opponent_deck'] and raw_label.get((row['digest'], opponent)) != spec['opponent_deck']:
                    continue
                matched.add((row['player'], row['digest']))
            spec['matched'] = matched
    grouped_digests = {row['digest'] for rows in groups.values() for row in rows}
    def _direction_in_scope(row):
        if mode == 'player':
            return True
        pair = pairs.get(row['digest'], ())
        return len(pair) == 2 and all((row['digest'], name) in eligible for name in pair)
    for spec in specs:
        if mode == 'matrix':
            spec['unassociated_views'] = sum(
                1 for row in spec['candidate_rows']
                if row['digest'] not in grouped_digests
                and (row['player'], row['digest']) in spec.get('matched', ()))
        else:
            spec['unassociated_views'] = sum(
                1 for row in spec['candidate_rows']
                if row['digest'] not in grouped_digests and _direction_in_scope(row))
    unassociated_total = sum(spec['unassociated_views'] for spec in specs)

    # 同一场对手标签（给 infer_side 的 tag 权重使用；不改算法本身）。
    opponent_label = {}
    for session_rows in groups.values():
        labels = _session_labels(session_rows, metadata)
        for row in session_rows:
            other = next((name for name in pairs.get(row['digest'], ())
                          if name != row['player']), None)
            opponent_label[row['digest']] = labels.get(other)

    evidence_builds = {row['build'] for rows in groups.values() for row in rows}
    cards = _cards_by_build(db, evidence_builds)
    mapping, _, _ = identity_reference(db)

    for spec in specs:
        side, target_info, observed_codes = _side_payload(
            db, spec['key'], spec['role'],
            spec.get('deck') if spec['mode'] == 'matrix' else None,
            spec, groups, metadata, cards, mapping, spec['unassociated_views'], first)
        side['filters'] = dict(deck=spec.get('deck'), opponent_deck=spec.get('opponent_deck'),
                               first='all' if first is None else ('first' if first else 'second'))
        if algorithm:
            selected = sorted(target_info['matched'],
                              key=lambda name: (-target_info['valid'].get(name, 0), name))
            rows_by_player = {name: [row for row in views if row['player'] == name]
                              for name in selected}
            # 既有算法要看到该玩家的全部有效场次，不能只看本对阵；这里补齐身份。
            missing_digests = {row['digest'] for rows in rows_by_player.values() for row in rows}
            missing_digests -= set(metadata)
            if missing_digests:
                metadata.update(identity_metadata(db, missing_digests))
                pairs_missing = missing_digests - set(pairs)
                if pairs_missing:
                    pairs.update(_pairs(db, pairs_missing))
                extra_groups, _ = _group_by_session(_rows_with_decks(db, missing_digests),
                                                    metadata, pairs)
                for session_rows in extra_groups.values():
                    labels = _session_labels(session_rows, metadata)
                    for row in session_rows:
                        other = next((name for name in pairs.get(row['digest'], ())
                                      if name != row['player']), None)
                        opponent_label[row['digest']] = labels.get(other)
            algorithm_builds = {row['build'] for rows in rows_by_player.values() for row in rows}
            extra_cards = _cards_by_build(db, algorithm_builds - set(cards))
            algorithm_cards = dict(cards)
            algorithm_cards.update(extra_cards)
            for rows in rows_by_player.values():
                for row in rows:
                    row['_pair'] = pairs.get(row['digest'], ())
                    row['_opponent_deck_type'] = opponent_label.get(row['digest'])
            side['algorithm'] = _collect_algorithm(
                rows_by_player, target_info, metadata, algorithm_cards, mapping,
                observed_codes)
        else:
            side['algorithm'] = dict(
                available=False,
                reason='matrix_observed_only' if mode == 'matrix' else 'disabled',
                players_total=0, players_analyzed=0, players_skipped=0, cards=[],
                latent_cards=[], observed_only=[], reason_counts={},
                anchors=[], selection_basis={},
                notes=['矩阵首版只展示本格明确换入/换出；不汇总玩家历史 infer_side 候选。'
                       if mode == 'matrix' else '本入口未计算算法推断。'])
        result['sides'].append(side)

    if unassociated_total:
        result['notes'].append('另有 %d 个方向匹配的有效视角没有唯一 session/双方关联，'
                               '未计入任何场次；不按时间补猜，也不当作未换牌。'
                               % unassociated_total)
    if not any(side['sample']['sessions_matched'] for side in result['sides']):
        result['notes'].append('没有匹配该玩家/卡组方向的场次；这是空样本，'
                               '不是“没有换 Side”。')
    return result
