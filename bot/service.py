# -*- coding: utf-8 -*-
"""把订阅、观战保存、分享链接和 QQ 私聊接在一起。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from contextlib import closing
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .card_extras import CardExtras
from .account_guard import webui_login_id
from .card_search import CardReply, CardSearch
from .player_history import PlayerHistory
from .price_reservations import PriceReservations
from .commands import HELP, PRICE_HELP, parse_command
from .roles import normalize_roles
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


def join_mode_value(value) -> str:
    """房间口令模式：auto（有凭证用萌卡口令）/ plain（口令=房间号）/ mycard。"""
    mode = str(value or "").strip().lower()
    return mode if mode in ("auto", "plain", "mycard") else "auto"


def url_items(value) -> list[str]:
    """房间列表/推送地址只接受非空字符串；配置写错时宁可为空，也不猜。"""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def room_items(value) -> list:
    """直连房间只接受房间号字符串或 {id,title} 字典。"""
    if not isinstance(value, list):
        return []
    rooms = []
    for item in value:
        if isinstance(item, str) and item.strip():
            rooms.append(item.strip())
        elif isinstance(item, dict):
            room_id = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            if room_id or title:
                rooms.append({"id": room_id, "title": title or room_id})
    return rooms


PUBLIC_BASE_MAX_AGE_SECONDS = 300.0

_PRIVATE_HOST_PREFIXES = ("192.168.", "10.", "127.", "172.16.", "172.17.", "172.18.", "172.19.",
                          "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
                          "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")


def valid_public_https(base) -> str:
    """只接受"非本机/非内网的 https 地址"；其它一律返回空串（调用方回退）。"""
    text = str(base or "").strip().rstrip("/")
    if not text.startswith("https://"):
        return ""
    try:
        host = (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""
    if not host or host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        return ""
    if host.startswith(_PRIVATE_HOST_PREFIXES):
        return ""
    return text


def read_external_public_base(data_dir, fallback="", *, max_age=PUBLIC_BASE_MAX_AGE_SECONDS,
                              now=None) -> str:
    """读独立 share 进程写的 data/share-public-base.json。

    文件缺失/过期/损坏 → 用 config 的 public_base_url；结果仍必须是"非本机 https"，
    否则返回空串（QQ 会提示未配置公网地址，而不是发一个 127.0.0.1 的假链接）。
    """
    from .share_service import read_public_base, share_public_base_path
    moment = time.time() if now is None else float(now)
    try:
        path = share_public_base_path(data_dir)
        fresh = path.is_file() and (moment - path.stat().st_mtime) <= float(max_age)
    except OSError:
        fresh = False
    value = read_public_base(data_dir, fallback) if fresh else fallback
    return valid_public_https(value)


class BotService:
    def __init__(self, config: dict, root: Path, clock=None, roles=None):
        # roles=None 仍是全开；拆分后每个进程只跑自己的任务，但共用同一个库。
        self.roles = normalize_roles(roles)
        self._started_at = time.time()
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
        self._fact_signatures = {}
        self._top100_enabled = bool(config.get('_pycharm_watch_entry')) and (config.get('statistics') or {}).get('top100_enabled', True) is not False
        self._capture_status_written = 0.0
        self._classification_rules_signature = None
        # 事实/录像索引只由观战进程写（设计：只有 collector 写事实库）。
        # 拆分后的 QQ 进程只读同一份库，启动时不再扫描录像、不再补写事实表，
        # 这样重启 QQ 窗口不会和正在录的观战抢写锁；M3 再把 QQ 的写请求交给 collector。
        if "watch" in self.roles:
            self.index_collector_data()
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
        if bool(config.get('_pycharm_watch_entry')):
            from .statistics_portal import StatisticsPortal, owner_secret
            try:
                secret=owner_secret(self.data_dir/'statistics-owner.key')
                self.share.statistics_portal=StatisticsPortal(self.store.path,self.replay_root,self.clock,secret)
            except (OSError,ValueError):
                print('本机统计入口暂不可用：入口凭据文件无法安全读取。')
        self.share_server = ShareServer(self.share)
        self.tunnel = None
        self._public_status = None
        # share.mode=process（或 tunnel_owner=share）：分享与隧道都归独立 share_service，
        # QQ/watch 不再建自己的隧道，公网 base 直接读 share 进程写的 share-public-base.json；
        # 文件缺失/过期/损坏时回退 config 的 public_base_url（且必须是 非本机 https）。
        self._external_public_base = (
            self.share_external()
            or str(share_cfg.get("tunnel_owner") or "").strip().lower() == "share"
        )
        if self._external_public_base:
            fallback_base = str(share_cfg.get("public_base_url") or "")
            self.share.public_url_provider = (
                lambda: read_external_public_base(self.data_dir, fallback_base)
            )
        elif share_cfg.get("quick_tunnel") is True:
            self.tunnel = self.build_tunnel(share_cfg)
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
        self.player_history = PlayerHistory(log=self.write_log)
        # 价格库可能被 price-watch 占用：不在这里打开，第一次用到“预定”时再连。
        self._price_db = (self.config.get("price_watch") or {}).get("db")
        self._price_reservations = None
        self._price_ui_lock = threading.Lock()
        self._price_quote_tasks = set()
        self._deck_cache = {}
        self._deck_tasks = set()

    @property
    def price_reservations(self):
        if self._price_reservations is None:
            self._price_reservations = PriceReservations(self._price_db)
        return self._price_reservations

    def authorized(self, user_id) -> bool:
        text = str(user_id)
        return (self.allow_all_users and text.isdigit() and int(text) > 0) or text in self.admins or text in self.users

    def default_query_rule_line(self) -> str:
        """启动时确认“未订阅默认对手前100”落在当前榜单上；名单缺失时明确说明不把未知当上榜。

        口径提醒：判断用的是**录像收录时**的排名，不是查询时的当前榜单快照，所以榜单刷新/掉榜
        不会改写已收录录像的查询结果（掉榜仅让该方视角失效，重回按原期限恢复）。
        """
        try:
            roster = self.store.top100_snapshot(float(self.clock()))
        except (OSError, ValueError, KeyError, AttributeError):
            return "录像查询默认：未订阅按对手前100；榜单读取失败，排名未知的对手不当作前100"
        if not roster.get("available"):
            return "录像查询默认：未订阅按对手前100；当前前100名单尚未加载，排名未知的对手不当作前100"
        stamp = datetime.fromtimestamp(roster.get("updated_at") or 0).strftime("%m-%d %H:%M")
        stale = "，名单已过期，仍按最近一次完整名单判断" if roster.get("stale") else ""
        return (f"录像查询默认：未订阅按对手前100（榜单v{roster.get('version')}，{stamp}{stale}；"
                "按录像收录时的排名判断，不是当前榜单快照）；已订阅沿用个人条件")

    def startup_lines(self) -> list[str]:
        mycard = self.config.get("mycard") or {}
        qq_cfg = self.config.get("qq") or {}
        public = self.share.public_base_url.strip()
        if self.tunnel is not None:
            public_line = "公网分享：" + ("免费临时地址已连接" if self.share.public_base() else "免费临时隧道正在连接")
        elif self.share.public_base():
            base = self.share.public_base()
            if self._external_public_base:
                public_line = "公网地址：已读取分享服务的 https 地址（独立 share 进程）：" + base
            else:
                public_line = "公网地址：已填写：" + base
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
            self.default_query_rule_line(),
        ]
        if not self.allow_all_users and not (self.admins or self.users):
            lines.append("还没有管理员或授权使用者。未授权的 QQ 不能订阅，也不能查看录像。")
        return lines

    def write_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {message}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def share_external(self) -> bool:
        """share.mode=process 时，分享与隧道由独立 share_service 提供，watch 不再监听 8787。"""
        share_cfg = self.config.get("share") or {}
        return str(share_cfg.get("mode") or "inline").strip().lower() == "process"

    def write_watch_status(self, status: str = 'running') -> None:
        """写 data/watch-status.json：pid/started_at/updated_at/roles/health（读不到/过期=未运行）。"""
        from .watch_status import write as write_watch_status
        try:
            write_watch_status(
                self.data_dir,
                pid=os.getpid(),
                roles=self.roles,
                started_at=getattr(self, '_started_at', 0.0),
                status=status,
                entry='pycharm/watch.py' if self.config.get('_pycharm_watch_entry') else '',
            )
        except OSError:
            pass

    async def watch_status_loop(self) -> None:
        while True:
            self.write_watch_status()
            await asyncio.sleep(10)
    def build_tunnel(self, share_cfg: dict):
        """按 share.tunnel_mode 选隧道：缺省 ssh（localhost.run）；cpolar 是候选实现。

        两种对象都提供 ensure_started / current_url / last_error / origin，QQ 进程只读它们的地址。
        免费版只允许 1 条隧道，所以 cpolar 用 data/cpolar-tunnel.lock 防重复启动。
        """
        from .cpolar_tunnel import resolve_mode
        try:
            mode = resolve_mode(share_cfg)
        except ValueError:
            mode = "ssh"
            self.write_log("share.tunnel_mode 配置无效，按 ssh 隧道处理。")
        if mode == "cpolar":
            from .cpolar_runtime import CpolarTunnel
            return CpolarTunnel(self.root, self.data_dir, self.share.local_base(), share_cfg)
        from .tunnel import QuickTunnel
        return QuickTunnel(self.root, self.data_dir, self.share.local_base(),
                           int(share_cfg.get("tunnel_metrics_port", 47879)))

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
            "join_mode": join_mode_value(watch.get("join_mode")),
            "name_match": "exact",
            "watch_waiting_rooms": bool(watch.get("watch_waiting_rooms", True)),
            # 房间来源允许由 config.json 指定（三个都为空时用观战核心自带的萌卡进行中对局列表）。
            # 只传字符串/房间字典；没配置时与原来完全一致。
            "direct_rooms": room_items(watch.get("direct_rooms")),
            "room_list_urls": url_items(watch.get("room_list_urls")),
            "websocket_urls": url_items(watch.get("websocket_urls")),
            "room_list_username": "",
            "room_list_password": "",
            "replay_dir": str(self.replay_root),
            "deck_dir": str(self.deck_dir),
            "cleanup_files": False,
            "poll_seconds": float(watch.get("poll_seconds") or 5),
            "max_concurrent": int(watch.get("max_concurrent") or 2),
            "automatic_rank_limit": 200 if self._top100_enabled else 100,
            "max_queued_rooms": int(watch.get("max_queued_rooms") or 200),
            "join_after_min_seconds": float(watch.get("join_after_min_seconds") or 120),
            "join_after_max_seconds": float(watch.get("join_after_max_seconds") or 240),
            # 以下三项沿用生产 2026-10-30 23:18 的既有改动（启动时不再一次性扑中途局）。
            "join_preexisting": bool(watch.get("join_preexisting", False)),
            "preexisting_join_min_seconds": float(watch.get("preexisting_join_min_seconds") or 1),
            "preexisting_join_max_seconds": float(watch.get("preexisting_join_max_seconds") or 10),
            # 进场节流四键：只透传，策略在 bot/join_throttle.py（由 watch.py 消费）。
            # 用 is not None 判断，允许把间隔/暂停显式配成 0（测试或临时放开）。
            "max_join_concurrent": int(watch.get("max_join_concurrent")
                                       if watch.get("max_join_concurrent") is not None else 2),
            "join_min_interval_seconds": float(watch.get("join_min_interval_seconds")
                                               if watch.get("join_min_interval_seconds") is not None else 5),
            "join_fail_threshold": int(watch.get("join_fail_threshold")
                                       if watch.get("join_fail_threshold") is not None else 3),
            "join_pause_seconds": float(watch.get("join_pause_seconds")
                                        if watch.get("join_pause_seconds") is not None else 60),
            "join_interval_jitter_seconds": watch.get("join_interval_jitter_seconds"),
            "join_max_room_age_seconds": watch.get("join_max_room_age_seconds"),
            "night_mode_enabled": watch.get("night_mode_enabled"),
            "night_start_hour": watch.get("night_start_hour"),
            "night_end_hour": watch.get("night_end_hour"),
            "night_min_concurrent": watch.get("night_min_concurrent"),
            "night_max_concurrent": watch.get("night_max_concurrent"),
            "night_min_interval_seconds": watch.get("night_min_interval_seconds"),
            "night_jitter_seconds": watch.get("night_jitter_seconds"),
            "startup_catchup_limit": watch.get("startup_catchup_limit"),
            "probation_enabled": watch.get("probation_enabled"),
            "max_joins_per_hour": watch.get("max_joins_per_hour"),
        }

    def current_rules(self) -> tuple[list[str], list[RankRule]]:
        # 后端追踪所有订阅玩家，排名只影响前端输出，不影响卡组的新鲜度。
        names = list(dict.fromkeys(sub['player'] for sub in self.store.list_subscriptions()
                                   if self.authorized(sub['user_id'])))
        from .watch_core import set_watch_target_reasons
        reasons={name:'QQ关注名单' for name in names}
        if self.config.get('_pycharm_watch_entry'):
            # 独立名单和QQ订阅使用同一个选房循环，同一房间只连接一次。
            from .watch_targets import standalone_targets
            for player in standalone_targets(self.data_dir / 'watch-config.json'):
                if player not in names: names.append(player)
                reasons[player]=reasons.get(player,'')+('；' if player in reasons else '')+'独立关注名单'
            if self._top100_enabled:
                from .watch_core import set_official_rank_snapshot
                roster=self.store.top100_snapshot(float(self.clock()))
                set_official_rank_snapshot(roster['members'],roster['updated_at'],roster['stale'])
                # 保留旧名单用于可追溯查询，但不能据过期榜单扩展新观战。
                for member in (roster['members'] if not roster['stale'] else []):
                    if member['player'] not in names:names.append(member['player'])
                    stamp=datetime.fromtimestamp(roster['updated_at']).strftime('%H:%M:%S')
                    name=member['player']
                    reasons[name]=reasons.get(name,'')+('；' if name in reasons else '')+f"前100第{member['rank']}名，榜单v{roster['version']}／{stamp}"
        set_watch_target_reasons(reasons)
        return names, []

    async def top100_loop(self):
        from .top100 import fetch,REFRESH_SECONDS,error_category
        while True:
            try:
                previous = self.store.top100_snapshot(float(self.clock()))
                payload=await asyncio.to_thread(fetch)
                state=self.store.replace_top100(payload,float(self.clock()))
                stamp=datetime.fromtimestamp(state['updated_at']).strftime('%H:%M:%S')
                message=f"官方当前前100已刷新：{stamp}，版本{state['version']}，完整100名；既有对局继续录完"
                print(message,flush=True);self.write_log(message)
                if previous.get('consecutive_failures'):
                    recovery = (f"平台接口（sapi）已恢复：前100刷新成功，此前连续失败 "
                                f"{previous['consecutive_failures']} 轮")
                    print(recovery,flush=True);self.write_log(recovery)
            except (OSError,ValueError,TypeError) as error:
                self.store.top100_failed(float(self.clock()),error_category(error))
                state=self.store.top100_snapshot(float(self.clock()))
                stamp=datetime.fromtimestamp(state['updated_at']).strftime('%m-%d %H:%M:%S') if state['updated_at'] else '尚无成功名单'
                message=(f"官方前100刷新失败：{state['last_error']}，连续{state['consecutive_failures']}轮；"
                         f"上次成功{stamp}，保留完整旧名单并标记过期；暂停新增前100自动观战，关注及在录对局继续")
                print(message,flush=True);self.write_log(message)
            await asyncio.sleep(REFRESH_SECONDS)

    def on_capture_status(self, status):
        import json
        import os
        now=float(self.clock())
        if now-self._capture_status_written < 10:return
        # 与 capture-status 同频刷新 watch-status.json（share 独立后它是唯一的 watch 存活来源）。
        # 用 getattr 兼容测试里的 duck-type 假对象（没有这个方法时跳过，不影响 capture-status）。
        writer = getattr(self, 'write_watch_status', None)
        if callable(writer):
            try:
                writer()
            except OSError:
                pass
        roster=self.store.top100_snapshot(now)
        payload=dict(status, watch_pid=os.getpid(), top100_enabled=self._top100_enabled, top100_version=roster['version'],
                     top100_updated_at=roster['updated_at'], top100_stale=roster['stale'],
                     top100_last_error=roster['last_error'],top100_consecutive_failures=roster['consecutive_failures'],
                     top100_member_count=len(roster['members']),
                     sapi_ok_at=roster['updated_at'], sapi_error=roster['last_error'],
                     sapi_failures=roster['consecutive_failures'], sapi_stale=roster['stale'])
        path=self.data_dir/'capture-status.json'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
        temporary.replace(path)
        self._capture_status_written=now

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
        if op == "top100_rank_build":
            return self._top100_rank_build_text(user, command["rank"])
        if op == "top100_deck":
            return self._top100_deck_text(command["keyword"])
        if op == "top100_pie":
            return self._top100_pie_reply()
        if op == "price_quote":
            return self._price_quote_blocking(command)

        if op == "price_help":
            return PRICE_HELP
        if op == "price_reserve":
            try:
                reservation_id = self.price_reservations.add(
                    user, command["name"], command["rarity"],
                    command["target"], command.get("language") or "日文")
                marked = str(user) in {str(item) for item in
                                       (self._price_cfg().get("marked_users") or [])}
                if not marked:
                    # 普通用户表面上只保留一张卡；不提示限制，也不提标记用户。
                    self.price_reservations.deactivate_others(user, reservation_id)
            except (ValueError, sqlite3.Error) as exc:
                return str(exc)
            label = f"{command['name']} {command['rarity']} {command.get('language') or '日文'}".strip()
            return f"已记录卡价预定：{label}，预期价 {command['target']:g} 元。到价后只私聊你，不发群。"
        if op == "price_reservations":
            rows = self.price_reservations.list_for(user)
            if not rows:
                return "你还没有卡价预定。发送：预定 卡名 罕贵 日文/简中 预期价"
            lines = ["你的卡价预定："]
            for row in rows:
                state = "已到价" if row["notified_at"] else "监控中"
                rar = f" {row['rarity']}" if row["rarity"] else ""
                lang = f" {row['language']}" if row.get("language") else ""
                lines.append(f"{row['name']}{rar}{lang}  预期 {row['target']:g} 元  {state}")
            return "\n".join(lines)
        if op == "price_cancel":
            try:
                count = self.price_reservations.cancel(user, command["name"],
                                                       command["rarity"], command.get("language") or "")
            except (ValueError, sqlite3.Error) as exc:
                return str(exc)
            label = f"{command['name']} {command['rarity']} {command.get('language') or ''}".strip()
            if count:
                return f"已取消 {count} 条卡价预定：{label}。"
            return f"没有找到这条预定：{label}。"
        if op == "stats_page":
            return self._stats_text(user)
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
        if op == "deck_build":
            return self._deck_build_text(user, command["player"])
        if op == "unknown":
            answer = command.get('text') or ''
            if self.card_extras.is_guess_answer(conversation or user, answer):
                return self.card_extras.handle(conversation or user, 'guess_answer', answer)
            # 有查卡候选时：裸数字=选卡，next=下一页；没有候选就不抢这两句。
            session_key = (conversation + ":" + user) if conversation else user
            if self.card_search.has_candidates(session_key, max_age=120):
                if answer.isascii() and answer.isdigit() and len(answer) <= 4:
                    return self.card_search.handle(session_key, 'select', answer)
                if answer.casefold() == 'next':
                    return self.card_search.handle(session_key, 'next')
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

    def _stats_text(self, user_id: str) -> str:
        """统计网页只读入口：2 小时 token，群里所有人可点。"""
        if not self._wait_public_url():
            return "统计页已就绪，公网分享暂未连接。请稍后再发送“统计”。"
        link = self.share.create_stats_link(user_id, hours=2)
        note = "" if link["public"] else "\n还没有配置公网地址，这个链接只能在本机打开。"
        return f"统计网页（2 小时内有效）：{link['url']}" + note

    def visible_replays(self, user_id: str, player: str) -> Optional[list[dict]]:
        sub = self.store.get_subscription(user_id, player)
        if sub is None:
            sub = dict(player=player, rank_top=100, notify=False)
        now = float(self.clock())
        return [replay for replay in self.store.replays_for_player(player)
                if subscription_accepts(sub, replay) and replay_in_window(replay, now)]

    def _deck_cache_key(self, user_id, player):
        """缓存键带上最新构筑与识别结果，网页改名后不会继续吐旧“未识别”。"""
        try:
            with closing(sqlite3.connect(self.store.path.resolve().as_uri() + '?mode=ro',
                                         uri=True, timeout=5)) as db:
                row = db.execute(
                    "SELECT v.build,b.deck_type,g.recorded_at FROM stat_views v "
                    "JOIN stat_games g ON g.digest=v.digest "
                    "JOIN stat_builds b ON b.fingerprint=v.build "
                    "WHERE v.player=? AND g.expires_at>? "
                    "ORDER BY g.recorded_at DESC LIMIT 1",
                    (player, float(self.clock()))).fetchone()
                manual = db.execute("SELECT COALESCE(MAX(updated_at),0) FROM stat_manual_decks").fetchone()[0]
            return ((str(user_id), player, row[0], row[1], row[2], manual)
                    if row else (str(user_id), player, None, manual))
        except sqlite3.Error:
            return (str(user_id), player, None)

    def _deck_build_text(self, user_id: str, player: str):
        """构筑 玩家名：最新构筑图（base64）+ 本链接的 ydk 下载地址。"""
        cache_key = self._deck_cache_key(user_id, player)
        cached = self._deck_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        self.index_player_replays(player)
        from .match_history import recover_sessions_from_prefix
        recover_sessions_from_prefix(self.store, self.replay_root, player)
        if not self._wait_public_url():
            return "构筑已保留在本机，公网分享暂未连接。请稍后再发送“构筑 玩家名”。"
        now = float(self.clock())
        rows = [row for row in (self.visible_replays(user_id, player) or [])
                if safe_replay_file(self.replay_root, row['rel_path'])]
        deck_ids = [replay['id'] for replay in self.store.replays_for_player(player)
                    if replay_in_window(replay, now)
                    and safe_replay_file(self.replay_root, replay['rel_path'])]
        if not deck_ids:
            return f"没有找到 {player} 最近14天的有效录像，暂时没有构筑图。"
        link = self.share.create_link(
            user_id, player, [row['id'] for row in rows], now - REPLAY_WINDOW_SECONDS,
            deck_replay_ids=deck_ids,
        )
        if not link['public']:
            return "还没有配置公网地址，构筑图和 YDK 链接只能在运行机器人的这台电脑上使用。"
        from .statistics_build_detail import query as build_query
        with closing(sqlite3.connect(self.store.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            result = build_query(db, self.replay_root, now, player)
        if result is None:
            return f"没有找到 {player} 的有效构筑（最近14天没有可用录像）。"
        inferred = result.get('inferred_side') or {}
        include_inferred = bool(inferred.get('cards')) and not inferred.get('conflict')
        ydk_url = link['url'].rstrip('/') + '/build-ydk' + ('?inferred=1' if include_inferred else '')
        if not inferred.get('cards'):
            ydk_note = ''
        elif include_inferred:
            ydk_note = '（含推测 Side）'
        else:
            ydk_note = '（推测 Side 有冲突，未并入）'
        from .statistics_local_cards import picture, wait_for
        from . import deck_image

        image_codes = [int(card.get('code') or 0) for card in
                       (result.get('cards') or []) + ((result.get('inferred_side') or {}).get('cards') or [])]
        wait_for(self.data_dir, image_codes, timeout=30)

        def load(code):
            found = picture(self.data_dir, code)
            return found[0] if found else None

        try:
            blob = deck_image.render(result, load)
        except Exception:
            return f"构筑图生成失败；YDK 可从这里下载：{ydk_url}"
        image_url = 'base64://' + base64.b64encode(blob).decode('ascii')
        selected = result.get('selected') or {}
        person = result.get('person') or {}
        recorded = selected.get('recorded_at')
        rank = self._top100_rank(player)
        rank_line = f"排名：#{rank}\n" if rank else ""
        text = (f"玩家：{player}\n"
                f"{rank_line}"
                f"卡组：{person.get('deck_type') or '未识别'}\n"
                f"观测小局：{format_time(recorded) if recorded else '时间未知'}\n"
                f"YDK：{ydk_url}{ydk_note}\n"
                f"链接最晚到期：{format_time(link['expires_at'])}（最长 {self.link_hours:g} 小时；构筑图见下）")
        reply = CardReply(text, image_url)
        self._deck_cache[cache_key] = (time.monotonic(), reply)
        while len(self._deck_cache) > 32:
            self._deck_cache.pop(next(iter(self._deck_cache)))
        return reply

    def _replay_text(self, user_id: str, player: str) -> str:
        self.index_player_replays(player)
        from .match_history import recover_sessions_from_prefix
        recover_sessions_from_prefix(self.store, self.replay_root, player)
        rows = [row for row in (self.visible_replays(user_id, player) or [])
                if safe_replay_file(self.replay_root, row['rel_path'])]
        subscribed = self.store.get_subscription(user_id, player) is not None
        reason = ("这是当前符合你订阅条件的录像目录。" if subscribed else
                  "未订阅，默认仅查看该玩家对手排名前100的录像；不会自动订阅或开启推送。")
        return self.directory_message(user_id, player, reason, rows)

    def _wait_public_url(self, timeout: float = 8.0) -> bool:
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
        if sub is None and for_delivery:
            return ""
        if not self._wait_public_url():
            return "" if for_delivery else "录像已保留在本机，公网分享暂未连接。请稍后再发送“录像 玩家名”查询。"
        if rows is None:
            rows = self.visible_replays(user_id, player) or []
        # 只把文件真的还在的录像算进消息和链接；清空 replays 后旧记录会自动落选。
        rows = [row for row in rows if safe_replay_file(self.replay_root, row['rel_path'])]
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
            f"对手条件：{'全部' if sub and sub['rank_top'] is None else '前'+str(sub['rank_top'] if sub else 100)}（按收录时排名，排名未知不计入限排名范围）\n"
            f"最近使用卡组：{deck_label}\n"
            f"收录范围：{format_time(link['range_start'])} 至 {format_time(link['created_at'])} 已保存的录像\n"
            f"共 {len(rows)} 个文件（仅提供最近14天保存的录像）。链接最晚到期：{format_time(link['expires_at'])}"
            f"（最长 {self.link_hours:g} 小时，不保证公网地址持续在线。重新查询不会延长这一条）\n"
            f"{link['url']}\n"
            f"直接下载 ZIP：{link['url'].rstrip('/')}/zip\n"
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

    def index_collector_data(self) -> None:
        """观战进程启动时补齐旧录像、会话和事实索引；QQ 进程不调用。

        旧版把每局开局时间当成收录时间：这里按落盘时间补齐，旧分享快照保持不变。
        """
        legacy = []
        for replay in self.store.legacy_replays():
            path = safe_replay_file(self.replay_root, replay["rel_path"])
            saved_at = path.stat().st_mtime if path is not None else replay["recorded_at"]
            legacy.append((float(saved_at), int(replay["id"])))
        if legacy:
            self.store.backfill_saved_times(legacy)
        from .match_history import recover_legacy_sessions
        recover_legacy_sessions(self.store, self.replay_root, self.deck_dir)
        self.index_existing_replays()
        self.sync_replay_facts()

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
        from .copy_retention import purge
        try:copies=purge(self.data_dir,now)
        except (OSError,ValueError,TypeError,KeyError):
            copies=dict(removed=0,blocked=['invalid_registry'])
        if copies['removed']:self.write_log(f"已删除{copies['removed']}份按原期限到期的数据备份，源码备份保留")
        if copies['blocked']:self.write_log('到期数据备份有变更或删除失败，已保留待人工核对')
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
        reference=self.root/'reference_decks.json'
        override=self.data_dir/'classification-rules.json'
        identity=self.data_dir/'card-equivalence.json'
        marker=tuple((p.stat().st_mtime_ns,p.stat().st_size) if p.exists() else None for p in (reference,override,identity))
        if reference.is_file() and (updated or marker!=self._classification_rules_signature):
            try:
                self.store.refresh_classifications(reference,override)
                self._classification_rules_signature=marker
            except (OSError,ValueError,TypeError,KeyError):
                self.write_log('构筑分类规则暂不可用，保留已有分类和未知状态')
        return updated

    async def cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.cleanup_old_files)
                from retention_policy import next_cleanup_delay
                delay = await asyncio.to_thread(next_cleanup_delay, float(self.clock()),
                                                self.replay_root, self.deck_dir,
                                                self.store.all_replays())
                from .copy_retention import next_delay
                delay=min(delay,await asyncio.to_thread(next_delay,self.data_dir,float(self.clock())))
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

    def accept_group_card_shortcut(self, event, text):
        """群里免@接裸数字/next：只在该群该用户当前有查卡候选时成立。"""
        value = (text or '').strip()
        if not ((value.isascii() and value.isdigit() and len(value) <= 4)
                or value.casefold() == 'next'):
            return False
        user = str(event.get('user_id') or '')
        group = str(event.get('group_id') or '')
        if not user or not group:
            return False
        return self.card_search.has_candidates('group:' + group + ':' + user, max_age=120)

    def accept_group_extra(self, event, text):
        return self.accept_group_guess(event, text) or self.accept_group_card_shortcut(event, text)

    async def on_qq_event(self, event: dict) -> None:
        if self.qq is None:
            return
        is_group = event.get("message_type") == "group"
        if is_group:
            if not self.group_commands:
                return
            text = group_command_text(event, self.group_without_at, self.accept_group_extra)
            if text is None:
                return
        else:
            text = event_text(event)
        user_id = event.get("user_id")
        receiver = event["group_id"] if is_group else user_id
        send = self.qq.send_group if is_group else self.qq.send_private
        forward = self.qq.send_group_forward if is_group else self.qq.send_private_forward
        conversation = "group:" + str(receiver) if is_group else ""
        parsed_command = parse_command(text)
        if parsed_command.get('op') in ('deck_build', 'top100_rank_build'):
            await send(receiver, "构筑图生成中，约几秒…")
            task = asyncio.create_task(self._deck_build_job(
                user_id, text, conversation, receiver, send))
            self._deck_tasks.add(task)
            task.add_done_callback(self._deck_tasks.discard)
            return
        if parsed_command.get('op') == 'price_quote':
            await send(receiver, "查价中，约 1 分钟；完成后把结果发到这里。")
            task = asyncio.create_task(self._price_quote_job(parsed_command, receiver, send))
            self._price_quote_tasks.add(task)
            task.add_done_callback(self._price_quote_tasks.discard)
            return
        if parsed_command.get('op') == 'help':
            # 帮助是一条分组清单，直接发文字，不再走合并转发。
            reply = await asyncio.to_thread(self.handle_text, user_id, text, conversation)
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
                    # 隧道进程归分享服务；QQ 进程只读同一份隧道状态，自己不再开第二条。
                    if "share" in self.roles:
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

    async def price_watch_loop(self) -> None:
        """每张预定单独错峰巡检：一次只查一张，查完再排下一轮。"""
        cfg = self.config.get("price_watch") or {}
        if cfg.get("enabled") is not True:
            return
        cli = str(cfg.get("cli") or "").strip()
        if not cli:
            self.write_log("price_watch: 未配置 cli，跳过")
            return
        import random
        low = max(60.0, float(cfg.get("interval_min_seconds", 1500)))
        high = max(low, float(cfg.get("interval_max_seconds", 2400)))
        timeout = max(30.0, float(cfg.get("timeout_seconds", 300)))
        retries = max(1, int(cfg.get("retries", 3)))
        retry_delay = max(5.0, float(cfg.get("retry_delay_seconds", 20)))
        delay = max(0.0, float(cfg.get("initial_delay_seconds", 120))) + random.uniform(0, 180)
        self._price_next_allowed = 0.0
        self._price_idle_logged = False
        self._price_ensure_schedules(low, high)
        self.write_log(f"price_watch: 已启用，按预定逐张错峰；每张 {low/60:.0f}-{high/60:.0f} 分钟随机，"
                       f"两张之间至少隔 {float(cfg.get('min_gap_seconds', 300))/60:.0f}-"
                       f"{float(cfg.get('min_gap_max_seconds', 600))/60:.0f} 分钟")
        await asyncio.sleep(delay)
        while True:
            try:
                due = self._price_due_reservation()
                if due is None:
                    if not self._price_idle_logged:
                        self.write_log("price_watch: 当前没有到点的预定，等待")
                        self._price_idle_logged = True
                    await asyncio.sleep(30)
                    continue
                self._price_idle_logged = False
                if time.monotonic() < self._price_next_allowed:
                    await asyncio.sleep(20)
                    continue
                self.write_log(f"price_watch: 检查预定 #{due['id']} {due['name']} {due['rarity']}".strip())
                await asyncio.to_thread(self._price_cycle_one, cfg, cli, timeout, retries, retry_delay, due)
                self._price_mark_checked(due["id"], low, high)
                self._price_next_allowed = time.monotonic() + random.uniform(
                    max(60.0, float(cfg.get("min_gap_seconds", 300))),
                    max(120.0, float(cfg.get("min_gap_max_seconds", 600))))
            except Exception as exc:
                self.write_log(f"price_watch 异常: {exc}")
                await asyncio.sleep(30)
            await asyncio.sleep(20)

    def _price_database_path(self):
        from .price_reservations import DEFAULT_DB
        if self._price_db:
            return Path(self._price_db)
        return Path(self._price_cfg().get("db") or DEFAULT_DB)

    def _price_schedule_conn(self):
        import sqlite3
        conn = sqlite3.connect(self._price_database_path(), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS reservation_schedule (
            reservation_id INTEGER PRIMARY KEY,
            next_check_at REAL NOT NULL,
            last_check_at REAL
        )""")
        conn.commit()
        return conn

    def _price_ensure_schedules(self, low, high):
        import random
        import sqlite3
        try:
            with closing(self._price_schedule_conn()) as conn:
                rows = conn.execute(
                    "SELECT r.id FROM reservations r LEFT JOIN reservation_schedule s "
                    "ON s.reservation_id=r.id WHERE r.active=1 AND s.reservation_id IS NULL").fetchall()
                now = time.time()
                for row in rows:
                    # 新预定随机摊到未来一个周期内，避免同时到点。
                    conn.execute("INSERT OR IGNORE INTO reservation_schedule (reservation_id, next_check_at) VALUES (?,?)",
                                 (int(row["id"]), now + random.uniform(0, high)))
                conn.commit()
        except (OSError, sqlite3.Error):
            pass

    def _price_due_reservation(self):
        import sqlite3
        try:
            with closing(self._price_schedule_conn()) as conn:
                row = conn.execute(
                    "SELECT r.id,r.name,r.rarity,r.language,s.next_check_at "
                    "FROM reservations r LEFT JOIN reservation_schedule s ON s.reservation_id=r.id "
                    "WHERE r.active=1 AND (s.next_check_at IS NULL OR s.next_check_at<=?) "
                    "ORDER BY COALESCE(s.next_check_at,0) LIMIT 1",
                    (time.time(),)).fetchone()
            return dict(row) if row else None
        except (OSError, sqlite3.Error):
            return None

    def _price_mark_checked(self, reservation_id, low, high):
        import random
        import sqlite3
        try:
            with closing(self._price_schedule_conn()) as conn:
                conn.execute(
                    "INSERT INTO reservation_schedule (reservation_id,next_check_at,last_check_at) "
                    "VALUES (?,?,?) ON CONFLICT(reservation_id) DO UPDATE SET "
                    "next_check_at=excluded.next_check_at,last_check_at=excluded.last_check_at",
                    (int(reservation_id), time.time() + random.uniform(low, high), time.time()))
                conn.commit()
        except (OSError, sqlite3.Error):
            pass

    def _top100_rank(self, player: str):
        from . import deck_naming
        try:
            with closing(deck_naming.connect(self.store.path, readonly=True)) as db:
                row = db.execute("SELECT rank FROM stat_current_top100 WHERE player=? AND rank<=100",
                                 (player,)).fetchone()
            return int(row["rank"]) if row else None
        except Exception:
            return None

    def _top100_rank_build_text(self, user_id, rank: int):
        from . import deck_naming
        with closing(deck_naming.connect(self.store.path, readonly=True)) as db:
            row = db.execute("SELECT player FROM stat_current_top100 WHERE rank=? AND rank<=100",
                             (int(rank),)).fetchone()
        if row is None:
            return f"前100里没有第 {rank} 名，榜单可能刚刷新，请稍后再试。"
        player = row["player"]
        reply = self._deck_build_text(user_id, player)
        if isinstance(reply, str) and self._top100_rank(player):
            return reply
        return reply

    def _top100_pie_reply(self):
        from .top100_pie import render
        blob, data = render(self.store.path, self.replay_root, self.clock())
        image_url = "base64://" + base64.b64encode(blob).decode("ascii")
        text = (f"前100卡组分布（有档案 {data['covered']}/{data['current']} 人，"
                f"已识别 {len(data['items'])} 类）")
        return CardReply(text, image_url)

    def _top100_deck_text(self, keyword: str) -> str:
        from . import deck_naming
        with closing(deck_naming.connect(self.store.path, readonly=True)) as db:
            roster = deck_naming.roster(db)
            views = {row["player"]: row for row in deck_naming.latest_views(db, self.clock(), self.replay_root)}
        wanted = keyword.strip().lower()
        hits = []
        for row in roster:
            deck = (views.get(row["player"]) or {}).get("deck_type") or "未识别"
            if wanted in str(deck).lower():
                hits.append((row["rank"], row["player"], deck))
        if not hits:
            return f"前100里没有卡组名包含「{keyword}」的玩家。"
        lines = [f"前100中卡组名包含「{keyword}」的玩家（{len(hits)} 人）："]
        for rank, player, deck in hits:
            lines.append(f"#{rank} {player} · {deck}")
        return "\n".join(lines)

    def _price_quote_blocking(self, command):
        from .price_quote import PriceQuote, format_quote
        from .price_shipping import ShippingCache
        if not self._price_ui_lock.acquire(timeout=180):
            return "前面还有查价在跑，请稍后再试。"
        try:
            result = PriceQuote(ShippingCache(self._price_database_path()), log=self.write_log).quote(
                command["name"], command.get("rarity") or "", command.get("language") or "日文")
            return format_quote(result)
        finally:
            self._price_ui_lock.release()

    async def _deck_build_job(self, user_id, text, conversation, receiver, send):
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(self.handle_text, user_id, text, conversation), timeout=40)
        except asyncio.TimeoutError:
            reply = "构筑生成超时，请稍后再试。"
        except Exception as exc:
            reply = f"构筑暂时失败：{exc}"
        try:
            await send(receiver, reply)
        except Exception as exc:
            self.write_log(f"构筑结果发送失败：{exc}")

    async def _price_quote_job(self, command, receiver, send):
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(self._price_quote_blocking, command), timeout=300)
        except asyncio.TimeoutError:
            reply = "查价超时：集换社页面没有响应，已复位，请稍后重试。"
            await asyncio.to_thread(self._reset_price_ui)
        except Exception as exc:
            from .price_quote import PriceQuoteError
            reply = (str(exc) if isinstance(exc, PriceQuoteError)
                     else "查价暂时不可用：集换社页面异常，已复位，请稍后再试。")
            self.write_log(f"查价异常: {type(exc).__name__}: {exc}")
        try:
            await send(receiver, reply)
        except Exception as exc:
            self.write_log(f"查价结果发送失败：{exc}")

    def _reset_price_ui(self):
        if not self._price_ui_lock.acquire(timeout=90):
            self.write_log("查价: reset 等待 UI 锁超时，跳过复位")
            return
        try:
            try:
                self._adb(["shell", "am", "force-stop", self._price_package()], timeout=20)
                self._adb(["shell", "uiautomator", "dump", "/sdcard/jhs_ui.xml"], timeout=10)
            except Exception:
                pass
        finally:
            self._price_ui_lock.release()

    def _price_cycle_one(self, cfg, cli, timeout, retries, retry_delay, due):
        if not self._price_ui_lock.acquire(timeout=10):
            self.write_log("price_watch: 设备正被查价占用，本轮跳过")
            return
        try:
            from .price_ui_lock import PriceUiLock
            with PriceUiLock(timeout=120):
                self._price_cycle_one_unlocked(cfg, cli, timeout, retries, retry_delay, due)
        finally:
            self._price_ui_lock.release()

    def _price_cycle_one_unlocked(self, cfg, cli, timeout, retries, retry_delay, due):
        if not self._ensure_price_emulator():
            self.write_log("price_watch: 模拟器/设备未就绪，本轮跳过，下一轮再试")
            return
        self._start_price_app()
        for attempt in range(1, retries + 1):
            started = datetime.now().astimezone().replace(microsecond=0)
            try:
                out, code = self._run_price_watch_once(cli, timeout, due["id"])
            except Exception as exc:
                out, code = str(exc), -1
            if code == 0 and self._price_snapshot_fresh(started):
                self.write_log(f"price_watch: 预定 #{due['id']} 第 {attempt} 次检查成功")
                return
            self.write_log(f"price_watch: 预定 #{due['id']} 第 {attempt}/{retries} 次未刷新（exit={code}）：{out[-300:]}")
            if attempt < retries:
                time.sleep(retry_delay)
        self.write_log(f"price_watch: 预定 #{due['id']} 本轮多次未刷新，等下一轮")

    def _price_cfg(self):
        return self.config.get("price_watch") or {}

    def _price_cli_dir(self):
        return Path(str(self._price_cfg().get("cli") or "")).resolve().parent

    def _price_adb(self):
        emu = self._price_cfg().get("emulator") or {}
        if emu.get("adb"):
            return str(emu["adb"])
        bundled = self._price_cli_dir() / "tools" / "platform-tools" / "adb.exe"
        return str(bundled) if bundled.is_file() else "adb"

    def _price_device(self):
        emu = self._price_cfg().get("emulator") or {}
        if emu.get("serial"):
            return str(emu["serial"])
        try:
            import json
            cfg = json.loads((self._price_cli_dir() / "config.json").read_text(encoding="utf-8"))
            return str((cfg.get("device") or {}).get("serial") or "127.0.0.1:16416")
        except (OSError, ValueError):
            return "127.0.0.1:16416"

    def _price_package(self):
        emu = self._price_cfg().get("emulator") or {}
        if emu.get("app_package"):
            return str(emu["app_package"])
        try:
            import json
            cfg = json.loads((self._price_cli_dir() / "config.json").read_text(encoding="utf-8"))
            return str((cfg.get("device") or {}).get("app_package") or "com.jihuanshe")
        except (OSError, ValueError):
            return "com.jihuanshe"

    def _price_instance(self, serial):
        emu = self._price_cfg().get("emulator") or {}
        if emu.get("instance"):
            return str(emu["instance"])
        manager = emu.get("manager") or ""
        if not manager or not Path(manager).is_file():
            return "1"
        try:
            import json
            proc = subprocess.run([manager, "info", "-v", "all"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30)
            raw = (proc.stdout or "").strip()
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            port = str(serial).rsplit(":", 1)[-1]
            for index, info in data.items():
                if str(info.get("adb_port")) == port:
                    return str(index)
        except Exception:
            pass
        return "1"

    def _adb(self, args, timeout=20):
        import subprocess as _sp
        return _sp.run([self._price_adb()] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)

    def _ensure_price_emulator(self):
        """设备不在线就通过 MuMuManager 拉起对应实例；返回是否就绪。"""
        import subprocess as _sp
        serial = self._price_device()
        out = (self._adb(["devices"]).stdout or "")
        if serial in out:
            self._adb(["connect", serial]) if ":" in serial else None
            return True
        emu = self._price_cfg().get("emulator") or {}
        manager = str(emu.get("manager") or "")
        if manager and Path(manager).is_file():
            instance = self._price_instance(serial)
            try:
                _sp.Popen([manager, "control", "-v", instance, "launch"],
                          stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                          creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
                self.write_log(f"price_watch: 已请求启动 MuMu 实例 {instance}")
            except OSError as exc:
                self.write_log(f"price_watch: 启动 MuMu 失败：{exc}")
        deadline = time.monotonic() + float(emu.get("wait_seconds", 90))
        while time.monotonic() < deadline:
            if ":" in serial:
                self._adb(["connect", serial], timeout=20)
            out = (self._adb(["devices"], timeout=20).stdout or "")
            if serial in out:
                return True
            time.sleep(3)
        return False

    def _start_price_app(self):
        serial, package = self._price_device(), self._price_package()
        try:
            self._adb(["-s", serial, "shell", "monkey", "-p", package, "-c",
                       "android.intent.category.LAUNCHER", "1"], timeout=30)
        except Exception as exc:
            self.write_log(f"price_watch: 打开集换社失败：{exc}")

    def _price_snapshot_fresh(self, since):
        from datetime import datetime
        from .price_reservations import DEFAULT_DB
        path = Path(self._price_cfg().get("db") or DEFAULT_DB)
        try:
            import sqlite3
            with closing(sqlite3.connect(path, timeout=5)) as conn:
                row = conn.execute("SELECT MAX(captured_at) FROM snapshots").fetchone()
            if not row or not row[0]:
                return False
            return datetime.fromisoformat(row[0]) >= since
        except Exception:
            return False

    def _price_cycle(self, cfg, cli, timeout, retries, retry_delay):
        if not self._ensure_price_emulator():
            self.write_log("price_watch: 模拟器/设备未就绪，本轮跳过，下一轮再试")
            return
        self._start_price_app()
        for attempt in range(1, retries + 1):
            started = datetime.now().astimezone().replace(microsecond=0)
            try:
                out, code = self._run_price_watch_once(cli, timeout)
            except Exception as exc:
                out, code = str(exc), -1
            if code == 0 and self._price_snapshot_fresh(started):
                self.write_log(f"price_watch: 第 {attempt} 次检查成功，价格已刷新")
                return
            self.write_log(f"price_watch: 第 {attempt}/{retries} 次未刷新（exit={code}）：{out[-300:]}")
            if attempt < retries:
                time.sleep(retry_delay)
        self.write_log("price_watch: 本轮多次未刷新，等待下一轮")

    def _run_price_watch_once(self, cli: str, timeout: float, reservation_id=None):
        import os
        import subprocess as _sp
        # jhs_watch 的汇总里有 "¥"，管道下 Windows 默认 GBK 会 UnicodeEncodeError；
        # 强制子进程用 UTF-8 输出，父进程也按 UTF-8 读。
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = _sp.run(
            [sys.executable, cli, "once"] + (["--reservation-id", str(reservation_id)] if reservation_id else []),
            cwd=str(Path(cli).resolve().parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        self.write_log("price_watch: " + (out[-800:] if out else f"exit={proc.returncode}"))
        return out, proc.returncode

    async def run(self, roles=None) -> None:
        # 多进程共用同一个 SQLite：这里不是单写者；M3 才把 QQ 的写请求收敛到 collector。
        active = self.roles if roles is None else normalize_roles(roles)
        if "share" in active and not self.share_external():
            self.share_server.start()
            if self.tunnel is not None:
                self.tunnel.origin = self.share.local_base()
        for line in self.startup_lines():
            print(line, flush=True)
            self.write_log(line)
        if "share" in active and not self.share_external():
            self.write_log(f"分享服务监听 http://{self.share.bind_host}:{self.share.bind_port}/")
        elif "share" in active and self.share_external():
            self.write_log("分享服务由独立 share_service 提供（share.mode=process），watch 不再监听 8787。")
        qq_cfg = self.config.get("qq") or {}
        token = str(qq_cfg.get("access_token") or "").strip()
        tasks = []
        # watch 存活状态：share.mode=process 后 8787 只代表分享进程，launcher/pycharm_link 改读这个文件。
        # 不单独起任务（否则 run() 的 gather 永远不结束）；启动写一次，之后跟着 on_capture_status 每 10 秒刷新。
        if "watch" in active:
            self.write_watch_status()
        # 只按角色裁剪；全开时任务创建顺序与原来完全一致。
        if "qq" in active:
            tasks.append(asyncio.create_task(self.notify_loop()))
        if "watch" in active:
            tasks.append(asyncio.create_task(self.cleanup_loop()))
        if "qq" in active:
            tasks.append(asyncio.create_task(self.guess_reveal_loop()))
            tasks.append(asyncio.create_task(self.napcat_watchdog()))
        if self._top100_enabled and "watch" in active:
            tasks.append(asyncio.create_task(self.top100_loop()))
        if "qq" in active:
            tasks.append(asyncio.create_task(self.price_watch_loop()))
        if "qq" in active:
            if token:
                def qq_status(message):
                    print(message, flush=True)
                    self.write_log(message)

                self.qq = OneBotClient(
                    str(qq_cfg.get("onebot_ws") or "ws://127.0.0.1:3001"), token,
                    self.on_qq_event, on_status=qq_status,
                    accept_group_messages=self.group_commands,
                    allow_unmentioned_commands=self.group_without_at,
                    group_extra_accept=self.accept_group_extra,
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
        if "watch" in active:
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
                        on_status=self.on_capture_status,
                    )
                )
            )
        try:
            if tasks:
                await asyncio.gather(*tasks)
            else:
                # 只开分享服务时没有后台任务，等窗口关闭或 Ctrl+C。
                await asyncio.Event().wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if "watch" in active:
                self.write_watch_status('stopped')
            self.share_server.stop()
            self.store.close()


def load_bot_config(path: Path) -> dict:
    import json

    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise RuntimeError("配置文件必须是一个 JSON 对象")
    return {key: value for key, value in raw.items() if not str(key).startswith("_")}
