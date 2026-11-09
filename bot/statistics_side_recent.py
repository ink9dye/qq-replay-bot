"""换备（最近 N 场）：当前构筑对最常遇到的对手卡组，按先/后攻汇总换入换出。

口径（2026-11-09 用户确认）：
- 只统计当前构筑（主卡相同，build_detail scope=build）的场次；
- 对手卡组按该构筑遇到的场次从多到少取前 6；
- 每个对手只取最近 5 场，使用这些场里所有有完整卡表的第 2/3 局；
- 有效换边局少于 3 个时标记 sample_low，不拒绝展示；
- 先攻/后攻按**该换边局（第 2/3 局）该玩家自己的座位**（first_player=1/0）分列：
  换入/换出要在“用这副主卡的这一局”里生效，BO3 中第 2/3 局的座位常与首局相反，
  不能拿首局座位代替。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Callable

MAX_TOP_DECKS = 6
RECENT_MATCHES = 5
MIN_SIDE_GAMES = 3
MAX_CARDS_PER_DIRECTION = 10
DECKS_PER_PAGE = 3
UNKNOWN_DECK = '未识别'


def _game_label(game: dict) -> str:
    for key in ('opponent_match_deck_type', 'opponent_deck_type'):
        value = game.get(key)
        if value and value not in (UNKNOWN_DECK, '未知', '卡组未识别'):
            return str(value)
    return UNKNOWN_DECK


def _group_label(group: dict) -> str:
    for game in group.get('games') or []:
        label = _game_label(game)
        if label != UNKNOWN_DECK:
            return label
    return UNKNOWN_DECK


def _first_game(group: dict):
    for game in group.get('games') or []:
        if game.get('game_no') == 1:
            return game
    return None


def _side_changes(group: dict) -> list[tuple[dict, dict]]:
    """返回该场可用的 (换边局, changes)；只认完整卡表的 known。"""
    if group.get('sequence_ambiguous'):
        return []
    if _first_game(group) is None:
        return []
    result = []
    for game in group.get('games') or []:
        if game.get('game_no') not in (2, 3):
            continue
        change = game.get('changes') or {}
        if change.get('status') != 'known':
            continue
        result.append((game, change))
    return result


def _seat(first_game: dict):
    value = first_game.get('first_player')
    if value == 1:
        return 'first'
    if value == 0:
        return 'second'
    return None


def _new_bucket() -> dict:
    return dict(side_games=0, added={}, removed={})


def _add_changes(bucket: dict, session_key: str, changes: dict) -> None:
    for direction in ('added', 'removed'):
        for card in changes.get(direction) or []:
            try:
                code = int(card.get('code') or 0)
                copies = int(card.get('copies') or 0)
            except (TypeError, ValueError):
                continue
            if not code or copies <= 0:
                continue
            partition = str(card.get('partition') or 'main')
            key = (partition, code)
            item = bucket[direction].get(key)
            if item is None:
                item = dict(code=code, partition=partition, sessions=set(),
                            side_games=0, copies=Counter())
                bucket[direction][key] = item
            item['sessions'].add(session_key)
            item['side_games'] += 1
            item['copies'][copies] += 1


def _cards(items: dict) -> list[dict]:
    result = []
    for item in items.values():
        copies = item['copies']
        result.append(dict(
            code=item['code'], partition=item['partition'],
            sessions=len(item['sessions']), side_games=item['side_games'],
            copies={str(k): v for k, v in sorted(copies.items())},
            min_copies=min(copies), max_copies=max(copies),
        ))
    result.sort(key=lambda card: (-card['sessions'], -card['max_copies'],
                                  card['partition'], card['code']))
    return result


def _finalize(bucket: dict) -> dict:
    return dict(side_games=bucket['side_games'],
                added=_cards(bucket['added']), removed=_cards(bucket['removed']))


def _combine_session(changes: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一场多局同一张卡取最大张数，不把多局相加。"""
    merged = {'added': {}, 'removed': {}}
    for change in changes:
        for direction in ('added', 'removed'):
            for card in change.get(direction) or []:
                try:
                    code = int(card.get('code') or 0)
                    copies = int(card.get('copies') or 0)
                except (TypeError, ValueError):
                    continue
                if not code or copies <= 0:
                    continue
                partition = str(card.get('partition') or 'main')
                key = (partition, code)
                item = merged[direction].get(key)
                if item is None:
                    item = dict(code=code, partition=partition, copies=0)
                    merged[direction][key] = item
                item['copies'] = max(item['copies'], copies)
    def ordered(items):
        return sorted(items.values(), key=lambda card: (card['partition'], card['code']))
    return ordered(merged['added']), ordered(merged['removed'])


def _session_row(group: dict) -> dict:
    first = _first_game(group)
    changes = _side_changes(group)
    added, removed = _combine_session([change for _, change in changes])
    game_numbers = [game.get('game_no') for game in group.get('games') or []
                    if isinstance(game.get('game_no'), int)]
    return dict(
        id=group.get('id'),
        digest=(first or {}).get('digest'),
        recorded_at=group.get('recorded_at') or (first or {}).get('recorded_at'),
        opponent=(first or {}).get('opponent'),
        first_player=(first or {}).get('first_player'),
        side_seats=[seat for seat in (_seat(game) for game, _ in changes) if seat],
        game_count=max(game_numbers) if game_numbers else None,
        side_games=len(changes),
        status='observed' if changes else 'no_side_evidence',
        added=added, removed=removed,
    )


def build_plan(detail: dict, top: int = MAX_TOP_DECKS,
               recent: int = RECENT_MATCHES,
               min_side_games: int = MIN_SIDE_GAMES) -> dict:
    """从 build_detail.query 的结果生成换备计划（纯函数，便于测试）。"""
    top = max(1, min(int(top), 12))
    recent = max(1, min(int(recent), 10))
    groups = list(detail.get('match_groups') or [])
    by_label = defaultdict(list)
    for group in groups:
        by_label[_group_label(group)].append(group)
    for items in by_label.values():
        items.sort(key=lambda group: group.get('recorded_at') or 0, reverse=True)

    counts = {label: len(items) for label, items in by_label.items()}
    latest = {label: max((group.get('recorded_at') or 0) for group in items)
              for label, items in by_label.items()}
    labels = [label for label in sorted(counts, key=lambda label: (
        -counts[label], -latest[label], label)) if label != UNKNOWN_DECK][:top]

    opponents = []
    for label in labels:
        recent_groups = by_label[label][:recent]
        buckets = {'first': _new_bucket(), 'second': _new_bucket()}
        sessions = []
        seat_unknown = 0
        for group in recent_groups:
            first = _first_game(group)
            changes = _side_changes(group)
            session_key = str(group.get('id') or (first or {}).get('digest') or '')
            # 换入/换出生效于第 2/3 局本身，座位取该局该玩家自己的 first_player；
            # 不能沿用首局座位（BO3 第 2/3 局座位常与首局相反）。
            for game, change in changes:
                seat = _seat(game)
                if not seat:
                    seat_unknown += 1
                    continue
                buckets[seat]['side_games'] += 1
                _add_changes(buckets[seat], session_key, change)
            sessions.append(_session_row(group))
        first_plan = _finalize(buckets['first'])
        second_plan = _finalize(buckets['second'])
        side_games = first_plan['side_games'] + second_plan['side_games']
        opponents.append(dict(
            deck=label, matches=counts[label],
            recent_matches=len(recent_groups), side_games=side_games,
            seat_unknown_games=seat_unknown,
            sample_low=side_games < max(1, int(min_side_games)),
            first=first_plan, second=second_plan, sessions=sessions,
        ))

    notes = ['只统计当前构筑（主卡相同）的场次；每个对手取最近 %d 场。' % recent,
             '先攻/后攻按该换边局（第2/3局）该玩家自己的实际座位；同一场多局同一张卡取最大张数。']
    if any(item['seat_unknown_games'] for item in opponents):
        notes.append('该玩家座位未知（first_player 缺失）的换边局不计入先/后攻，单独计数。')
    if any(item['sample_low'] for item in opponents):
        notes.append('标记“样本少”表示最近几场里完整可比较的换边局少于 %d 个。' %
                     max(1, int(min_side_games)))
    if not groups:
        notes.append('当前构筑最近14天没有可用于 Side 的场次。')
    person = detail.get('person') or {}
    selected = detail.get('selected') or {}
    return dict(
        version=1,
        player=person.get('player'),
        deck=person.get('deck_type'),
        selected=dict(digest=selected.get('digest'),
                      recorded_at=selected.get('recorded_at'),
                      observation_kind=selected.get('observation_kind')),
        generated_at=detail.get('generated_at'),
        top_decks=top, recent_matches=recent,
        min_side_games=max(1, int(min_side_games)),
        total_matches=len(groups),
        opponents=opponents,
        notes=notes,
    )


def query(db, replay_root, now, player, digest=None, top: int = MAX_TOP_DECKS,
          recent: int = RECENT_MATCHES, min_side_games: int = MIN_SIDE_GAMES):
    from .statistics_build_detail import query as build_query
    detail = build_query(db, replay_root, now, player, digest=digest, scope='build')
    if detail is None:
        return None
    return build_plan(detail, top=top, recent=recent, min_side_games=min_side_games)


def _card_text(cards: list[dict], name_for: Callable[[int], str],
               max_cards: int = MAX_CARDS_PER_DIRECTION) -> str:
    parts = []
    for card in (cards or [])[:max_cards]:
        copies = ('×%d' % card['max_copies'] if card['min_copies'] == card['max_copies']
                  else '×%d–%d' % (card['min_copies'], card['max_copies']))
        label = name_for(card['code'])
        if card.get('partition') == 'extra':
            label += '（额外）'
        parts.append(label + copies)
    if len(cards or []) > max_cards:
        parts.append('等%d张' % len(cards))
    return '、'.join(parts) if parts else '无'


def _deck_block(index: int, item: dict, name_for: Callable[[int], str],
                max_cards: int) -> list[str]:
    first, second = item['first'], item['second']
    low = '，样本少' if item.get('sample_low') else ''
    lines = ['%d. %s（%d场｜用最近%d场，%d个Side局%s）' % (
        index, item['deck'], item['matches'], item['recent_matches'],
        item['side_games'], low)]
    if not item['side_games']:
        lines.append('   最近几场没有完整可比较的换边局（缺首局／卡表不完整／2-0 等）。')
        return lines
    for label, plan in (('先攻', first), ('后攻', second)):
        if not plan['side_games']:
            lines.append('   %s：没有有效换边局' % label)
            continue
        lines.append('   %s（%d局）：+%s ／ -%s' % (
            label, plan['side_games'],
            _card_text(plan['added'], name_for, max_cards),
            _card_text(plan['removed'], name_for, max_cards)))
    return lines


def format_text(plan: dict, name_for: Callable[[int], str],
                max_cards: int = MAX_CARDS_PER_DIRECTION,
                decks_per_page: int = DECKS_PER_PAGE) -> list[str]:
    """把换备计划排成 QQ 文字；返回按页拆好的列表。"""
    player = plan.get('player') or '未知玩家'
    deck = plan.get('deck') or '未识别'
    opponents = plan.get('opponents') or []
    header = ['换备 · %s' % player, '当前构筑：%s' % deck,
              '口径：当前构筑最近14天，按对手场次取前%d；每个对手取最近%d场，只用有完整卡表的第2/3局，'
              '先攻/后攻按该局该玩家自己的座位；每方向最多列前%d张，完整明细见网页构筑页。' % (
                  plan.get('top_decks') or MAX_TOP_DECKS,
                  plan.get('recent_matches') or RECENT_MATCHES,
                  max_cards)]
    if not opponents:
        return ['\n'.join(header + ['', '当前构筑最近14天没有足够的对手卡组数据。'])]
    blocks = []
    for index, item in enumerate(opponents, 1):
        blocks.append(_deck_block(index, item, name_for, max_cards))
    per_page = max(1, int(decks_per_page))
    chunks = [blocks[i:i + per_page] for i in range(0, len(blocks), per_page)]
    pages = []
    for number, chunk in enumerate(chunks, 1):
        title = header[:] if len(chunks) == 1 else [
            header[0] + '（%d/%d）' % (number, len(chunks)), header[1], header[2]]
        lines = title + ['']
        for block in chunk:
            lines.extend(block)
        pages.append('\n'.join(lines))
    return pages
