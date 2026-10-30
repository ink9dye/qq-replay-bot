# -*- coding: utf-8 -*-
"""把订阅、观战保存、分享链接和 QQ 私聊接在一起。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .card_extras import CardExtras
from .account_guard import webui_login_id
from .card_search import CardSearch
from .player_history import PlayerHistory
from .commands import HELP, parse_command
from .help import help_cards
from .replay_window import REPLAY_WINDOW_SECONDS, replay_in_window
from .qq import OneBotClient, event_text, group_command_text
from .share import ShareApp, ShareServer, format_time, safe_replay_file
from .store import Store
from .watch_core import (
    RankRule,
    arena_rank_number,
    decks_from_replay,
    rank_is_within,
    ranks_from_filename,
    replay_recorded_time,
    safe_filename_part,
    serve,
)


def system_clock() -> float:
    """生产时钟。每次读取都是当前时间，不会停在进程启动的那一刻。"""
    return time.time()


class ManualClock:
    """测试注入的时钟。只有显式修改 now 时，读到的时间才会变化。"""

    def __init__(self, now: float):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


def join_rules(subscriptions: list[dict]) -> tuple[list[str], list[RankRule]]:
    """同一玩家只保留一份观战需求。有人要全部对局时直接观战，否则用最宽的名次。"""
    always: list[str] = []
    loosest: dict[str, int] = {}
    for sub in subscriptions:
        name = sub["player"]
        if sub["rank_top"] is None:
            if name not in always:
                always.append(name)
        else:
            loosest[name] = max(loosest.get(name, 0), int(sub["rank_top"]))
    rules = [RankRule(name, top) for name, top in loosest.items() if name not in always]
    return always, rules


def subscription_accepts(sub: dict, replay: dict) -> bool:
    """录像在订阅同一玩家的用户间共享。只按玩家、对手排名条件判断，最近14天由调用方另行限制。"""
    if replay["focus_player"] != sub["player"]:
        return False
    if sub["rank_top"] is None:
        return True
    opponents = [
        name
        for name in replay["players"]
        if name and name != "[Server]" and name != sub["player"]
    ]
    if not opponents:
        return False
    return any(rank_is_within(replay["ranks"].get(name), int(sub["rank_top"])) for name in opponents)


class BotService:
    def __init__(self, config: dict, root: Path, clock=None):
        self.config = config
        self.root = Path(root)
        self.data_dir = Path(config.get("data_dir") or (self.root / "data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ygopro_dir = Path(str(config.get("ygopro_dir") or r"D:\MyCardLibrary\ygopro"))
        self.replay_root = Path(str(config.get("replay_dir") or (self.data_dir / "replays")))
        self.deck_dir = Path(str(config.get("deck_dir") or (self.data_dir / "decks")))
        self.replay_root.mkdir(parents=True, exist_ok=True)
        self.deck_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "logs" / "bot.log"
        self.clock = system_clock if clock is None else clock
        self.store = Store(self.data_dir / "bot.sqlite3")
        # 旧版把每局开局时间当成收录时间。现有录像按落盘时间补齐，旧分享快照保持不变。
        legacy = []
        for replay in self.store.legacy_replays():
            path = safe_replay_file(self.replay_root, replay["rel_path"])
            saved_at = path.stat().st_mtime if path is not None else replay["recorded_at"]
            legacy.append((float(saved_at), int(replay["id"])))
        if legacy:
            self.store.backfill_saved_times(legacy)
        from .match_history import recover_legacy_sessions
        recover_legacy_sessions(self.store, self.replay_root, self.deck_dir)
        self._fact_signatures = {}
        self.index_existing_replays()
        self.sync_replay_facts()
        share_cfg = config.get("share") or {}
        qq_cfg = config.get("qq") or {}
        self.link_hours = float(share_cfg.get("link_hours") or 5)
        self.share = ShareApp(
            self.store,
            self.replay_root,
            self.clock,
            public_base_url=str(share_cfg.get("public_base_url") or ""),
            bind_host=str(share_cfg.get("bind_host") or "127.0.0.1"),
            bind_port=int(share_cfg.get("bind_port", 8787)),
            link_hours=self.link_hours,
        )
        self.share_server = ShareServer(self.share)
        self.tunnel = None
        self._public_status = None
        if share_cfg.get("quick_tunnel") is True:
            from .tunnel import QuickTunnel
            self.tunnel = QuickTunnel(self.root, self.data_dir, self.share.local_base(),
                                      int(share_cfg.get("tunnel_metrics_port", 47879)))
            self.share.public_url_provider = self.tunnel.current_url
        self.admins = {str(item) for item in (qq_cfg.get("admins") or [])}
        self.users = {str(item) for item in (qq_cfg.get("users") or [])}
        self.allow_all_users = qq_cfg.get("allow_all_users") is True
        self.group_commands = qq_cfg.get("group_commands") is True
        self.group_without_at = qq_cfg.get('group_without_at', True) is True
        self._webui_password = str(qq_cfg.get("webui_password") or "")
        self.qq: Optional[OneBotClient] = None
        self._deliver_lock: Optional[asyncio.Lock] = None
        self._guess_reveal_lock: Optional[asyncio.Lock] = None
        self.card_search = CardSearch(self.data_dir)
        self.card_extras = CardExtras(self.data_dir, self.card_search)
        self.player_history = PlayerHistory()

    def authorized(self, user_id) -> bool:
        text = str(user_id)
        return (self.allow_all_users and text.isdigit() and int(text) > 0) or text in self.admins or text in self.users

    def startup_lines(self) -> list[str]:
        mycard = self.config.get("mycard") or {}
        qq_cfg = self.config.get("qq") or {}
        public = self.share.public_base_url.strip()
        if self.tunnel is not None:
            public_line = "公网分享：" + ("免费临时地址已连接" if self.share.public_base() else "免费临时隧道正在连接")
        elif self.share.public_base():
            public_line = "公网地址：已填写"
        elif public:
            public_line = "公网地址：填写的是本机地址，好友无法打开。请改成隧道提供的 https 地址"
        else:
            public_line = "公网地址：未填写，分享链接只能在本机打开，不能当作好友可用的外网地址"
        lines = [
            "萌卡账号：" + ("已填写" if str(mycard.get("account") or "").strip() else "未填写，只能看到对局，不能加入观战"),
            "NapCat Token：" + ("已填写" if str(qq_cfg.get("access_token") or "").strip() else "未填写，QQ 私聊不会连接"),
            "NapCat WebUI 密码：" + ("已填写" if self._webui_password else "未在此保存，不影响机器人；网页密码由 NapCat 管理"),
            public_line,
            "使用权限：所有 QQ 私聊用户，各自管理自己的订阅" if self.allow_all_users else "授权人数：" + str(len(self.admins | self.users)),
            "群内指令：" + (("已启用，固定格式指令无需@，结果群内回复；新录像推送私发" if self.group_without_at else "已启用，@工具号 后发送指令，结果群内回复；新录像推送私发") if self.group_commands else "未启用"),
        ]
        if not self.allow_all_users and not (self.admins or self.users):
            lines.append("还没有管理员或授权使用者。未授权的 QQ 不能订阅，也不能查看录像。")
        return lines

    def write_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {message}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def watch_config(self) -> dict:
        mycard = self.config.get("mycard") or {}
        watch = self.config.get("watch") or {}
        host = str(mycard.get("host") or "").strip() or "tiramisu.moenext.com"
        return {
            "host": host,
            "port": int(mycard.get("port") or 8911),
            "version": mycard.get("version") or "0x1362",
            "nickname": str(mycard.get("nickname") or "").strip() or "Spectator",
            "account": str(mycard.get("account") or ""),
            "password": str(mycard.get("password") or ""),
            "mycard_secret": mycard.get("mycard_secret"),
            "join_mode": "auto",
            "name_match": "exact",
            "watch_waiting_rooms": bool(watch.get("watch_waiting_rooms", True)),
            "direct_rooms": [],
            "room_list_urls": [],
            "websocket_urls": [],
            "room_list_username": "",
            "room_list_password": "",
            "replay_dir": str(self.replay_root),
            "deck_dir": str(self.deck_dir),
            "cleanup_files": False,
            "poll_seconds": float(watch.get("poll_seconds") or 5),
            "max_concurrent": int(watch.get("max_concurrent") or 2),
            "join_after_min_seconds": float(watch.get("join_after_min_seconds") or 120),
            "join_after_max_seconds": float(watch.get("join_after_max_seconds") or 240),
        }

    def current_rules(self) -> tuple[list[str], list[RankRule]]:
        # 后端追踪所有订阅玩家，排名只影响前端输出，不影响卡组的新鲜度。
        names = list(dict.fromkeys(sub['player'] for sub in self.store.list_subscriptions()
                                   if self.authorized(sub['user_id'])))
        if self.config.get('_pycharm_watch_entry'):
            # 独立名单和QQ订阅使用同一个选房循环，同一房间只连接一次。
            from .watch_targets import standalone_targets
            for player in standalone_targets(self.data_dir / 'watch-config.json'):
                if player not in names: names.append(player)
        return names, []

    def handle_text(self, user_id, text: str, conversation: str = "") -> str:
        user = str(user_id)
        command = parse_command(text)
        if not self.authorized(user):
            if command.get("op") == "help":
                return HELP + "\n\n你还没有使用权限，不能订阅或查看录像。"
            return "你没有使用权限，不能订阅或查看录像。"
        op = command.get("op")
        if op == "help":
            return HELP
        if op == "empty":
            return "请发送“帮助”查看用法。"
        if op == "error":
            return command["message"]
        if op == "player_history":
            return self.player_history.handle(command["player"])
        if op in ("random", "guess_start", "guess_answer", "guess_reveal", "limits_card", "limits_table"):
            return self.card_extras.handle(conversation or user, op, command.get("value", ""))
        if op == "card_search":
            return self.card_search.handle((conversation + ":" + user) if conversation else user, "search", command["query"])
        if op == "card_select":
            return self.card_search.handle((conversation + ":" + user) if conversation else user, "select", command["index"])
        if op == "card_next":
            return self.card_search.handle((conversation + ":" + user) if conversation else user, "next")
        if op == "list":
            return self._list_text(user)
        if op == "subscribe":
            sub = self.store.upsert_subscription(
                user,
                command["player"],
                command["rank_top"],
                command["notify"],
                self.clock(),
            )
            return "已订阅。\n" + self._format_sub(sub) + "\n后台持续观测该玩家所有对局；推送和录像链接按以上条件筛选，卡组使用最新观测。"
        if op in ("push", "silent"):
            notify = op == "push"
            if not self.store.set_notify(user, command["player"], notify):
                return "你还没有订阅这个玩家。"
            word = "推送" if notify else "静默"
            return f"已把 {command['player']} 改为{word}。之后保存的新录像按这个方式处理。"
        if op == "unsubscribe":
            if not self.store.delete_subscription(user, command["player"]):
                return "你没有这个订阅。"
            return f"已取消订阅 {command['player']}。其他用户的订阅不受影响，已经在看的那盘会继续到结束。"
        if op == "replays":
            return self._replay_text(user, command["player"])
        if op == "unknown":
            answer = command.get('text') or ''
            if self.card_extras.is_guess_answer(conversation or user, answer):
                return self.card_extras.handle(conversation or user, 'guess_answer', answer)
            player = command.get("text") or ""
            if self.store.get_subscription(user, player):
                return self._replay_text(user, player)
            return "无法识别。发送“帮助”查看用法。已经订阅的玩家可以直接发送完整玩家名。"
        return "无法识别。发送“帮助”查看用法。"

    def _format_sub(self, sub: dict) -> str:
        scope = "全部" if sub["rank_top"] is None else f"前{sub['rank_top']}"
        mode = "推送" if sub["notify"] else "静默"
        return f"{sub['player']}  {scope}  {mode}  自 {format_time(sub['created_at'])}"

    def _list_text(self, user_id: str) -> str:
        rows = self.store.list_subscriptions(user_id)
        if not rows:
            return "你还没有订阅。"
        return "你的订阅：\n" + "\n".join(self._format_sub(row) for row in rows)

    def visible_replays(self, user_id: str, player: str) -> Optional[list[dict]]:
        sub = self.store.get_subscription(user_id, player)
        if sub is None:
            return None
        now = float(self.clock())
        return [replay for replay in self.store.replays_for_player(player)
                if subscription_accepts(sub, replay) and replay_in_window(replay, now)]

    def _replay_text(self, user_id: str, player: str) -> str:
        self.index_player_replays(player)
        from .match_history import recover_sessions_from_prefix
        recover_sessions_from_prefix(self.store, self.replay_root, player)
        rows = self.visible_replays(user_id, player)
        if rows is None:
            return "你还没有订阅这个玩家。"
        return self.directory_message(user_id, player, "这是当前符合你订阅条件的录像目录。", rows)

    def _wait_public_url(self, timeout: float = 30.0) -> bool:
        """免费隧道偶尔断线，这里等它自动重连。返回是否连上。"""
        if self.tunnel is None:
            return True
        if self.share.public_base():
            return True
        ensure = getattr(self.tunnel, 'ensure_started', None)
        if ensure is None:
            return False
        ensure()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.share.public_base():
                return True
            time.sleep(1)
        return bool(self.share.public_base())

    def latest_deck_label(self, link: dict) -> str:
        from .match_history import build_match_groups, supplemented_deck
        from .deck_links import observed_title, relative_age
        groups = build_match_groups(self.store, self.share.deck_observations(link), link['player'])
        current = supplemented_deck(groups)
        if current is None:
            return '尚未从有效观战录像中识别'
        title = observed_title(current['main'], current['extra'], current['side'], self.data_dir / 'cache')
        age = relative_age(current['group']['stamp'], float(self.clock()))
        return f'{title or "卡组类型待识别"}（{age}）'

    def directory_message(self, user_id: str, player: str, reason: str, rows: Optional[list[dict]] = None,
                          *, for_delivery: bool = False) -> str:
        sub = self.store.get_subscription(user_id, player)
        if sub is None:
            return "你还没有订阅这个玩家。"
        if not self._wait_public_url():
            return "" if for_delivery else "录像已保留在本机，公网分享暂未连接。请稍后再发送“录像 玩家名”查询。"
        if rows is None:
            rows = self.visible_replays(user_id, player) or []
        link = self.share.create_link(
            user_id,
            player,
            [row["id"] for row in rows],
            float(self.clock()) - REPLAY_WINDOW_SECONDS,
            deck_replay_ids=[replay['id'] for replay in self.store.replays_for_player(player)
                             if replay_in_window(replay, float(self.clock()))],
        )
        note = ""
        if self.tunnel is not None and not link["public"]:
            return "" if for_delivery else "录像已保留在本机，公网分享暂未连接。请稍后再发送“录像 玩家名”查询。"
        if not link["public"]:
            note = "\n还没有配置公网地址，这个链接只能在运行机器人的这台电脑上打开。"
        elif self.tunnel is not None:
            note = ('\n公网可访问时间：从现在起至下方最晚到期时间，且须保持电脑和当前隧道在线。'
                    '\n临时公网地址没有固定保活时长；网络中断、电脑休眠或公网地址变化都可能使链接提前打不开。'
                    f'打不开时请重新发送“录像 {player}”获取当前链接。')
        saved_link = self.store.get_link(link['token'])
        deck_label = self.latest_deck_label(saved_link)
        return (
            f"{reason}\n"
            f"玩家：{player}\n"
            f"最近使用卡组：{deck_label}\n"
            f"收录范围：{format_time(link['range_start'])} 至 {format_time(link['created_at'])} 已保存的录像\n"
            f"共 {len(rows)} 个文件（仅提供最近14天保存的录像）。链接最晚到期：{format_time(link['expires_at'])}"
            f"（最长 {self.link_hours:g} 小时，不保证公网地址持续在线。重新查询不会延长这一条）\n"
            f"{link['url']}"
            f"{note}"
        )

    def _focus_player(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self.replay_root.resolve())
        except ValueError:
            return ""
        if len(relative.parts) < 2:
            return ""
        folder = relative.parts[0]
        for sub in self.store.list_subscriptions():
            if safe_filename_part(sub["player"]) == folder:
                return sub["player"]
        # 独立名单及未来前100采集不要求QQ订阅；用录像内真实玩家校验目录。
        # 这里只建立事实索引，查询授权仍由订阅/分享范围独立判断。
        try:
            if path.stat().st_size <= 2 * 1024 * 1024:
                for name, _main, _extra in decks_from_replay(path.read_bytes()):
                    if name and name != '[Server]' and safe_filename_part(name) == folder:
                        return name
        except (OSError, ValueError):
            pass
        return ""

    def _index_replay_file(self, path: Path, focus: str) -> bool:
        """把一个录像文件加入索引，返回是否新增。"""
        try:
            relative = path.resolve().relative_to(self.replay_root.resolve())
        except ValueError:
            return False
        rel_path = str(relative)
        try:
            blob = path.read_bytes()
        except OSError:
            return False
        decks = decks_from_replay(blob)
        players = [name for name, _, _ in decks if name and name != "[Server]"]
        if not players:
            return False
        mtime = path.stat().st_mtime
        when = replay_recorded_time(blob)
        recorded = when.timestamp() if when else mtime
        file_ranks = ranks_from_filename(path.name)
        ranks = {}
        for name in players:
            key = name if name in file_ranks else safe_filename_part(name)
            if key in file_ranks:
                ranks[name] = file_ranks[key]
            else:
                ranks[name] = arena_rank_number(name)
        digest = hashlib.sha256(blob).hexdigest()
        self.store.add_replay(digest, focus, rel_path, players, ranks, recorded, saved_at=mtime)
        return True

    def index_player_replays(self, player: str) -> int:
        """查询时按目录补齐某玩家目录里的录像索引，让列表直接反映磁盘内容。"""
        directory = self.replay_root / safe_filename_part(player)
        if not directory.is_dir():
            return 0
        known = {replay["rel_path"] for replay in self.store.all_replays()}
        indexed = 0
        for path in sorted(directory.glob("*.yrp")):
            try:
                relative = path.resolve().relative_to(self.replay_root.resolve())
            except ValueError:
                continue
            if str(relative) in known:
                continue
            if self._index_replay_file(path, player):
                indexed += 1
        return indexed

    def index_existing_replays(self) -> int:
        """扫描录像目录，把已存在但还没索引的录像（例如 watch.py 保存的）加入索引。"""
        known = {replay["rel_path"] for replay in self.store.all_replays()}
        indexed = 0
        for path in sorted(self.replay_root.rglob("*.yrp")):
            try:
                relative = path.resolve().relative_to(self.replay_root.resolve())
            except ValueError:
                continue
            if str(relative) in known:
                continue
            focus = self._focus_player(path)
            if not focus:
                continue
            if self._index_replay_file(path, focus):
                indexed += 1
        if indexed:
            self.write_log(f"已索引 {indexed} 个本地已有录像")
        return indexed

    def on_replay_saved(self, written: list[Path], players: list[str], when, digest: str) -> None:
        saved_at = float(self.clock())
        recorded = float(when.timestamp()) if isinstance(when, datetime) else float(self.clock())
        ranks = {
            name: arena_rank_number(name)
            for name in players
            if name and name != "[Server]"
        }
        clean_players = [name for name in players if name]
        for path in written:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            focus = self._focus_player(path)
            if not focus:
                continue
            relative = path.resolve().relative_to(self.replay_root.resolve())
            replay, created = self.store.add_replay(
                digest,
                focus,
                str(relative),
                clean_players,
                ranks,
                recorded,
                saved_at=saved_at,
            )
            if not created:
                stored = self.replay_root / replay["rel_path"]
                if path.resolve() != stored.resolve() and path.exists():
                    path.unlink()
                continue
            for sub in self.store.list_subscriptions():
                if not self.authorized(sub["user_id"]):
                    continue
                if not subscription_accepts(sub, replay):
                    continue
                if sub["notify"]:
                    self.store.enqueue_notification(sub["user_id"], replay["id"], focus)

    def cleanup_old_files(self) -> int:
        """物理清理复用统一watch后端，前端只负责同步数据库索引。"""
        from .watch_core import cleanup_old_files
        now = float(self.clock())
        self.store.purge_expired_facts(now)
        timestamps = {}
        existing = set()
        for replay in self.store.all_replays():
            path = safe_replay_file(self.replay_root, replay['rel_path'])
            if path is not None:
                timestamps[str(path.resolve())] = float(replay['saved_at'])
                existing.add(replay['id'])
        removed = cleanup_old_files(self.replay_root, self.deck_dir, now, replay_times=timestamps)
        for replay in self.store.replays_older_than(now - REPLAY_WINDOW_SECONDS):
            if safe_replay_file(self.replay_root, replay['rel_path']) is None:
                self.store.delete_replay(replay['id'])
                if replay['id'] not in existing:
                    removed += 1
        if removed:
            self.write_log(f"已清理 {removed} 个超过14天的旧录像/卡组")
        self.sync_replay_facts()
        return removed

    def sync_replay_facts(self) -> int:
        """只同步新增或证据/解析已改变的文件；查询始终另行检查原始到期时间。"""
        from .share import safe_replay_file
        from .statistics_import import load_report, signature
        updated = 0
        existing = set()
        now = float(self.clock())
        for replay in self.store.all_replays():
            identifier = replay['id']
            existing.add(identifier)
            if not replay_in_window(replay, now):
                continue
            path = safe_replay_file(self.replay_root, replay['rel_path'])
            if path is None:
                continue
            try:
                marker = signature(path)
                if self._fact_signatures.get(identifier) == marker:
                    continue
                changed = self.store.import_replay_facts(replay, path, load_report(path), now)
                self._fact_signatures[identifier] = marker
                updated += int(changed)
            except (OSError, ValueError, TypeError, KeyError):
                self.write_log(f'录像{identifier}的原子记录暂未同步，保留未知待核对')
        self._fact_signatures = {key:value for key,value in self._fact_signatures.items() if key in existing}
        return updated

    async def cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.cleanup_old_files)
                from retention_policy import next_cleanup_delay
                delay = await asyncio.to_thread(next_cleanup_delay, float(self.clock()),
                                                self.replay_root, self.deck_dir,
                                                self.store.all_replays())
            except Exception as exc:
                self.write_log(f"清理旧文件失败：{exc}")
                delay = 60
            await asyncio.sleep(delay)

    async def deliver(self) -> int:
        if self._deliver_lock is None:
            self._deliver_lock = asyncio.Lock()
        async with self._deliver_lock:
            delivered = 0
            if self.qq is None or not getattr(self.qq, "connected", False):
                return 0
            if self.tunnel is not None and not self.share.public_base():
                return 0
            batches = {}
            for row in self.store.pending_notifications():
                sub = self.store.get_subscription(row["user_id"], row["player"])
                replay = self.store.get_replay(row["replay_id"])
                if (not self.authorized(row["user_id"]) or sub is None or not sub["notify"]
                        or replay is None or not subscription_accepts(sub, replay)
                        or not replay_in_window(replay, float(self.clock()))):
                    self.store.mark_skipped(row["user_id"], row["replay_id"])
                    continue
                batches.setdefault((row['user_id'], row['player']), []).append(row)
            for batch in batches.values():
                row = batch[0]
                text = self.directory_message(row["user_id"], row["player"], "已保存新的观战录像。", for_delivery=True)
                if not text:
                    continue
                ok = await self.qq.send_private(row["user_id"], text)
                if ok:
                    for item in batch:
                        self.store.mark_sent(item['user_id'], item['replay_id'])
                    delivered += len(batch)
                else:
                    for item in batch:
                        self.store.mark_retry(item['user_id'], item['replay_id'])
            return delivered

    def accept_group_guess(self, event, text):
        return (self.authorized(event.get('user_id')) and
                self.card_extras.is_guess_answer('group:' + str(event.get('group_id')), text))

    async def on_qq_event(self, event: dict) -> None:
        if self.qq is None:
            return
        is_group = event.get("message_type") == "group"
        if is_group:
            if not self.group_commands:
                return
            text = group_command_text(event, self.group_without_at, self.accept_group_guess)
            if text is None:
                return
        else:
            text = event_text(event)
        user_id = event.get("user_id")
        receiver = event["group_id"] if is_group else user_id
        send = self.qq.send_group if is_group else self.qq.send_private
        forward = self.qq.send_group_forward if is_group else self.qq.send_private_forward
        conversation = "group:" + str(receiver) if is_group else ""
        if parse_command(text).get('op') == 'help':
            cards = help_cards()
            if not self.authorized(str(user_id)):
                cards.append('你目前没有使用权限，不能订阅或查看录像。')
            ok = await forward(receiver, cards, event.get('self_id'))
            if not ok:
                self.write_log('合并转发帮助发送失败，尝试发送文字版。')
                reply = '合并转发帮助暂时发送失败，以下为文字版：\n\n' + await asyncio.to_thread(self.handle_text, user_id, text, conversation)
                ok = await send(receiver, reply)
        else:
            reply = await asyncio.to_thread(self.handle_text, user_id, text, conversation)
            pages = getattr(reply, 'forward_cards', None)
            if pages:
                ok = await forward(receiver, pages, event.get('self_id'))
                if not ok:
                    ok = await send(receiver, str(reply))
            else:
                ok = await send(receiver, reply)
        if not ok:
            self.write_log('QQ 指令回复发送失败，未改为其他会话发送。')

    async def notify_loop(self) -> None:
        while True:
            try:
                if self.tunnel is not None:
                    await asyncio.to_thread(self.tunnel.ensure_started)
                    public_ready = bool(self.share.public_base())
                    status = (public_ready, self.tunnel.last_error)
                    if status != self._public_status:
                        self._public_status = status
                        message = ("免费公网分享已连接，之后生成的录像链接可从外网打开。" if public_ready else
                                   (self.tunnel.last_error or "免费公网分享正在连接，待发送的录像通知会保留。"))
                        print(message, flush=True)
                        self.write_log(message)
                await self.deliver()
            except Exception as exc:
                self.write_log(f"发送通知失败：{exc}")
            await asyncio.sleep(2)

    async def napcat_watchdog(self) -> None:
        """只执行本机暂停请求；接口恢复由OneBot重连，不重启或重新登录QQ。"""
        while True:
            await asyncio.sleep(1)
            if self.qq is not None and not self.qq.suspended and (self.data_dir / 'qq-paused.json').exists():
                await self.qq.pause_for_attention('已暂停QQ收发；处理官方QQ提示后请运行 resume-qq.bat。')

    async def deliver_guess_reveals(self) -> int:
        if self._guess_reveal_lock is None:
            self._guess_reveal_lock = asyncio.Lock()
        async with self._guess_reveal_lock:
            pending = self.card_extras.pending_reveals()
            if self.qq is None or not self.qq.connected:
                return 0
            delivered = 0
            for key, conversation, reply in pending:
                if conversation.startswith('group:'):
                    receiver = conversation.split(':', 1)[1]
                    if not self.group_commands or not receiver.isdigit():
                        continue
                    ok = await self.qq.send_group(receiver, reply)
                elif self.authorized(conversation):
                    ok = await self.qq.send_private(conversation, reply)
                else:
                    self.card_extras.acknowledge_reveal(key)
                    continue
                if ok:
                    self.card_extras.acknowledge_reveal(key)
                    delivered += 1
            return delivered

    async def guess_reveal_loop(self) -> None:
        while True:
            try:
                await self.deliver_guess_reveals()
            except Exception:
                self.write_log('猜卡自动揭晓发送失败，稍后重试。')
            await asyncio.sleep(1)

    async def run(self) -> None:
        self.share_server.start()
        if self.tunnel is not None:
            self.tunnel.origin = self.share.local_base()
        for line in self.startup_lines():
            print(line, flush=True)
            self.write_log(line)
        self.write_log(f"分享服务监听 http://{self.share.bind_host}:{self.share.bind_port}/")
        qq_cfg = self.config.get("qq") or {}
        token = str(qq_cfg.get("access_token") or "").strip()
        tasks = [asyncio.create_task(self.notify_loop()), asyncio.create_task(self.cleanup_loop()),
                 asyncio.create_task(self.guess_reveal_loop()), asyncio.create_task(self.napcat_watchdog())]
        if token:
            def qq_status(message):
                print(message, flush=True)
                self.write_log(message)

            self.qq = OneBotClient(
                str(qq_cfg.get("onebot_ws") or "ws://127.0.0.1:3001"), token,
                self.on_qq_event, on_status=qq_status,
                accept_group_messages=self.group_commands,
                allow_unmentioned_commands=self.group_without_at,
                group_extra_accept=self.accept_group_guess,
                send_interval=float(qq_cfg.get('send_interval_seconds', 1)),
                destination_interval=float(qq_cfg.get('destination_interval_seconds', 2)),
                duplicate_reply_seconds=float(qq_cfg.get('duplicate_reply_seconds', 15)),
                request_cooldown=float(qq_cfg.get('request_cooldown_seconds', .8)),
                duplicate_command_seconds=float(qq_cfg.get('duplicate_command_seconds', 8)),
                state_dir=self.data_dir,
                expected_self_id=qq_cfg.get('expected_self_id'),
                account_probe=lambda: webui_login_id(self.root),
            )
            tasks.append(asyncio.create_task(self.qq.run()))
        else:
            self.write_log("未填写 NapCat Token，跳过 QQ 连接。")
        names, rules = self.current_rules()
        tasks.append(
            asyncio.create_task(
                serve(
                    self.watch_config(),
                    names,
                    rules,
                    rules_provider=self.current_rules,
                    on_saved=self.on_replay_saved,
                    on_session=self.store.record_session,
                )
            )
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.share_server.stop()
            self.store.close()


def load_bot_config(path: Path) -> dict:
    import json

    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise RuntimeError("配置文件必须是一个 JSON 对象")
    return {key: value for key, value in raw.items() if not str(key).startswith("_")}
