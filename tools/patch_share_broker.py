# -*- coding: utf-8 -*-
"""W95：分享链接 broker（复用未过期链接、失败重试上限、错误显式化）。"""
import pathlib

p = pathlib.Path(r'D:\pythons\qq-replay-bot\bot\share.py')
t = p.read_text(encoding='utf-8')
t = t.replace('import sys\nimport threading\n', 'import sys\nimport threading\nimport time\n', 1)
needle = "\n\nclass ShareApp:"
helper = '''

class ShareLinkUnavailable(RuntimeError):
    """链接生成失败（数据库错误/重试上限），上层必须明确告诉用户，而不是发死链。"""
'''
assert t.count(needle) == 1
t = t.replace(needle, helper + needle, 1)
old = """        self.public_url_provider = None
        self.statistics_portal = None
        # /s/<token>/analysis-refresh 鏄?GET 瑙﹀彂鍐欙紙閲嶈В鏋?+ sidecar锛夛紝鐙珛 share 杩涚▼蹇呴』鍏虫帀瀹冦€?        self.allow_analysis_refresh = True"""
new = """        self.public_url_provider = None
        self.statistics_portal = None
        # 同一分享范围复用未过期链接；失败重试有上限，避免发死链。
        self.link_reuse_margin_seconds = 600.0
        self.link_attempts = 3
        self._link_lock = threading.Lock()
        # /s/<token>/analysis-refresh 鏄?GET 瑙﹀彂鍐欙紙閲嶈В鏋?+ sidecar锛夛紝鐙珛 share 杩涚▼蹇呴』鍏虫帀瀹冦€?        self.allow_analysis_refresh = True"""
assert t.count(old) == 1
t = t.replace(old, new, 1)
old = """    def create_link(self, user_id: str, player: str, replay_ids: list[int], range_start: float,
                    *, deck_replay_ids: Optional[list[int]] = None) -> dict:
        now = float(self.clock())
        token = secrets.token_urlsafe(32)
        expires = now + self.link_hours * 3600
        self.store.insert_link(token, user_id, player, now, expires, list(replay_ids), range_start, deck_replay_ids)
        url, public = self.url_for(token)
        return {
            "token": token,
            "url": url,
            "public": public,
            "created_at": now,
            "expires_at": expires,
            "range_start": float(range_start),
            "replay_ids": list(replay_ids),
        }

    def create_stats_link(self, user_id: str, hours: float = 2) -> dict:
        \"\"\"只读统计页的临时入口；与录像链接分开表和有效期。\"\"\"
        now = float(self.clock())
        token = secrets.token_urlsafe(32)
        expires = now + max(0.1, float(hours)) * 3600
        self.store.insert_stats_link(token, user_id, now, expires)
        try:
            self.store.purge_stats_links(now)
        except Exception:
            pass
        public = self.public_base()
        base = public or self.local_base()
        return {"token": token, "url": base + "/stats/" + token,
                "public": bool(public), "created_at": now, "expires_at": expires}"""
new = """    def _link_payload(self, link: dict) -> dict:
        url, public = self.url_for(link['token'])
        return {
            "token": link['token'], "url": url, "public": public,
            "created_at": link['created_at'], "expires_at": link['expires_at'],
            "range_start": float(link['range_start']), "replay_ids": list(link['replay_ids']),
            "reused": bool(link.get('reused')),
        }

    def create_link(self, user_id: str, player: str, replay_ids: list[int], range_start: float,
                    *, deck_replay_ids: Optional[list[int]] = None) -> dict:
        ids = list(replay_ids)
        deck_ids = list(ids if deck_replay_ids is None else deck_replay_ids)
        now = float(self.clock())
        with self._link_lock:
            reusable = self.store.find_reusable_link(
                user_id, player, ids, float(range_start), deck_ids,
                now + self.link_reuse_margin_seconds)
            if reusable:
                reusable['reused'] = True
                return self._link_payload(reusable)
            last_error = None
            for attempt in range(self.link_attempts):
                token = secrets.token_urlsafe(32)
                expires = now + self.link_hours * 3600
                try:
                    self.store.insert_link(token, user_id, player, now, expires, ids, range_start, deck_ids)
                    return self._link_payload({
                        "token": token, "created_at": now, "expires_at": expires,
                        "range_start": range_start, "replay_ids": ids, "reused": False})
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < self.link_attempts:
                        time.sleep(0.05 * (attempt + 1))
            raise ShareLinkUnavailable('分享链接生成失败，请稍后再试。') from last_error

    def create_stats_link(self, user_id: str, hours: float = 2) -> dict:
        \"\"\"只读统计页的临时入口；与录像链接分开表和有效期。\"\"\"
        now = float(self.clock())
        with self._link_lock:
            reusable = self.store.find_reusable_stats_link(user_id, now + self.link_reuse_margin_seconds)
            if reusable:
                public = self.public_base()
                base = public or self.local_base()
                return {"token": reusable['token'], "url": base + "/stats/" + reusable['token'],
                        "public": bool(public), "created_at": reusable['created_at'],
                        "expires_at": reusable['expires_at'], "reused": True}
            last_error = None
            for attempt in range(self.link_attempts):
                token = secrets.token_urlsafe(32)
                expires = now + max(0.1, float(hours)) * 3600
                try:
                    self.store.insert_stats_link(token, user_id, now, expires)
                    self.store.purge_stats_links(now)
                    public = self.public_base()
                    base = public or self.local_base()
                    return {"token": token, "url": base + "/stats/" + token,
                            "public": bool(public), "created_at": now, "expires_at": expires,
                            "reused": False}
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < self.link_attempts:
                        time.sleep(0.05 * (attempt + 1))
            raise ShareLinkUnavailable('统计链接生成失败，请稍后再试。') from last_error"""
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t, encoding='utf-8', newline='')
print('patched share broker')
