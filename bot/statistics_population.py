"""The common latest valid build selection; filter only AFTER selecting per person."""


def latest_views(basis):
    """每位玩家取最新一场比赛；该场有主卡局时优先主卡局构筑。

    Side 局可能换过牌，但不能用 Side 构筑代表主卡组；同一 session 的 game_no=1
    才是该场主卡组。没有 session/game_no 的旧数据仍回退到最新视角。
    """
    grouped = {}
    for row in basis['views']:
        player = row['player']
        session = row.get('observed_session_id')
        key = (session, tuple(row.get('observed_match_players') or ())) if session else row['digest']
        grouped.setdefault(player, {}).setdefault(key, []).append(row)
    latest = {}
    for player, matches in grouped.items():
        # 最新一场按该场最晚记录时间选；session 相同才归为同一场。
        group = max(matches.values(),
                    key=lambda rows: max((r['recorded_at'], r.get('received_at', 0), r['digest']) for r in rows))
        main = [row for row in group if row.get('observed_game_no') == 1
                and row.get('observed_captured_from_start') is True]
        latest[player] = max(main or group,
                             key=lambda r: (r['recorded_at'], r.get('received_at', 0), r['digest']))
    return latest
