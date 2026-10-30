# -*- coding: utf-8 -*-
"""把订阅、观战保存、分享链接和 QQ 私聊接在一起。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .commands import HELP, parse_command
from .qq import OneBotClient, event_text
from .share import ShareApp, ShareServer, format_time
from .store import Store
from .watch_core import RankRule, arena_rank_number, rank_is_within, safe_filename_part, serve


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
    """每位用户只看自己的订阅。别人放宽条件，不会把不符合的录像分过来。"""
    if replay["focus_player"] != sub["player"]:
        return False
    if float(replay["recorded_at"]) < float(sub["created_at"]):
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
        self.replay_root = self.data_dir / "replays"
        self.replay_root.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "decks").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "logs" / "bot.log"
        self.clock = system_clock if clock is None else clock
        self.store = Store(self.data_dir / "bot.sqlite3")
        share_cfg = config.get("share") or {}
        qq_cfg = config.get("qq") or {}
        self.link_hours = float(share_cfg.get("link_hours") or 5)
        self.share = ShareApp(
            self.store,
            self.replay_root,
            self.clock,
            public_base_url=str(share_cfg.get("public_base_url") or ""),
            bind_host=str(share_cfg.get("bind_host") or "127.0.0.1"),
            bind_port=int(share_cfg.get("bind_port") or 8787),
            link_hours=self.link_hours,
        )
        self.share_server = ShareServer(self.share)
        self.admins = {str(item) for item in (qq_cfg.get("admins") or [])}
        self.users = {str(item) for item in (qq_cfg.get("users") or [])}
        self._webui_password = str(qq_cfg.get("webui_password") or "")
        self.qq: Optional[OneBotClient] = None
        self._deliver_lock: Optional[asyncio.Lock] = None

    def authorized(self, user_id) -> bool:
        text = str(user_id)
        return text in self.admins or text in self.users

    def startup_lines(self) -> list[str]:
        mycard = self.config.get("mycard") or {}
        qq_cfg = self.config.get("qq") or {}
        public = self.share.public_base_url.strip()
        if self.share.public_base():
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
            "授权人数：" + str(len(self.admins | self.users)),
        ]
        if not (self.admins or self.users):
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
            "deck_dir": str(self.data_dir / "decks"),
            "poll_seconds": float(watch.get("poll_seconds") or 5),
            "max_concurrent": int(watch.get("max_concurrent") or 2),
            "join_after_min_seconds": float(watch.get("join_after_min_seconds") or 120),
            "join_after_max_seconds": float(watch.get("join_after_max_seconds") or 240),
        }

    def current_rules(self) -> tuple[list[str], list[RankRule]]:
        return join_rules([sub for sub in self.store.list_subscriptions() if self.authorized(sub["user_id"])])

    def handle_text(self, user_id, text: str) -> str:
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
            return "已订阅。\n" + self._format_sub(sub) + "\n之后的新对局会按这个条件观战，已经开始的观战不会被打断。"
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
        return [replay for replay in self.store.replays_for_player(player) if subscription_accepts(sub, replay)]

    def _replay_text(self, user_id: str, player: str) -> str:
        rows = self.visible_replays(user_id, player)
        if rows is None:
            return "你还没有订阅这个玩家。"
        return self.directory_message(user_id, player, "这是当前符合你订阅条件的录像目录。", rows)

    def directory_message(self, user_id: str, player: str, reason: str, rows: Optional[list[dict]] = None) -> str:
        sub = self.store.get_subscription(user_id, player)
        if sub is None:
            return "你还没有订阅这个玩家。"
        if rows is None:
            rows = self.visible_replays(user_id, player) or []
        link = self.share.create_link(
            user_id,
            player,
            [row["id"] for row in rows],
            sub["created_at"],
        )
        note = ""
        if not link["public"]:
            note = "\n还没有配置公网地址，这个链接只能在运行机器人的这台电脑上打开。"
        return (
            f"{reason}\n"
            f"玩家：{player}\n"
            f"收录范围：{format_time(link['range_start'])} 至 {format_time(link['created_at'])} 已保存的录像\n"
            f"共 {len(rows)} 个文件。链接到期：{format_time(link['expires_at'])}"
            f"（生成后 {self.link_hours:g} 小时。重新查询会生成新链接，不会延长这一条）\n"
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
        return ""

    def on_replay_saved(self, written: list[Path], players: list[str], when, digest: str) -> None:
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

    async def deliver(self) -> int:
        if self._deliver_lock is None:
            self._deliver_lock = asyncio.Lock()
        async with self._deliver_lock:
            delivered = 0
            if self.qq is None or not getattr(self.qq, "connected", False):
                return 0
            for row in self.store.pending_notifications():
                sub = self.store.get_subscription(row["user_id"], row["player"])
                replay = self.store.get_replay(row["replay_id"])
                if (not self.authorized(row["user_id"]) or sub is None or not sub["notify"]
                        or replay is None or not subscription_accepts(sub, replay)):
                    self.store.mark_skipped(row["user_id"], row["replay_id"])
                    continue
                text = self.directory_message(row["user_id"], row["player"], "已保存新的观战录像。")
                ok = await self.qq.send_private(row["user_id"], text)
                if ok:
                    self.store.mark_sent(row["user_id"], row["replay_id"])
                    delivered += 1
                else:
                    self.store.mark_retry(row["user_id"], row["replay_id"])
            return delivered

    async def on_qq_event(self, event: dict) -> None:
        if self.qq is None:
            return
        reply = self.handle_text(event.get("user_id"), event_text(event))
        await self.qq.send_private(event.get("user_id"), reply)

    async def notify_loop(self) -> None:
        while True:
            try:
                await self.deliver()
            except Exception as exc:
                self.write_log(f"发送通知失败：{exc}")
            await asyncio.sleep(2)

    async def run(self) -> None:
        self.share_server.start()
        for line in self.startup_lines():
            print(line, flush=True)
            self.write_log(line)
        self.write_log(f"分享服务监听 http://{self.share.bind_host}:{self.share.bind_port}/")
        qq_cfg = self.config.get("qq") or {}
        token = str(qq_cfg.get("access_token") or "").strip()
        tasks = [asyncio.create_task(self.notify_loop())]
        if token:
            def qq_status(message):
                print(message, flush=True)
                self.write_log(message)

            self.qq = OneBotClient(
                str(qq_cfg.get("onebot_ws") or "ws://127.0.0.1:3001"), token,
                self.on_qq_event, on_status=qq_status,
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
