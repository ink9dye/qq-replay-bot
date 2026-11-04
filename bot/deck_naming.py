# -*- coding: utf-8 -*-
"""前100卡组命名只读查询；命名统一使用 series_naming 字段规则。"""
from __future__ import annotations
import argparse
import json
import sqlite3
import time
from pathlib import Path
TOP_N = 100

def load_config(root: Path) -> dict:
    return json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))

def paths(root: Path) -> tuple[Path, Path]:
    cfg = load_config(root)
    data = Path(cfg.get('data_dir') or root / 'data')
    return data, data / 'bot.sqlite3'

def connect(db_path: Path, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        db = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    else:
        db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    return db

def latest_views(db: sqlite3.Connection, now: float, replay_root: Path | None = None) -> list[dict]:
    """当前前100每人的最新一局有效视角。

    与网页卡组命名/玩家排行共用同一条路径：
    - eligible_views：只算当前名单、未排除、来源文件仍存在的视角；
    - inherit_first_game_types：同场首局继承；
    - 命名由统一字段规则计算，不再叠加人工/相似度标签。
    不再直接读 stat_builds 的最新原始行（会把缺源/被排除的视角算进来）。
    """
    data_dir = Path(db.execute('PRAGMA database_list').fetchone()[2]).parent
    root = Path(replay_root) if replay_root else data_dir / 'replays'
    from .statistics_deck_naming import _latest_views, _roster
    roster = _roster(db)
    views = _latest_views(db, now, root, only=set(roster))
    rows = []
    for name, view in views.items():
        member = roster.get(name)
        if member is None or member['rank'] > TOP_N:
            continue
        rows.append(dict(rank=member['rank'], player=name, build=view['build'],
                         digest=view['digest'], recorded_at=view['recorded_at'],
                         deck_type=view['deck_type'], classification_version=None,
                         classification_source=view.get('classification_source')))
    rows.sort(key=lambda row: row['rank'])
    return rows

def roster(db: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in db.execute(
        'SELECT rank,player FROM stat_current_top100 WHERE rank<=? ORDER BY rank', (TOP_N,))]

def main_cards(db: sqlite3.Connection, build: str, names: dict, limit: int = 6) -> str:
    rows = db.execute("SELECT code,copies FROM stat_build_cards WHERE build=? AND partition='main' "
                      "AND copies>0 ORDER BY copies DESC,code LIMIT ?", (build, limit)).fetchall()
    parts = []
    for row in rows:
        entry = names.get(str(row['code'])) or {}
        label = entry.get('cn_name') or entry.get('name') or str(row['code'])
        parts.append(f"{label}×{row['copies']}")
    return ' / '.join(parts) or '（无主卡组记录）'

def name_index(data: Path) -> dict:
    try:
        from .statistics_card_names import index
        return index(data)
    except Exception:
        return {}

def print_rows(rows: list[dict], names: dict, errors: dict | None = None, all_rows: bool = False):
    shown = 0
    print(f"{'#':>3}  {'玩家':<18} {'卡组':<14} {'构筑':<14} 主卡组")
    for row in rows:
        known = bool(row.get('deck_type'))
        if not all_rows and known:
            continue
        state = row.get('deck_type') or '未识别'
        cards = main_cards_from_row(row, names)
        print(f"{row['rank']:>3}  {row['player']:<18} {state:<14} {row['build'][:12]:<14} {cards}")
        shown += 1
    if errors:
        for item in errors:
            print(f"  ! {item}")
    if not shown and not errors:
        print('（没有需要处理的玩家）')

def main_cards_from_row(row: dict, names: dict) -> str:
    # row 来自 latest_views，不含卡片；由调用方补充 build 的卡表查询。
    return row.get('_cards', '')

def attach_cards(db: sqlite3.Connection, rows: list[dict], names: dict, limit: int = 6):
    for row in rows:
        row['_cards'] = main_cards(db, row['build'], names, limit)
    return rows

def command_list(root: Path, args) -> int:
    data, db_path = paths(root)
    db = connect(db_path)
    now = time.time()
    rows = latest_views(db, now)
    names = name_index(data)
    attach_cards(db, rows, names)
    db.close()
    if args.json:
        print(json.dumps([{k: v for k, v in row.items() if not k.startswith('_')} for row in rows],
                         ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print('当前名单没有可用对局（名单可能未更新，或所有对局都已过期）。')
        return 0
    total = len(roster(connect(db_path)))
    print(f"当前前100：{total} 人；有有效对局 {len(rows)} 人；已识别 {sum(1 for r in rows if r['deck_type'])} 人。")
    if args.all:
        print_rows(rows, names, all_rows=True)
    else:
        print('\n还没识别出卡组的（按排名）：')
        print_rows(rows, names, all_rows=False)
        have = {row['player'].strip() for row in rows}
        missing = [p for p in roster(connect(db_path)) if p['player'].strip() not in have]
        if missing:
            print(f'\n没有有效对局、暂不能命名的（{len(missing)} 人）：')
            for person in missing:
                print(f"{person['rank']:>3}  {person['player']}")
    return 0

def main_cards_db(db_path: Path, build: str, names: dict) -> str:
    db = connect(db_path)
    try:
        return main_cards(db, build, names)
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest='command')
    listing = sub.add_parser('list', help='按排名查看未识别卡组')
    listing.add_argument('--all', action='store_true')
    listing.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.command is None:
        args.all, args.json = False, False
    return command_list(args.root.resolve(), args)


if __name__ == '__main__':
    raise SystemExit(main())
