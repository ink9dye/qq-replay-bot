"""每日一卡：按 QQ 号 + 北京日期固定一张卡，跨群聊/私聊、跨重启都不变。

和“随机一卡”共用同一个卡池与抽卡函数，区别只是把结果按 (QQ号, 日期) 落库：
当天第一次抽到什么，之后（包括重启后）再发同一指令都回到同一张。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path


class DailyCards:
    """一张表：user_id + day -> 卡片 JSON；只保留今天和昨天。"""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'daily-cards.sqlite3'

    def get(self, user_id, day: date, choose):
        user = str(user_id)
        if not user.isascii() or not user.isdigit() or int(user) <= 0:
            raise ValueError('daily card requires a QQ user id')
        user = str(int(user))
        stamp = day.isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS daily_cards ('
                       'user_id TEXT NOT NULL, day TEXT NOT NULL, card_json TEXT NOT NULL, '
                       'PRIMARY KEY(user_id, day))')
            row = db.execute('SELECT card_json FROM daily_cards WHERE user_id=? AND day=?',
                             (user, stamp)).fetchone()
            if row:
                return json.loads(row[0])
            # 抽卡要读卡库（首次还要下载），放在拿写锁之前，避免长时间占着数据库。
            candidate = json.dumps(choose(), ensure_ascii=False)
            try:
                db.execute('BEGIN IMMEDIATE')
                # 同一用户并发（理论上被上层锁挡住）时也只认先写入的那张。
                db.execute('INSERT OR IGNORE INTO daily_cards VALUES (?,?,?)',
                           (user, stamp, candidate))
                winner = db.execute('SELECT card_json FROM daily_cards WHERE user_id=? AND day=?',
                                    (user, stamp)).fetchone()[0]
                # 只留今天和昨天：查询过哪天不会变成永久历史。
                db.execute('DELETE FROM daily_cards WHERE day < ?',
                           ((day - timedelta(days=1)).isoformat(),))
                db.commit()
            except Exception:
                db.rollback()
                raise
            return json.loads(winner)
