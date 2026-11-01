# -*- coding: utf-8 -*-
"""前100卡组识别/命名后台工具（只改构筑分类，不改对局事实）。

用法（在项目根目录）：
  卡组命名.cmd                       列出"当前前100里还没识别出卡组"的玩家，按排名排序
  卡组命名.cmd list --all            列出前100全部玩家与卡组
  卡组命名.cmd set --rank 5 --deck 混沌三幻魔            预览
  卡组命名.cmd set --rank 5 --deck 混沌三幻魔 --apply    写入
  卡组命名.cmd import --file 榜单.txt                    预览批量导入
  卡组命名.cmd import --file 榜单.txt --apply            写入

榜单文本格式（可直接粘贴你发的那种列表，多余的时间/分数/URL 会忽略）：
  1. 葬夜 · 混沌道化
  5. 宋五皇的子弟兵二號 · 混沌三幻魔（8 小时前） · 1625分

写入前会自动备份数据库到 data/classification-backups/；只认当前前100名单里的玩家，
缺有效对局的会单独列出来，不会瞎猜。已有系列标签保持不变（除非 set --series）。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime
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
    - stat_manual_decks：构筑 manual 优先，否则相似度匹配。
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


def parse_ladder(text: str) -> tuple[list[dict], list[str]]:
    entries, errors = [], []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith(('http://', 'https://')):
            continue
        # 允许 "1. 玩家 · 卡组（8 小时前） · 1625分" 或只有 "玩家 · 卡组"
        match = re.match(r'^(?:(\d+)\.\s*)?(.+?)\s*·\s*(.+)$', line)
        if not match:
            errors.append(f'第{number}行无法解析：{line}')
            continue
        rank_text, player, deck = match.groups()
        deck = re.split(r'[（(]', deck, 1)[0].strip()
        player = player.strip()
        if not player or not deck:
            errors.append(f'第{number}行缺少玩家或卡组：{line}')
            continue
        entries.append(dict(rank=int(rank_text) if rank_text else None, player=player, deck=deck))
    return entries, errors


def find_change(db: sqlite3.Connection, now: float, player: str, rank: int | None) -> dict:
    views = latest_views(db, now)
    by_player = {v['player']: v for v in views}
    by_rank = {v['rank']: v for v in views}
    view = by_player.get(player) or (by_rank.get(rank) if rank else None)
    if view is None:
        known = by_player.get(player)
        if known is None and rank is not None:
            hit = next((r for r in roster(db) if r['rank'] == rank), None)
            if hit:
                return dict(rank=hit['rank'], player=hit['player'], error='no_valid_view')
        return dict(rank=rank, player=player, error='not_in_top100')
    return view


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


def backup_database(db_path: Path) -> Path:
    target = db_path.parent / 'classification-backups'
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = target / f'bot.sqlite3.{stamp}.bak'
    source = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    dest = sqlite3.connect(backup)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return backup


def apply_changes(root: Path, db_path: Path, changes: list[dict], log: dict) -> dict:
    from .store import Store
    db = connect(db_path)
    revision_before = db.execute('SELECT revision FROM stat_classification_state').fetchone()[0]
    db.close()
    backup = backup_database(db_path)
    store = Store(db_path)
    applied = []
    try:
        for change in changes:
            view = change['view']
            build = view['build']
            row = store._db.execute('SELECT * FROM stat_builds WHERE fingerprint=?', (build,)).fetchone()
            series = [r[0] for r in store._db.execute(
                'SELECT series FROM stat_build_series WHERE build=? ORDER BY series', (build,))]
            version = (row['classification_version'] if row is not None else None) or 'manual'
            for attempt in range(5):
                try:
                    changed = store.classify_build(build, change['deck'], series, version)
                    break
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() or attempt == 4:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            applied.append(dict(rank=view['rank'], player=view['player'], build=build,
                                deck=change['deck'], changed=bool(changed)))
    finally:
        store.close()
    db = connect(db_path)
    revision_after = db.execute('SELECT revision FROM stat_classification_state').fetchone()[0]
    db.close()
    log.update(backup=str(backup), revision_before=revision_before,
               revision_after=revision_after, applied=applied)
    return log


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


def command_set(root: Path, args) -> int:
    data, db_path = paths(root)
    db = connect(db_path)
    view = find_change(db, time.time(), args.player or '', args.rank)
    names = name_index(data)
    db.close()
    if view.get('error') == 'no_valid_view':
        print(f"#{view['rank']} {view['player']} 当前没有有效对局（14 天内无可用录像），无法命名。")
        return 2
    if view.get('error') == 'not_in_top100':
        print(f"不在当前前100名单里：{args.player or ('#' + str(args.rank))}")
        return 2
    print(f"#{view['rank']} {view['player']}：{view['deck_type'] or '未识别'} → {args.deck}")
    print('主卡组：', main_cards(connect(db_path), view['build'], names))
    if args.rank:
        print('提示：当前前100名单每 2 分钟刷新，名次会漂移；写入建议用 --player（或先 list 再立刻 set）。')
    if not args.apply:
        print('\n（预览；加 --apply 才写入）')
        return 0
    if args.series:
        series = args.series
    else:
        db = connect(db_path)
        series = [r[0] for r in db.execute(
            'SELECT series FROM stat_build_series WHERE build=? ORDER BY series', (view['build'],))]
        db.close()
    log = apply_changes(root, db_path, [dict(view=view, deck=args.deck)], {})
    print(f"已写入：{log['applied'][0]['changed']} 处变更；备份 {log['backup']}")
    print(f"分类修订 {log['revision_before']} → {log['revision_after']}（网页刷新后生效）")
    return 0


def command_import(root: Path, args) -> int:
    data, db_path = paths(root)
    text = Path(args.file).read_text(encoding='utf-8-sig')
    entries, errors = parse_ladder(text)
    db = connect(db_path)
    now = time.time()
    views = latest_views(db, now)
    by_player = {v['player'].strip(): v for v in views}
    names = name_index(data)
    attach_cards(db, views, names)
    changes, skipped, problems = [], [], []
    for entry in entries:
        view = by_player.get(entry['player'].strip())
        if view is None:
            hit = next((r for r in roster(db) if r['player'].strip() == entry['player'].strip()), None)
            if hit:
                problems.append(f"#{entry['rank']} {entry['player']}（系统名单 #{hit['rank']}）：当前没有有效对局，暂不能命名")
            else:
                problems.append(f"#{entry['rank']} {entry['player']}：不在系统当前前100名单（名单可能还没刷新到最新，等有新对局后再命名）")
            continue
        if view['deck_type'] == entry['deck']:
            skipped.append(f"#{view['rank']} {view['player']}：已经是「{entry['deck']}」")
            continue
        changes.append(dict(view=view, deck=entry['deck']))
    db.close()
    print(f"榜单解析 {len(entries)} 条；待改 {len(changes)} 条；已是目标 {len(skipped)} 条；无法匹配 {len(problems)} 条。")
    for item in problems:
        print('  !', item)
    for item in skipped[:10]:
        print('  =', item)
    if args.json:
        print(json.dumps(dict(changes=[dict(rank=c['view']['rank'], player=c['view']['player'],
                                            build=c['view']['build'], deck=c['deck']) for c in changes],
                               problems=problems, skipped=skipped, errors=errors),
                         ensure_ascii=False, indent=2))
        return 0
    if not changes:
        print('没有需要写入的变更。')
        return 0
    print(f"\n{'#':>3}  {'玩家':<18} {'原卡组':<12} {'新卡组':<12} 主卡组")
    for change in changes[:args.limit]:
        view = change['view']
        print(f"{view['rank']:>3}  {view['player']:<18} {(view['deck_type'] or '未识别'):<12} "
              f"{change['deck']:<12} {view.get('_cards', '')}")
    if len(changes) > args.limit:
        print(f'… 另有 {len(changes) - args.limit} 条（--limit 可调）')
    if errors:
        for item in errors:
            print('  !', item)
    if not args.apply:
        print('\n（预览；加 --apply 才写入）')
        return 0
    log = apply_changes(root, db_path, changes, dict(mode='import', file=str(args.file)))
    print(f"\n已写入 {sum(1 for a in log['applied'] if a['changed'])} 条；备份 {log['backup']}")
    print(f"分类修订 {log['revision_before']} → {log['revision_after']}（网页刷新后生效）")
    report = Path(log['backup']).with_suffix('.import.json')
    report.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding='utf-8')
    print('导入报告：', report)
    return 0


def main_cards_db(db_path: Path, build: str, names: dict) -> str:
    db = connect(db_path)
    try:
        return main_cards(db, build, names)
    finally:
        db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='前100卡组识别/命名（只改分类）')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest='command')
    p_list = sub.add_parser('list', help='列出未识别卡组（--all 看全部）')
    p_list.add_argument('--all', action='store_true')
    p_list.add_argument('--json', action='store_true')
    p_set = sub.add_parser('set', help='给单个玩家命名卡组')
    group = p_set.add_mutually_exclusive_group(required=True)
    group.add_argument('--rank', type=int)
    group.add_argument('--player')
    p_set.add_argument('--deck', required=True)
    p_set.add_argument('--series', action='append', default=[])
    p_set.add_argument('--apply', action='store_true')
    p_import = sub.add_parser('import', help='从榜单文本批量命名')
    p_import.add_argument('--file', required=True)
    p_import.add_argument('--apply', action='store_true')
    p_import.add_argument('--json', action='store_true')
    p_import.add_argument('--limit', type=int, default=100)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not (root / 'config.json').is_file():
        print(f'找不到 {root / "config.json"}；请在项目根目录运行，或用 --root 指定。')
        return 2
    if args.command in (None, 'list'):
        if args.command is None:
            args.command, args.all, args.json = 'list', False, False
        return command_list(root, args)
    if args.command == 'set':
        return command_set(root, args)
    if args.command == 'import':
        return command_import(root, args)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
