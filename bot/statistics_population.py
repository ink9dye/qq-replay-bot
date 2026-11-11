"""The common latest valid build selection; filter only AFTER selecting per person."""


def latest_views(basis):
    """每位玩家取「最近一场有确证首局」的比赛，代表构筑固定为该场第 1 局。

    没有局次信息、或整场找不到确证首局的比赛**直接忽略**：不拿第 2/3 局的换备
    构筑冒充主卡组，也不按时间猜局次。一场有效比赛都没有的玩家不会出现在结果里。
    """
    grouped = {}
    for row in basis['views']:
        player = row['player']
        session = row.get('observed_session_id')
        key = (session, tuple(row.get('observed_match_players') or ())) if session else row['digest']
        grouped.setdefault(player, {}).setdefault(key, []).append(row)
    latest = {}
    for player, matches in grouped.items():
        usable = []
        for rows in matches.values():
            first = [row for row in rows if row.get('observed_game_no') == 1
                     and row.get('observed_identity_confirmed',
                                 row.get('observed_captured_from_start')) is True]
            if first:
                usable.append((rows, first))
        if not usable:
            continue
        # 先按整场最晚记录时间选最近一场，再取该场首局。
        rows, first = max(
            usable,
            key=lambda item: max((r['recorded_at'], r.get('received_at', 0), r['digest'])
                                 for r in item[0]))
        latest[player] = max(first, key=lambda r: (r['recorded_at'], r.get('received_at', 0), r['digest']))
    return latest
