"""录像、卡组、解析和查询共用的保留期；分享令牌有效期另行管理。"""
from pathlib import Path

RETENTION_DAYS = 14
RETENTION_SECONDS = RETENTION_DAYS * 24 * 3600


def next_cleanup_delay(now, replay_root, deck_root, replays=()):
    """在最近到期时唤醒；最多60秒重新发现新增文件，不延长原始保存时间。

    到期文件删除失败时至少等1秒再试，避免持续占用CPU。
    索引保存时间优先于文件修改时间（重新解析、复制不得续期）。
    """
    indexed = {}
    deadlines = []
    for row in replays:
        stamp = row.get('saved_at')
        if stamp is None:
            stamp = row['recorded_at']
        indexed[str((Path(replay_root) / row['rel_path']).absolute())] = float(stamp)
        deadlines.append(float(stamp) + RETENTION_SECONDS)
    for root, suffix in ((Path(replay_root), '.yrp'), (Path(deck_root), '.ydk')):
        for path in root.rglob('*' + suffix):
            try:
                stamp = indexed.get(str(path.absolute()))
                if stamp is None:
                    stamp = path.stat().st_mtime
                deadlines.append(stamp + RETENTION_SECONDS)
            except OSError:
                continue
    return max(1.0, min([60.0] + [deadline - float(now) for deadline in deadlines]))
