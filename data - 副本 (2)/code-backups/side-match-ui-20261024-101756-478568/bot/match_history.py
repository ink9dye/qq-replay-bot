"""以真实观战场次分组，复用 watch 的换卡推断，并按主卡构筑补充历史 side。"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

from .deck_links import observed_deck
from .watch_core import sided_decks, safe_filename_part


def build_match_groups(store, available: list, player: str) -> list[dict]:
    mapping = store.session_map(player)
    groups = {}
    for replay, path in available:
        known = mapping.get(replay['id'])
        key = known['session_id'] if known else f'ungrouped:{replay["id"]}'
        group = groups.setdefault(key, {'id': key, 'entries': [], 'games': [], 'confirmed': bool(known)})
        number = known['game_no'] if known else 1
        group['entries'].append((number, replay, path))
        deck = observed_deck(path, player)
        if deck:
            main, extra = deck
            group['games'].append((number, [(player, main, extra)]))
    result = []
    for group in groups.values():
        group['entries'].sort(key=lambda entry: (entry[0], entry[1]['recorded_at'], entry[1]['id']))
        group['deck'] = sided_decks(group['games']).get(player)
        group['stamp'] = max(replay['saved_at'] for _, replay, _ in group['entries'])
        group['latest'] = max((replay for _, replay, _ in group['entries']),
                              key=lambda replay: (replay['saved_at'], replay['recorded_at'], replay['id']))
        result.append(group)
    return sorted(result, key=lambda group: (group['stamp'], group['latest']['recorded_at'],
                                           group['latest']['id']), reverse=True)


def supplemented_deck(groups: list[dict]):
    for index, group in enumerate(groups):
        if group['deck'] is None:
            continue
        main, extra, side, base_no, game_count = group['deck']
        counts = Counter(side)
        contributors = 0
        for previous in groups[index + 1:]:
            if previous['deck'] is None:
                continue
            old_main, _, old_side, _, _ = previous['deck']
            if Counter(old_main) != Counter(main):
                continue
            expanded = counts | Counter(old_side)  # 相同卡取已观测的最大张数，不跨场累加。
            if expanded != counts:
                contributors += 1
                counts = expanded
        return {'group': group, 'main': main, 'extra': extra, 'side': list(counts.elements()),
                'base_no': base_no, 'game_count': game_count, 'history_count': contributors}
    return None


def read_ydk(path: Path):
    if path.stat().st_size > 128 * 1024:
        return None
    sections = {'#main': [], '#extra': [], '!side': []}
    current = None
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line in sections:
            current = sections[line]
        elif current is not None and line.isdigit():
            current.append(int(line))
    return sections['#main'], sections['#extra'], sections['!side']


def recover_legacy_sessions(store, replay_root: Path, deck_root: Path) -> int:
    """仅在 watch 的既有YDK与唯一录像组的主卡、额外、side全部一致时回填旧场次。"""
    if not deck_root.is_dir():
        return 0
    from .share import safe_replay_file
    players = {sub['player'] for sub in store.list_subscriptions()}
    pending = {}
    for player in players:
        mapped = store.session_map(player)
        rows = []
        for replay in store.replays_for_player(player):
            if replay['id'] in mapped:
                continue
            path = safe_replay_file(replay_root, replay['rel_path'])
            deck = observed_deck(path, player) if path else None
            if deck:
                rows.append((replay, path, deck))
        pending[player] = rows
    recovered = 0
    for ydk in deck_root.glob('*.ydk'):
        try:
            cards = read_ydk(ydk)
            if not cards or not cards[0]:
                continue
            modified = ydk.stat().st_mtime
            candidates = {}
            for player, records in pending.items():
                if not re.match(r'^\d{2}-\d{2} ' + re.escape(safe_filename_part(player)) + r'(?: |\.ydk$)', ydk.name):
                    continue
                for anchor, _, _ in records:
                    if not 0 <= modified - anchor['saved_at'] <= 120:
                        continue
                    opponents = set(anchor['players'])
                    batch = [(row, path, deck) for row, path, deck in records
                             if abs(row['saved_at'] - anchor['saved_at']) <= 2
                             and set(row['players']) == opponents]
                    batch.sort(key=lambda entry: (entry[0]['recorded_at'], entry[0]['id']))
                    games = [(number, [(player, deck[0], deck[1])])
                             for number, (_, _, deck) in enumerate(batch, 1)]
                    inferred = sided_decks(games).get(player)
                    if inferred and all(Counter(found) == Counter(expected)
                                        for found, expected in zip(inferred[:3], cards)):
                        identifiers = tuple(row['id'] for row, _, _ in batch)
                        candidates[(player, identifiers)] = batch
            if len(candidates) != 1:
                continue
            (player, identifiers), batch = next(iter(candidates.items()))
            session = 'legacy:' + hashlib.sha256((player + ':' + ','.join(map(str, identifiers))).encode()).hexdigest()
            store.recover_session(session, [(row['id'], number)
                                           for number, (row, _, _) in enumerate(batch, 1)])
            mapped = set(identifiers)
            pending[player] = [entry for entry in pending[player] if entry[0]['id'] not in mapped]
            recovered += 1
        except (OSError, ValueError):
            continue
    return recovered
