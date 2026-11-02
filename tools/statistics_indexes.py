# -*- coding: utf-8 -*-
"""统计查询覆盖索引维护：只新增索引，不改数据、不改统计口径。

背景（2026-11-02 生产实测，8 局在录）：
  recordings 首查 19–25 秒、card-report 20 秒、cards-extra 17 秒，
  其中主要耗时是反复扫 stat_card_facts（64 万行）与 stat_build_cards（43 万行）后逐行回表。
本工具创建两个覆盖索引，让「按 digest 取事实」「按分区取额外事实」只读索引；
再跑一次 ANALYZE，让查询计划在新索引上稳定。

用法（在服务器上）：
  sudo -u ygo python3 tools/statistics_indexes.py --root /opt/ygobot            # 只报告，不写库
  sudo -u ygo python3 tools/statistics_indexes.py --root /opt/ygobot --apply    # 创建缺失索引
  sudo -u ygo python3 tools/statistics_indexes.py --root /opt/ygobot --apply --analyze

回滚（任一时刻可安全删除，只影响查询速度）：
  sqlite3 data/bot.sqlite3 'DROP INDEX IF EXISTS stat_card_facts_digest;'
  sqlite3 data/bot.sqlite3 'DROP INDEX IF EXISTS stat_card_facts_extra;'
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

INDEXES = (
    (
        "stat_card_facts_digest",
        "CREATE INDEX IF NOT EXISTS stat_card_facts_digest "
        "ON stat_card_facts(digest,player,code,drawn,used)",
        "按对局 digest 读取抽到/使用事实（录像筛选的卡片条件、构筑事实）",
    ),
    (
        "stat_card_facts_extra",
        "CREATE INDEX IF NOT EXISTS stat_card_facts_extra "
        "ON stat_card_facts(partition,code,digest,player,appeared,used)",
        "额外卡统计只扫 partition='extra' 的事实",
    ),
)


def database_path(root, override):
    if override:
        return Path(override)
    config = root / "config.json"
    data_dir = root / "data"
    if config.is_file():
        try:
            payload = json.loads(config.read_text(encoding="utf-8-sig"))
            if payload.get("data_dir"):
                data_dir = Path(payload["data_dir"])
        except (OSError, ValueError):
            pass
    return Path(data_dir) / "bot.sqlite3"


def index_size(db, name):
    try:
        row = db.execute("SELECT SUM(pgsize) FROM dbstat WHERE name=?", (name,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def report_size(value):
    return "—" if value is None else f"{value / 1024 / 1024:.1f} MB"


def main():
    parser = argparse.ArgumentParser(description="统计查询覆盖索引维护（只加索引，不改数据）")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--database", default="")
    parser.add_argument("--apply", action="store_true", help="真正创建缺失索引")
    parser.add_argument("--analyze", action="store_true", help="创建后执行 ANALYZE（写 sqlite_stat1）")
    args = parser.parse_args()

    path = database_path(args.root.resolve(), args.database)
    if not path.is_file():
        raise SystemExit(f"找不到数据库：{path}")
    db = sqlite3.connect(str(path), timeout=60)
    try:
        db.execute("PRAGMA busy_timeout=60000")
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        print(f"数据库：{path}（{path.stat().st_size / 1024 / 1024:.0f} MB）")
        for name, statement, purpose in INDEXES:
            if name in existing:
                print(f"[已存在] {name}  {report_size(index_size(db, name))}  {purpose}")
                continue
            print(f"[缺失]   {name}  {purpose}")
            print(f"         {statement}")
            if not args.apply:
                continue
            started = time.perf_counter()
            db.execute(statement)
            db.commit()
            elapsed = time.perf_counter() - started
            print(f"[完成]   {name}  {elapsed:.1f}s  {report_size(index_size(db, name))}")
        if args.analyze:
            started = time.perf_counter()
            db.execute("ANALYZE")
            db.commit()
            print(f"[analyze] 完成 {time.perf_counter() - started:.1f}s")
        if not args.apply:
            print("\n未加 --apply，未写库。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
