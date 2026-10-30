"""公开竞技历史匹配及标准BO3约束推断；积分变化只做辅助展示。"""
import itertools
import time

from .player_history import PlayerHistory, moment


def matching_history(names, recorded_at, rows):
    found = {}
    for row in rows:
        try:
            if row.get('type') != 'athletic' or {row['usernamea'], row['usernameb']} != set(names):
                continue
            start, end = moment(row['start_time']).timestamp(), moment(row['end_time']).timestamp()
            if not start <= recorded_at <= end:
                continue
            key = (row['usernamea'], row['usernameb'], start, end)
            found[key] = row
        except (ValueError, KeyError, TypeError):
            continue
    return list(found.values())


def infer_bo3(winner, scores, target, count, known=None):
    """必须是已确认完整的小局序列；枚举满足比分及先到两胜的所有可能。"""
    if winner not in (0, 1) or count not in (2, 3) or target not in range(count):
        return None
    if sorted(scores) not in ([0, 2], [1, 2]) or sum(scores) != count or scores[winner] != 2:
        return None
    possible = []
    for seq in itertools.product((0, 1), repeat=count):
        if [seq.count(0), seq.count(1)] != list(scores) or seq[-1] != winner:
            continue
        if any(seq[:i].count(0) >= 2 or seq[:i].count(1) >= 2 for i in range(1, count)):
            continue
        if any(seq[i] != value for i, value in (known or {}).items()):
            continue
        possible.append(seq[target])
    return possible[0] if possible and len(set(possible)) == 1 else None


def supplement_history(report, context=None, fetch=None):
    names = [p['name'] for p in report['players']]
    if len(names) != 2 or not report.get('recorded_at'):
        return
    fetch = fetch or PlayerHistory._fetch
    rows = []
    try:
        # 翻到目标时间为止；受页数和总时限限制，查不到时不猜。
        deadline = time.monotonic() + 15
        for page in range(1, 11):
            remaining = deadline - time.monotonic()
            if remaining <= 0: break
            payload = fetch('history', {'username': names[0], 'type': 1, 'page': page, 'page_num': 100}, min(5, remaining))
            batch = payload.get('data')
            if not isinstance(batch, list): raise ValueError('历史格式异常')
            rows.extend(batch)
            if not batch or any(moment(row['start_time']).timestamp() <= report['recorded_at'] for row in batch):
                break
        candidates = matching_history(names, report['recorded_at'], rows)
        if len(candidates) != 1:
            report['history_status'] = '未找到唯一对应的公开对战记录'
            return
        row = candidates[0]
        report['match_evidence'] = {key: row.get(key) for key in
            ('type', 'usernamea', 'usernameb', 'start_time', 'end_time', 'winner',
             'userscorea', 'userscoreb', 'pta_ex', 'pta', 'ptb_ex', 'ptb')}
        report['history_status'] = '已匹配对手和对战时间；公开记录为整场结果'
        winner_name = row.get('winner')
        if winner_name not in names:
            return
        match_winner = names.index(winner_name)
        scores = [row.get('userscorea'), row.get('userscoreb')]
        if not all(type(s) is int and 0 <= s <= 2 for s in scores):
            return
        if row['usernamea'] != names[0]: scores.reverse()
        inferred, rule = None, None
        if scores[match_winner] == 2 and scores[1 - match_winner] == 0:
            inferred, rule = match_winner, 'BO3整场2:0，两小局赢家相同'
        elif context and context.get('confirmed'):
            games = context.get('games', [])
            start, end = moment(row['start_time']).timestamp(), moment(row['end_time']).timestamp()
            valid = (len(games) == 3 and sorted(g['game_no'] for g in games) == [1, 2, 3]
                and len({g['digest'] for g in games}) == 3
                and all(start <= g['recorded_at'] <= end for g in games)
                and all(set(g['players']) == set(names) for g in games))
            if valid:
                target = next((g['game_no'] - 1 for g in games if g['digest'] == report['digest']), None)
                if target is not None:
                    inferred = infer_bo3(match_winner, scores, target, 3)
                    rule = '完整BO3共三小局，第三小局赢家等于整场赢家'
        if inferred is None:
            return
        if report.get('winner') is not None:
            if report['winner'] != inferred:
                report['warnings'].append('公开整场记录推断与直接小局结果不符，保留直接结果供核对。')
            return
        report.update(winner=inferred, result='win', result_source='history_inferred', inference_rule=rule)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report['history_status'] = '公开对战记录暂不可用，未用积分方向猜测小局输赢'
