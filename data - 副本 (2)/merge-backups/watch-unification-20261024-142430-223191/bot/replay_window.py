"""录像可分享的滚动时间范围，不删除本地文件。"""

REPLAY_WINDOW_SECONDS = 7 * 24 * 3600


def replay_in_window(replay: dict, now: float) -> bool:
    stamp = replay.get('saved_at')
    if stamp is None:
        stamp = replay['recorded_at']
    return float(now) - REPLAY_WINDOW_SECONDS < float(stamp) <= float(now)
