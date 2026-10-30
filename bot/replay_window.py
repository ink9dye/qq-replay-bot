"""录像可分享的滚动时间范围，与物理清理共用保留策略。"""

from retention_policy import RETENTION_SECONDS

REPLAY_WINDOW_SECONDS = RETENTION_SECONDS


def replay_in_window(replay: dict, now: float) -> bool:
    stamp = replay.get('saved_at')
    if stamp is None:
        stamp = replay['recorded_at']
    return float(now) - REPLAY_WINDOW_SECONDS < float(stamp) <= float(now)
