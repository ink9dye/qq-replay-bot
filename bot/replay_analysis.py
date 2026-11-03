"""单小局只读重放；所有未知结果显式保留，绝不从响应耗尽猜胜负。"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import struct
import time
from pathlib import Path

from . import replay_native as N

VERSION = 4
ROOT = Path(__file__).resolve().parents[1]
USE_LABELS = {60: '通常召唤尝试', 62: '特殊召唤尝试', 64: '反转召唤尝试', 70: '发动卡片或效果'}


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sidecar(path, kind='analysis'):
    return Path(str(path) + '.' + kind + '.json')


def write_json(path, value):
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Events:
    def __init__(self, start_hand=None):
        self.first = None
        self.turn = 0
        self.winner = None
        self.win_reason = None
        self.items = []
        self.pending_summons = []
        self.sequence = 0
        self.draws = []
        self.unknown_draws = [0, 0]
        self.initial_remaining = [start_hand, start_hand] if start_hand is not None else None

    def observe(self, message):
        self.sequence += 1
        body = message.body
        if message.type == N.MSG_DRAW:
            if len(body) < 2 or body[0] not in (0, 1) or len(body) != 2 + body[1] * 4:
                raise N.ReplayError('抽卡消息长度或玩家编号异常')
            actor, count = body[:2]
            for i in range(count):
                initial = None if self.initial_remaining is None else (
                    self.turn == 0 and self.initial_remaining[actor] > 0)
                if initial:
                    self.initial_remaining[actor] -= 1
                code = struct.unpack_from('<I', body, 2 + i * 4)[0] & 0x7fffffff
                if not code:
                    self.unknown_draws[actor] += 1
                    continue
                self.draws.append(dict(player=actor, card=code, sequence=self.sequence,
                                       turn=self.turn, initial=initial))
        elif message.type == N.MSG_NEW_TURN:
            if body[0] not in (0, 1):
                raise N.ReplayError('回合玩家编号异常')
            self.turn += 1
            if self.first is None:
                self.first = body[0]
        elif message.type == N.MSG_WIN:
            if body[0] not in (0, 1, 2):
                raise N.ReplayError('胜者编号异常')
            self.winner, self.win_reason = body[:2]
        elif message.type in USE_LABELS:
            code = struct.unpack_from('<I', body)[0] & 0x7fffffff
            # CHAINING 的第8字节是发动方；第4字节是卡片当前控制方，二者可能不同。
            actor = body[8] if message.type == N.MSG_CHAINING else body[4]
            if actor not in (0, 1) or not code:
                raise N.ReplayError('用卡事件归属或卡号异常')
            self.items.append({'index': len(self.items) + 1, 'turn': self.turn,
                               'sequence': self.sequence,
                               'player': actor, 'card': code, 'message': message.type,
                               'kind': USE_LABELS[message.type], 'raw': body.hex()})
            if message.type in (60, 62, 64):
                event = self.items[-1]
                event.update(summon_success=False, location=body[5], sequence=body[6])
                self.pending_summons.append(event)
        elif message.type in (61, 63, 65):
            attempt = {61: 60, 63: 62, 65: 64}[message.type]
            for event in self.pending_summons:
                if event['message'] == attempt: event['summon_success'] = True
            self.pending_summons = [e for e in self.pending_summons if e['message'] != attempt]
        elif message.type == N.MSG_MOVE:
            # 召唤无效后离场的卡片不能被后续另一张卡的召唤成功消息算成出场。
            old = tuple(body[4:7])
            new = tuple(body[8:11])
            if old != new:
                self.pending_summons = [e for e in self.pending_summons
                    if (e['player'], e['location'], e['sequence']) != old]


def fill_usage(report, events, cards=None):
    names = report.setdefault('card_names', {})
    types = report.setdefault('card_types', {})
    for event in events.items:
        code = event['card']
        if cards is not None:
            names[str(code)] = cards.name(code)
            row = cards.rows.get(code)
            if row: types[str(code)] = row.type
        event['card_name'] = names.get(str(code), str(code))
    for player in report['players']:
        actor = player['index']
        own = [e for e in events.items if e['player'] == actor]
        codes = sorted({e['card'] for e in own})
        extra_codes = set(player['extra'])
        extra_codes.update(code for code in codes if types.get(str(code), 0) & N.TYPES_EXTRA)
        player['used_cards'] = [{'code': code, 'name': names.get(str(code), str(code)),
            'category': 'extra' if code in extra_codes else 'main_side' if code in player['main'] else 'other',
            'in_deck': code in player['main'] + player['extra'],
            'events': sum(e['card'] == code for e in own)} for code in codes]
        player['extra_appearances'] = []
        for code in sorted(extra_codes):
            count = sum(e['card'] == code and e.get('summon_success', False) for e in own)
            player['extra_appearances'].append({'code': code, 'name': names.get(str(code), str(code)),
                'carried': code in player['extra'], 'appeared': bool(count), 'appearance_count': count})


def fill_draws(report, events, complete=False, source='native_replay'):
    """仅MSG_DRAW算抽到；检索或回收的MOVE不会形成抽卡事实。"""
    names = report.get('card_names', {})
    types = report.get('card_types', {})
    for player in report['players']:
        actor = player['index']
        known = [d for d in events.draws if d['player'] == actor]
        initial_accounted = events.initial_remaining is not None and events.initial_remaining[actor] == 0
        trusted = complete and initial_accounted and events.unknown_draws[actor] == 0
        # 隐藏手牌的观战日志不能覆盖完整重放，或抹掉已确认的部分抽卡。
        if player.get('draw_record_complete') or (not trusted and player.get('drawn_cards')):
            continue
        cards = []
        for code in sorted({d['card'] for d in known}):
            draws = [d for d in known if d['card'] == code]
            first = min(d['sequence'] for d in draws)
            category = ('extra' if code in player['extra'] or types.get(str(code), 0) & N.TYPES_EXTRA
                        else 'main_side' if code in player['main'] else 'other')
            cards.append(dict(code=code, name=names.get(str(code), str(code)), category=category,
                draw_count=len(draws), initial_count=(sum(d['initial'] is True for d in draws)
                    if events.initial_remaining is not None else None), first_draw_sequence=first,
                used_after_draw=any(u['player'] == actor and u['card'] == code and
                    u['sequence'] > first for u in events.items)))
        player.update(drawn_cards=cards, draw_source=source, draw_record_complete=trusted,
                      initial_draws_accounted=initial_accounted, unknown_draw_count=events.unknown_draws[actor])


def analyze(path, game_root, dll=None, timeout=25):
    path, game_root = Path(path), Path(game_root)
    dll = Path(dll) if dll else ROOT / ('runtime/ocgcore.dll' if os.name == 'nt' else 'runtime/ocgcore.so')
    report = {'version': VERSION, 'digest': digest_file(path), 'file': path.name,
              'analyzed_at': time.time(), 'playback': 'failed', 'result': 'unknown',
              'winner': None, 'result_source': None, 'first_player': None,
              'players': [], 'events': [], 'warnings': [], 'responses_consumed': 0}
    duel = None
    try:
        replay = N.parse_replay(path)
        report['start_hand'] = replay.start_hand
        cards = N.CardDB(game_root)
        report['card_names'] = {str(code): cards.name(code) for deck in replay.main + replay.extra for code in deck}
        report['card_types'] = {str(code): cards.rows[code].type for deck in replay.main + replay.extra for code in deck if code in cards.rows}
        from . import watch_core
        watch_core._CARD_NAMES = dict(cards.names)  # 使用本机卡库，重放不联网更新卡表。
        from .watch_core import deck_title_for
        for i, name in enumerate(replay.names):
            report['players'].append({'index': i, 'name': name,
                'main': list(reversed(replay.main[i])), 'extra': list(reversed(replay.extra[i])),
                'deck_type': deck_title_for(replay.main[i], replay.extra[i], []), 'used_cards': []})
        report['responses_total'] = len(replay.responses)
        report['recorded_at'] = struct.unpack_from('<I', path.read_bytes(), 20)[0]
        report['replay_version'] = hex(struct.unpack_from('<I', path.read_bytes(), 4)[0])
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise N.ReplayError('录像解析需要64位Python')
        if report['replay_version'] != '0x1362':
            raise N.ReplayError('当前验证的内核仅支持0x1362录像')
        core = N.Core(dll, cards, game_root)
        report['engine_sha256'] = digest_file(dll)
        report['database_sha256'] = {p.name: digest_file(p) for p in
            [game_root / 'cards.cdb'] + sorted((game_root / 'expansions').glob('*.cdb')) if p.is_file()}
        if replay.seeds:
            duel = core.lib.create_duel_v2((ctypes.c_uint32 * 8)(*replay.seeds))
        else:
            # 客户端的YRP1使用std::mt19937(seed)的首个输出作为内核种子。
            duel = core.lib.create_duel(mt19937_first(replay.seed))
        if not duel:
            raise N.ReplayError('无法创建录像重放')
        for p in range(2):
            core.lib.set_player_info(duel, p, replay.start_lp, replay.start_hand, replay.draw_count)
            for code in replay.main[p]: core.lib.new_card(duel, code, p, p, 1, 0, 8)
            for code in replay.extra[p]: core.lib.new_card(duel, code, p, p, 64, 0, 8)
        core.lib.start_duel(duel, replay.duel_flag)
        events, consumed = Events(start_hand=replay.start_hand), 0
        deadline = time.monotonic() + timeout
        for step in range(100000):
            if time.monotonic() > deadline:
                raise N.ReplayError('重放超时')
            messages, flag = core.messages(duel)
            if core.errors:
                raise N.ReplayError('脚本错误：' + '；'.join(core.errors[:3]))
            # 普通怪兽没有独立脚本是合法情况；效果卡缺脚本则不接受本次重放。
            missing = [s for s in core.missing_scripts if not s.rsplit('/', 1)[-1].startswith('c')
                or not s.rsplit('/', 1)[-1][1:-4].isdigit()
                or (cards.rows.get(int(s.rsplit('/', 1)[-1][1:-4])) is not None
                    and cards.rows[int(s.rsplit('/', 1)[-1][1:-4])].type & (0x20 | 6))]
            if missing:
                raise N.ReplayError('缺少必要脚本：' + '、'.join(missing[:3]))
            held = None
            for msg in messages:
                if msg.type == N.MSG_RETRY:
                    raise N.ReplayError(f'第{consumed}次操作被内核拒绝，可能版本不兼容')
                events.observe(msg)
                if msg.type in N.RESPONSE_MSGS: held = msg
            if events.winner is not None:
                if consumed != len(replay.responses):
                    raise N.ReplayError('胜负消息后仍有未消费操作，不能确认重放')
                report['playback'] = 'completed'
                break
            if flag == N.PROCESSOR_END:
                raise N.ReplayError('内核提前结束且没有胜负消息')
            if flag == N.PROCESSOR_WAITING and held:
                if consumed == len(replay.responses):
                    report['playback'] = 'responses_exhausted'
                    report['warnings'].append('已重放全部记录操作；录像没有保存结束原因，单靠录像无法确认胜负。')
                    break
                core.respond(duel, replay.responses[consumed])
                consumed += 1
                report['responses_consumed'] = consumed
        else:
            raise N.ReplayError('重放超过步骤上限')
        report.update(first_player=events.first, events=events.items,
                      script_sha256=core.script_hashes, turns=events.turn)
        if events.winner is not None:
            report.update(winner=events.winner, result='draw' if events.winner == 2 else 'win',
                          result_source='native_replay', win_reason=events.win_reason)
        fill_usage(report, events, cards)
    except (OSError, ValueError, N.ReplayError, struct.error) as exc:
        report['playback'] = 'failed'
        report['warnings'].append(str(exc))
    finally:
        if duel: core.lib.end_duel(duel)
    if report['players'] and 'events' in locals():
        fill_draws(report, events, complete=report['playback'] in ('completed', 'responses_exhausted'))
    # 直接观战证据不依赖本机卡片脚本版本，即使旧录像重放失败也可保留胜负。
    if report['players']:
        apply_live_evidence(report, path, cards=locals().get('cards'))
    return report


def apply_live_evidence(report, path, cards=None):
    if report.get('result_conflict'):
        return
    try:
        evidence = json.loads(sidecar(path, 'evidence').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    winner = evidence.get('winner')
    if (evidence.get('digest') != report['digest'] or type(winner) is not int or winner not in (0, 1, 2)
            or evidence.get('players') != [p['name'] for p in report['players']]):
        report['warnings'].append('观战结果与录像未能一致关联，未采用。')
        return
    if report['winner'] is not None and winner != report['winner']:
        report.update(result='unknown', winner=None, result_source=None, result_conflict=True)
        report['warnings'].append('观战与重放胜负冲突，需要核对。')
        return
    report.update(result='draw' if winner == 2 else 'win', winner=winner,
                  result_source='live_server', evidence=evidence)
    previous = evidence.get('previous_game')
    if isinstance(previous, dict) and previous.get('loser') in evidence['players']:
        report['initiative_chooser'] = previous['loser']
        report['initiative_chooser_source'] = '上一小局败方选择先后手（标准BO3规则）'
    if evidence.get('captured_from_start') and isinstance(evidence.get('events'), list):
        events = Events(start_hand=report.get('start_hand'))
        try:
            if len(evidence['events']) > 100000: raise ValueError('事件数量超限')
            lengths = {5: 3, 40: 2, 50: 17, 60: 9, 61: 1, 62: 9, 63: 1, 64: 9, 65: 1, 70: 17}
            for raw in evidence['events']:
                msg = bytes.fromhex(raw)
                draw = bool(msg and msg[0] == N.MSG_DRAW and len(msg) >= 3 and
                            msg[1] in (0, 1) and len(msg) == 3 + 4 * msg[2])
                if not msg or (not draw and len(msg) != lengths.get(msg[0])):
                    raise ValueError('消息长度异常')
                events.observe(N.Message(msg[0], msg[1:]))
            if events.winner != winner or events.first is None: raise ValueError('观战消息不完整')
            fill_usage(report, events, cards)
            if evidence.get('draw_capture_version') == 1:
                fill_draws(report, events, complete=True, source='live_server')
            report.update(first_player=events.first, events=events.items, turns=events.turn,
                          playback='live_complete', usage_source='live_server')
        except (ValueError, N.ReplayError, TypeError):
            report['warnings'].append('观战事件不完整，用卡过程仍以成功的录像重放为准。')


def mt19937_first(seed):
    state = [seed & 0xffffffff]
    for i in range(1, 624):
        state.append((1812433253 * (state[-1] ^ (state[-1] >> 30)) + i) & 0xffffffff)
    value = state[397] ^ (((state[0] & 0x80000000) | (state[1] & 0x7fffffff)) >> 1)
    if state[1] & 1: value ^= 0x9908b0df
    value ^= value >> 11
    value ^= (value << 7) & 0x9d2c5680
    value ^= (value << 15) & 0xefc60000
    return (value ^ (value >> 18)) & 0xffffffff


def main():
    parser = argparse.ArgumentParser(description='解析一个小局的卡组、先后手、用卡和胜负证据')
    parser.add_argument('replay', type=Path)
    parser.add_argument('--game-root', type=Path, default=Path(r'D:\MyCardLibrary\ygopro'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--history', action='store_true', help='同时匹配萌卡公开对战记录')
    parser.add_argument('--context', type=Path, help='已确认的整场小局关联信息')
    args = parser.parse_args()
    report = analyze(args.replay, args.game_root)
    if args.history and report['players']:
        from .replay_result import supplement_history
        context = json.loads(args.context.read_text(encoding='utf-8')) if args.context else None
        supplement_history(report, context)
    if args.output: write_json(args.output, report)
    else: print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__': main()
