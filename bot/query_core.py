# -*- coding: utf-8 -*-
"""查询平台数据层（T7，交接文档 v5）。

架构决策：/query-api/* 的统计数据不复制统计门户的查询与缓存体系，而是经
回环调用统计门户（127.0.0.1:8788）的 JSON API——roster 预热、300s 缓存、
重放分析批次复用等机制全部复用，数据与网页端天然一致；门户不可用时统一
按 PortalUnavailable 处理（外层转 503）。

门户鉴权：用 data/statistics-owner.key 的 owner secret 走
/statistics/access/<secret> 换 owner cookie（与 statistics_proxy 同一模式），
cookie 约 5 小时有效，过期自动重取一次再重试。门户 _route() 拒绝
Forwarded 类头与非常见 Host，因此这里不发任何转发头。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path


class PortalUnavailable(Exception):
    """统计门户不可达/拒绝/响应异常。"""


class PortalBusy(PortalUnavailable):
    """统计门户正在计算（可重试）；retry_after 来自门户或默认 3 秒。"""

    def __init__(self, retry_after: int = 3):
        super().__init__("portal_busy")
        self.retry_after = int(retry_after)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class PortalClient:
    """回环访问统计门户的 JSON API。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8788", secret_path=None,
                 timeout: float = 8.0, clock=time.time):
        self.base = str(base_url or "http://127.0.0.1:8788").rstrip("/")
        self.secret_path = Path(secret_path) if secret_path else None
        self.timeout = float(timeout)
        self.clock = clock
        self._cookie = ("", 0.0)

    def _session_cookie(self, force: bool = False) -> str:
        from .statistics_portal import COOKIE, owner_secret
        value, at = self._cookie
        if not force and value and self.clock() - at < 4 * 3600:
            return value
        if self.secret_path is None:
            raise PortalUnavailable("portal_secret_missing")
        url = f"{self.base}/statistics/access/{owner_secret(self.secret_path)}"
        request = urllib.request.Request(url)
        try:
            _OPENER.open(request, timeout=self.timeout)
            raise PortalUnavailable("portal_access_no_redirect")
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307):
                raise PortalUnavailable(f"portal_access_{exc.code}") from exc
            set_cookie = exc.headers.get("Set-Cookie") or ""
            exc.close()
        except (urllib.error.URLError, OSError) as exc:
            raise PortalUnavailable("portal_unreachable") from exc
        value = set_cookie.split(";", 1)[0].strip()
        if not value.startswith(COOKIE + "=") or len(value) <= len(COOKIE) + 1:
            raise PortalUnavailable("portal_cookie_invalid")
        self._cookie = (value, self.clock())
        return value

    def get_json(self, path: str, params: dict | None = None):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        url = f"{self.base}{path}{query}"
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                request = urllib.request.Request(
                    url, headers={"Cookie": self._session_cookie(force=attempt == 2)})
                with _OPENER.open(request, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except PortalUnavailable:
                raise
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read(4096)
                except OSError:
                    pass
                finally:
                    exc.close()
                if exc.code == 503 and body:
                    try:
                        doc = json.loads(body.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        doc = None
                    if isinstance(doc, dict) and doc.get("code") == "query_busy":
                        try:
                            retry = int(doc.get("retry_after") or 3)
                        except (TypeError, ValueError):
                            retry = 3
                        raise PortalBusy(max(2, min(retry, 5)))
                if exc.code == 403 and attempt == 1:
                    last = exc
                    continue  # cookie 失效：强制重取一次再试
                raise PortalUnavailable(f"portal_http_{exc.code}") from exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise PortalUnavailable("portal_unreachable") from exc
        raise PortalUnavailable("portal_session") from last


class QueryCore:
    """对 /query-api/* 的数据服务：门户透传 + 简单 TTL 缓存（计算放锁外）。

    store/share/replay_root/database 可选注入：提供后解锁 P3 的饼图与 /s/ 链接。"""

    def __init__(self, portal: PortalClient, clock=time.monotonic,
                 store=None, share=None, replay_root=None, database=None, data_dir=None):
        self.portal = portal
        self.clock = clock
        self.store = store
        self.share = share
        self.replay_root = Path(replay_root) if replay_root else None
        self.database = Path(database) if database else None
        self.data_dir = Path(data_dir) if data_dir else (self.database.parent if self.database else None)
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._owned_store = False
        self._cache_bytes = 0
        self._inflight: OrderedDict = OrderedDict()
        self._negative: OrderedDict = OrderedDict()

    def close(self):
        if self._owned_store and self.store is not None:
            self.store.close()
            self._owned_store = False

    def _cached(self, key: tuple, ttl: float, fn):
        # 按 key 单飞：锁只保护字典，门户请求在锁外；不同 key 可并行。
        now = self.clock()
        with self._lock:
            neg = self._negative.get(key)
            if neg and neg[0] > now:
                raise PortalBusy(neg[1])
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                self._cache.move_to_end(key)
                return hit[1]
            if key in self._inflight:
                raise PortalBusy(2)
            self._inflight[key] = now
            while len(self._inflight) > 64:
                self._inflight.popitem(last=False)
        try:
            value = fn()
        except PortalBusy as exc:
            with self._lock:
                self._negative[key] = (self.clock() + 2.5,
                                       max(2, min(int(getattr(exc, "retry_after", 3) or 3), 5)))
                while len(self._negative) > 64:
                    self._negative.popitem(last=False)
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        with self._lock:
            old = self._cache.pop(key, None)
            if old:
                self._cache_bytes -= old[2]
            if size <= 4 * 1024 * 1024:
                self._cache[key] = (self.clock() + ttl, value, size)
                self._cache_bytes += size
                while len(self._cache) > 32 or self._cache_bytes > 8 * 1024 * 1024:
                    _, expired = self._cache.popitem(last=False)
                    self._cache_bytes -= expired[2]
        return value

    def top100(self):
        # 门户对 roster 有 300s 缓存 + 后台预热；这里再兜一层，避免门户重启后的冷查询打到用户。
        return self._cached(("roster",), 300.0, lambda: self.portal.get_json("/statistics/api/roster"))

    def player_history(self, name: str):
        return self._cached(("history", name), 60.0,
                            lambda: self.portal.get_json("/statistics/api/player-history", {"player": name}))

    def player_pt(self, name: str):
        return self._cached(("pt", name), 60.0,
                            lambda: self.portal.get_json("/statistics/api/pt-history", {"player": name}))

    def player_decks(self, name: str):
        latest = 0.0
        if self.store is not None:
            try:
                rows = self.store.replays_for_player(name) or []
                latest = max((float(row.get("recorded_at") or 0) for row in rows), default=0.0)
            except Exception:
                latest = 0.0
        if self.data_dir is not None:
            from .query_precompute import MISS, read_precomputed
            hit = read_precomputed(self.data_dir, name, latest)
            if hit is not MISS:
                return hit
        return self._cached(("decks", name), 300.0,
                            lambda: self.portal.get_json("/statistics/api/build", {"player": name}))

    def player_replays(self, name: str, identity: str = ""):
        """P3：有 share 时返回 {items, link}（/s/ 分享链接，与 QQ「录像」同机制）；
        否则退回门户 recordings 透传（无链接）。"""
        if self.share is not None and self.store is not None and self.replay_root is not None:
            return self._cached(("replay-links", identity, name), 30.0,
                                lambda: self._replay_links(identity or "query", name))
        return self._cached(("replays", name), 30.0,
                            lambda: self.portal.get_json("/statistics/api/recordings", {"player": name}))

    def _replay_links(self, identity: str, player: str) -> dict:
        from .share import safe_replay_file
        cutoff = time.time() - 14 * 86400
        rows = [row for row in (self.store.replays_for_player(player) or [])
                if float(row.get("recorded_at") or 0) >= cutoff
                and safe_replay_file(self.replay_root, row["rel_path"])][-1000:]
        items = [dict(id=row.get("id"), name=Path(row["rel_path"]).name,
                      recorded_at=row.get("recorded_at")) for row in rows]
        if not rows:
            return dict(ok=True, items=items, link=None)
        # range_start 与 QQ 端一致：最近 14 天窗口（REPLAY_WINDOW_SECONDS）。
        link = self.share.create_link(identity, player, [row.get("id") for row in rows],
                                      cutoff)
        if not link.get("public"):
            raise PortalUnavailable("share_unavailable")
        for item in items:
            item["url"] = f"{link['url']}/file/{item['id']}"
        return dict(ok=True, items=items,
                    link=dict(url=link.get("url"), expires_at=link.get("expires_at"),
                              public=link.get("public")))

    def pie(self):
        """P3：前百卡组分布（top100_pie.collect 纯数据层；OCG 饼图无现成数据源，见 v5 默认决策 #2）。"""
        if self.database is None or self.replay_root is None:
            raise PortalUnavailable("pie_unavailable")
        def result():
            data = self.top100()
            items = [dict(name=str(row['name']), count=int(row['players']))
                     for row in data.get('deck_distribution', []) if int(row.get('players', 0)) > 0]
            return dict(items=items, pie=data.get('deck_pie'),
                        covered=int(data.get('covered_players', 0)),
                        current=int(data.get('current_players', 0)),
                        unidentified=int(data.get('unidentified_players', 0)))
        return self._cached(("pie",), 300.0, result)

    # ---- P4：订阅（写操作，双闸：账号 writes + 全局 writes_enabled） ------
    def subscription_list(self, identity: str) -> dict:
        if self.store is None:
            raise PortalUnavailable("store_unavailable")
        subs = self.store.list_subscriptions(identity)
        return dict(items=[dict(player=s["player"], rank_top=s.get("rank_top"),
                                notify=bool(s.get("notify"))) for s in subs])

    def subscribe(self, identity: str, player: str, rank_top=None, notify: bool = False) -> dict:
        """与 QQ「订阅」同表同语义（rank_top None=全部；notify False=静默）。
        调用方负责双闸校验（watch_api 层）。"""
        if self.store is None:
            raise PortalUnavailable("store_unavailable")
        player = str(player or "").strip()
        if not player or len(player) > 128:
            raise ValueError("bad_player")
        if rank_top is not None and (type(rank_top) is not int or not 1 <= rank_top <= 100):
            raise ValueError("bad_rank_top")
        row = self.store.upsert_subscription(identity, player, rank_top, bool(notify), time.time())
        return dict(player=row["player"], rank_top=row.get("rank_top"), notify=bool(row.get("notify")))

    def unsubscribe(self, identity: str, player: str) -> dict:
        if self.store is None:
            raise PortalUnavailable("store_unavailable")
        player = str(player or "").strip()
        if not player or len(player) > 128:
            raise ValueError("bad_player")
        ok = self.store.delete_subscription(identity, player)
        return dict(removed=bool(ok), player=player)

    # ---- P2：搜索与单卡 --------------------------------------------------
    def search_players(self, q: str):
        return self._cached(("psearch", q), 60.0,
                            lambda: self.portal.get_json("/statistics/api/player-search", {"q": q}))

    def search_cards(self, q: str):
        """卡名/卡号检索（门户 card-search，含本地卡库兜底）。
        「卡组搜索」也走这里：卡名 -> code -> card-metrics 的 by_deck 给出携带构筑分布。"""
        return self._cached(("csearch", q), 60.0,
                            lambda: self.portal.get_json("/statistics/api/card-search", {"q": q}))

    def search_decks(self, q: str):
        data = self.top100()
        key = q.casefold()
        items = [dict(row) for row in data.get('deck_distribution', [])
                 if key in str(row.get('name', '')).casefold()]
        return dict(items=items[:30], limited=len(items) > 30)

    def card_details(self, code: int):
        return self._cached(("cdetail", code), 600.0,
                            lambda: self.portal.get_json("/statistics/api/card-info", {"code": code}))

    def card_metrics(self, code: int, partition: str = "main"):
        """携带率/胜率（card_statistics 透传：metrics + by_deck 构筑分布）。"""
        return self._cached(("cmetrics", code, partition), 300.0,
                            lambda: self.portal.get_json("/statistics/api/card-metrics",
                                                         {"code": code, "partition": partition}))


def _admin_call(base_url: str, admin_token: str, action: str, identity: str,
                timeout: float = 4.0) -> dict:
    """回环调 collector 管理接口。

    返回 {ok, code, code_expires_at, identity} 或 {ok:False, error, status}。
    激活码只出现在本返回值里，调用方负责只通过 QQ 私聊展示、不写日志。
    """
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/query-api/admin/{action}",
        data=json.dumps({"identity": str(identity or "")}).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Token": str(admin_token or "")})
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return dict(ok=False, error=f"http_{exc.code}", status=exc.code)
    except (urllib.error.URLError, OSError):
        return dict(ok=False, error="query_service_unavailable", status=503)


def admin_issue(base_url: str, admin_token: str, identity: str, timeout: float = 4.0) -> dict:
    """QQ「开通查询」首次开通：建号并取一次性激活码；已有账号返回 already_provisioned。"""
    return _admin_call(base_url, admin_token, "issue", identity, timeout)


def admin_reissue(base_url: str, admin_token: str, identity: str, timeout: float = 4.0) -> dict:
    """QQ「开通查询」自助重发：旧待用码作废、发新一次性码；不动已有令牌。
    账号不存在或已停用返回 not_found（停用账号不能靠自助恢复）。"""
    return _admin_call(base_url, admin_token, "reissue", identity, timeout)
