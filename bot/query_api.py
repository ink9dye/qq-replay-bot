"""Mobile query routes, separate from watch authorization and task lifecycle."""
import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
import time
from urllib.parse import parse_qs, unquote
from .query_core import (PortalBusy, PortalUnavailable, QueryRequestError,
                         query_options, query_player)

class QueryHandlerMixin:
    ADMIN_UI_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>查询平台管理</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui;max-width:760px;margin:24px auto;padding:0 12px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;text-align:left}
button{margin:2px;padding:2px 8px}.lock{color:#b00}</style></head><body>
<h2>查询平台管理</h2>
<p>访问令牌：<input id="tok" type="password" size="24"> <button onclick="load()">载入</button></p>
<div id="out">输入 admin_token 后载入。</div>
<script>
const H=()=>({'X-Admin-Token':document.getElementById('tok').value,'Content-Type':'application/json'});
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){
 const r=await fetch('/query-api/admin/list',{headers:H()}).then(r=>r.json()).catch(e=>({error:String(e)}));
 if(!r.users){document.getElementById('out').textContent='载入失败：'+(r.error||'无数据');return}
 let h='<h3>账号</h3><table><tr><th>身份</th><th>写权限</th><th>待用码</th><th>设备</th><th>操作</th></tr>';
 for(const u of r.users){
  h+=`<tr><td>${esc(u.identity)}</td><td>${u.writes?'开':'关'}</td><td>${u.pending_code?'有':'—'}</td><td>${
   (u.devices||[]).map(d=>d.token_id).join(', ')||'—'}</td><td>
   <button onclick="writes('${esc(u.identity)}',${!u.writes})">写权限${u.writes?'关':'开'}</button>
   <button onclick="reset('${esc(u.identity)}')">重置</button>
   <button onclick="reissue('${esc(u.identity)}')">重发码</button>
   <button onclick="disable('${esc(u.identity)}')">注销</button></td></tr>`;
  for(const d of (u.devices||[])) h+=`<tr><td colspan=4>设备 ${esc(d.token_id)} · ${new Date(d.issued_at*1000).toLocaleString()}</td><td><button onclick="revoke('${esc(d.token_id)}')">吊销</button></td></tr>`;
 }
 h+='</table>';
 if(r.locks&&r.locks.length){h+='<h3 class="lock">锁定</h3><table>';
  for(const l of r.locks)h+=`<tr><td>${esc(l.key)}</td><td>${l.count} 次</td><td><button data-lock="${esc(l.key)}" onclick="unlock(this.dataset.lock)">解锁</button></td></tr>`;
  h+='</table>';}
 document.getElementById('out').innerHTML=h;
}
async function act(path,body){const r=await fetch('/query-api/admin/'+path,{method:'POST',headers:H(),body:JSON.stringify(body)}).then(r=>r.json()).catch(e=>({error:String(e)}));alert(r.ok?(r.code?'新激活码（仅显示一次）：'+r.code:'完成'):'失败：'+(r.error||r.status));load()}
const writes=(i,on)=>act('writes',{identity:i,enabled:on});
const reset=i=>{if(confirm('作废该账号全部令牌并发新码？'))act('reset',{identity:i})};
const reissue=i=>act('reissue',{identity:i});
const disable=i=>{if(confirm('注销该账号？'))act('disable',{identity:i})};
const revoke=t=>act('revoke-device',{token_id:t});
const unlock=k=>act('unlock',{key:k});
</script></body></html>"""

    def _client_ip(self) -> str:
        peer = self.client_address[0] if self.client_address else ""
        value = (self.headers.get("X-Real-IP") or peer) if peer in ("127.0.0.1", "::1") else peer
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError:
            return peer

    def _query_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("bad_json")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 65536:
            raise ValueError("bad_json")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("bad_json")
        return data

    _HEAVY_PREFIXES = ("/query-api/player/", "/query-api/card/")

    @classmethod
    def _is_heavy(cls, path: str) -> bool:
        return path.startswith(cls._HEAVY_PREFIXES) or path == "/query-api/command"

    @staticmethod
    def _is_query_extension(path):
        return (path in ('/query-api/binding', '/query-api/binding/unbind')
                or bool(re.fullmatch(r'/query-api/player/[^/]+/side-plan', path))
                or bool(re.fullmatch(r'/query-api/card/[^/]+/(usage|flow|winrate)', path)))

    def _query_dispatch(self, parts, post=False):
        self.close_connection = True
        self.connection.settimeout(10)
        if getattr(self.server, "query_auth", None) is None:
            self._send(503, dict(ok=False, error="query_auth_disabled"))
            return
        extension = self._is_query_extension(parts.path)
        if extension and self._query_identity() is None:
            return
        heavy = self._is_heavy(parts.path)
        light_gate = getattr(self.server, "query_light_gate", None) or getattr(self.server, "query_gate", None)
        heavy_gate = getattr(self.server, "query_heavy_gate", None) or light_gate
        gate = heavy_gate if heavy else light_gate
        wait = float(getattr(self.server, "query_heavy_wait" if heavy else "query_light_wait", 0.0) or 0.0)
        if gate is None or not gate.acquire(timeout=wait):
            retry = 3 if heavy else 1
            self._send(429, dict(ok=False, error="busy" if extension else "query_busy", retry_after=retry),
                       headers={"Retry-After": str(retry)})
            return
        try:
            try:
                return self._query_post(parts) if post else self._query_get(parts)
            except QueryRequestError as exc:
                headers = {'Retry-After': str(exc.retry_after)} if exc.retry_after else None
                self._send(exc.status, dict(ok=False, error=exc.error), headers=headers)
            except PortalBusy as exc:
                retry = max(2, min(int(getattr(exc, "retry_after", 3) or 3), 5))
                self._send(429 if extension else 503, dict(ok=False, error="busy" if extension else "query_busy", retry_after=retry),
                           headers={"Retry-After": str(retry)})
            except (ValueError, TypeError, UnicodeError):
                self._send(400, dict(ok=False, error="invalid_query_request"))
            except (PortalUnavailable, sqlite3.Error, OSError, TimeoutError):
                self._send(503, dict(ok=False, error="query_unavailable"))
        finally:
            gate.release()

    def _query_identity(self):
        """校验用户 access 令牌；失败发送 401 并返回 None。"""
        qa = getattr(self.server, "query_auth", None)
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        info = qa.authenticate(token) if (qa and token) else None
        if info is None:
            self._send(401, dict(ok=False, error="unauthorized"))
        return info

    def _admin_transport_ok(self):
        return (self.client_address and self.client_address[0] in ("127.0.0.1", "::1")
                and not any(self.headers.get(h) is not None
                            for h in ("X-Real-IP", "X-Forwarded-For", "Forwarded")))

    def _admin_ok(self) -> bool:
        admin_token = getattr(self.server, "admin_token", "")
        if not admin_token or not self._admin_transport_ok():
            return False
        header = self.headers.get("Authorization") or ""
        got = header[7:].strip() if header.lower().startswith("bearer ") else (self.headers.get("X-Admin-Token") or "").strip()
        return bool(got) and hmac.compare_digest(got, admin_token)

    def _query_get(self, parts) -> None:
        path = parts.path
        if path == "/query-api/status":
            info = self._query_identity()
            if info is not None:
                self._send(200, dict(ok=True, **info))
            return
        if path == "/query-api/me":
            info = self._query_identity()
            if info is not None:
                self._send(200, dict(ok=True, **info))
            return
        if path == "/query-api/admin/list":
            if not self._admin_ok():
                self._send(401, dict(ok=False, error="unauthorized"))
                return
            qa = self.server.query_auth
            self._send(200, dict(ok=True, users=qa.list_users(), locks=qa.list_locks()))
            return
        if path == "/query-api/admin/ui":
            if not self._admin_transport_ok():
                self._send(401, dict(ok=False, error="unauthorized"))
                return
            # 页面本身不含机密；数据请求都要 X-Admin-Token。公网由 nginx 404 拦截。
            html = self.ADMIN_UI_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return
        core = getattr(self.server, "query_core", None)
        if core is None:
            self._send(503, dict(ok=False, error="query_disabled"))
            return
        if self._query_extension_get(parts, core):
            return
        if path == "/query-api/top100":
            if self._query_identity() is not None:
                self._passthrough(core.top100)
            return
        if path == "/query-api/charts/pie":
            if self._query_identity() is not None:
                self._passthrough(core.pie)
            return
        if path == "/query-api/subscriptions":
            info = self._query_identity()
            if info is None:
                return
            try:
                self._passthrough(lambda: core.subscription_list(info["identity"]))
            except ValueError as exc:
                self._send(400, dict(ok=False, error=str(exc)))
            return
        if path == "/query-api/search":
            if self._query_identity() is None:
                return
            args = parse_qs(parts.query)
            stype = (args.get("type") or ["player"])[0]
            q = (args.get("q") or [""])[0].strip()
            if not q or len(q) > 80 or stype not in ("player", "card", "deck"):
                self._send(400, dict(ok=False, error="bad_query"))
                return
            fn = {"player": core.search_players, "card": core.search_cards, "deck": core.search_decks}[stype]
            self._passthrough(lambda: fn(q))
            return
        seg = path.split("/")
        # 认证下载：旧安卓 QueryApi.downloadBytes() 会带 Bearer 令牌请求这个 URL。
        # 取代公开 /s/ 分享链接，不再依赖 share.public_base_url，也不写 share_links。
        if len(seg) == 4 and seg[2] == "replay":
            code = seg[3]
            if not code.isdigit() or not 0 < int(code) <= 2 ** 62:
                self._send(400, dict(ok=False, error="bad_replay_id"))
                return
            # 新安卓“复制链接/浏览器打开”不会带 Bearer，只认 URL 里的短期签名；
            # 查询 API 下载仍可用 Bearer。两种任一通过即可。
            if not self._replay_signed_ok(int(code), parts.query):
                if self._query_identity() is None:
                    return
            found = core.replay_file(int(code))
            if found is None:
                self._send(404, dict(ok=False, error="replay_missing"))
                return
            file_path, _row = found
            try:
                data = file_path.read_bytes()
            except OSError:
                self._send(404, dict(ok=False, error="replay_missing"))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if len(seg) == 4 and seg[2] == "card":
            code = seg[3]
            if not code.isdigit() or not 0 < int(code) <= 4294967295:
                self._send(400, dict(ok=False, error="bad_code"))
                return
            if self._query_identity() is None:
                return

            def card_pack():
                details = core.card_details(int(code))
                try:
                    metrics = core.card_metrics(int(code))
                except PortalUnavailable:
                    metrics = None  # 统计层缺失（该卡无人携带等），效果照常展示
                return dict(details=details, metrics=metrics)

            self._passthrough(card_pack)
            return
        if len(seg) == 5 and seg[2] == "player":
            name = unquote(seg[3]).strip()
            if not name or len(name) > 100 or "/" in name or "\\" in name:
                self._send(400, dict(ok=False, error="bad_player"))
                return
            info = self._query_identity()
            if info is None:
                return
            fn = {"history": core.player_history, "pt": core.player_pt,
                  "decks": core.player_decks, "replays": core.player_replays}.get(seg[4])
            if fn is None:
                self._send(404, dict(ok=False, error="not_found"))
                return
            if seg[4] == "replays":
                # 列表项的下载 URL 转成绝对地址：新版安卓会直接复制/浏览器打开 item.url，
                # 相对路径会变成无效链接；downloadBytes 对两种形式都兼容。
                try:
                    data = core.player_replays(name, info["identity"])
                except PortalUnavailable:
                    self._send(503, dict(ok=False, error="query_unavailable"))
                    return
                self._send(200, dict(ok=True, data=self._absolute_replay_urls(data)))
                return
            self._passthrough(lambda: fn(name))
            return
        self._send(404, dict(ok=False, error="not_found"))

    def _query_extension_get(self, parts, core):
        path = parts.path
        if not self._is_query_extension(path):
            return False
        info = self._query_identity()
        if info is None:
            return True
        if path == '/query-api/binding':
            if parts.query:
                raise QueryRequestError('bad_param')
            data = core.binding_get(info['identity'])
        elif path.startswith('/query-api/card/'):
            seg = path.split('/')
            if not re.fullmatch(r'[0-9]{1,10}', seg[3]) or not 1 <= int(seg[3]) <= 4294967295:
                raise QueryRequestError('bad_code')
            data = core.card_stat(seg[4], int(seg[3]), **query_options(parts.query, seg[4]))
        elif path.endswith('/side-plan'):
            encoded = path.split('/')[3]
            if re.search(r'%(?![0-9a-fA-F]{2})', encoded):
                raise QueryRequestError('bad_player')
            try:
                name = query_player(unquote(encoded, errors='strict'))
            except UnicodeError:
                raise QueryRequestError('bad_player')
            data = core.side_plan(name, **query_options(parts.query, 'side-plan'))
        else:
            self._send(404, dict(ok=False, error='not_found'))
            return True
        self._send(200, dict(ok=True, data=data))
        return True

    def _replay_secret(self) -> str:
        app = self.server
        return str(getattr(app, "token", "") or getattr(app, "admin_token", "") or "")

    def _signed_replay_url(self, base: str, replay_id) -> str:
        try:
            rid = int(replay_id)
        except (TypeError, ValueError):
            return f"{base}/query-api/replay/{replay_id}"
        secret = self._replay_secret()
        if not secret:
            return f"{base}/query-api/replay/{rid}"
        exp = int(time.time()) + 5 * 3600  # 与旧分享链接默认 5h 对齐
        msg = f"{rid}.{exp}".encode("ascii")
        sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        return f"{base}/query-api/replay/{rid}?exp={exp}&sig={sig}"

    def _replay_signed_ok(self, replay_id: int, query: str) -> bool:
        secret = self._replay_secret()
        if not secret:
            return False
        args = parse_qs(query)
        exp = (args.get("exp") or [""])[0]
        sig = (args.get("sig") or [""])[0]
        if not exp.isdigit() or not sig or int(exp) < int(time.time()):
            return False
        msg = f"{int(replay_id)}.{int(exp)}".encode("ascii")
        expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def _query_base_url(self) -> str:
        host = str(self.headers.get("Host") or "").strip()
        if not host:
            return ""
        proto = str(self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        if proto not in ("http", "https"):
            bare = host.rsplit(":", 1)[0].strip("[]").lower()
            proto = "http" if bare in ("127.0.0.1", "localhost", "::1") else "https"
        return f"{proto}://{host}".rstrip("/")

    def _absolute_replay_urls(self, data):
        if not isinstance(data, dict):
            return data
        base = self._query_base_url()
        if not base:
            return data
        # 不修改 QueryCore 的 30s 缓存对象：不同 Host（直连/域名）不能互相污染。
        out = dict(data)
        out["items"] = [dict(item) if isinstance(item, dict) else item
                        for item in (data.get("items") or [])]
        for item in out["items"]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if url.startswith("/"):
                item["url"] = self._signed_replay_url(base, item.get("id"))
        return out

    def _passthrough(self, fn) -> None:
        try:
            data = fn()
        except PortalUnavailable:
            self._send(503, dict(ok=False, error="query_unavailable"))
            return
        self._send(200, dict(ok=True, data=data))

    def _query_post(self, parts) -> None:
        path = parts.path
        app = self.server
        if path == "/query-api/login":
            qa = getattr(app, "query_auth", None)
            if qa is None:
                self._send(503, dict(ok=False, error="query_auth_disabled"))
                return
            data = self._query_json()
            result = qa.login(str(data.get("account") or ""), str(data.get("secret") or ""), self._client_ip())
            status = 200 if result.get("ok") else int(result.get("status") or 500)
            self._send(status, {k: v for k, v in result.items() if k != "status"})
            return
        if path == "/query-api/refresh":
            qa = getattr(app, "query_auth", None)
            if qa is None:
                self._send(503, dict(ok=False, error="query_auth_disabled"))
                return
            result = qa.refresh(str(self._query_json().get("refresh_token") or ""), self._client_ip())
            status = 200 if result.get("ok") else int(result.get("status") or 500)
            self._send(status, {k: v for k, v in result.items() if k != "status"})
            return
        if path == "/query-api/logout":
            info = self._query_identity()
            if info is not None:
                header = self.headers.get("Authorization") or ""
                token = header[7:].strip()
                self._send(200, dict(ok=app.query_auth.logout(token)))
            return
        if path in ('/query-api/binding', '/query-api/binding/unbind'):
            info = self._query_identity()
            if info is None:
                return
            core = getattr(app, 'query_core', None)
            if core is None:
                raise PortalUnavailable('query_disabled')
            if parts.query:
                raise QueryRequestError('bad_param')
            if path.endswith('/unbind'):
                data = core.binding_clear(info['identity'])
            else:
                body = self._query_json()
                if set(body) - {'player'}:
                    raise QueryRequestError('bad_param')
                data = core.binding_set(info['identity'], body.get('player', ''))
            self._send(200, dict(ok=True, data=data))
            return
        if path == "/query-api/subscriptions":
            info = self._query_identity()
            if info is None:
                return
            core = getattr(app, "query_core", None)
            if core is None:
                self._send(503, dict(ok=False, error="query_disabled"))
                return
            # 双闸：全局 writes_enabled + 账号 writes（v5 §P4；改权限即时生效无需换令牌）。
            if not getattr(app, "writes_enabled", False):
                self._send(403, dict(ok=False, error="writes_disabled"))
                return
            if not info.get("writes"):
                self._send(403, dict(ok=False, error="forbidden"))
                return
            data = self._query_json()
            try:
                if type(data.get("notify", False)) is not bool:
                    raise ValueError("bad_notify")
                if data.get("op", "subscribe") not in ("subscribe", "unsubscribe"):
                    raise ValueError("bad_operation")
                if data.get("op") == "unsubscribe":
                    result = core.unsubscribe(info["identity"], str(data.get("player") or ""))
                else:
                    rank_top = data.get("rank_top")
                    result = core.subscribe(info["identity"], str(data.get("player") or ""),
                                            rank_top,
                                            bool(data.get("notify")))
            except ValueError as exc:
                self._send(400, dict(ok=False, error=str(exc)))
                return
            self._send(200, dict(ok=True, **result))
            return
        if path == "/query-api/command":
            if self._query_identity() is None:
                return
            # 查询命令入口：阶段 3 按结构化端点实现（交接文档 v5），先显式 501。
            self._send(501, dict(ok=False, error="not_implemented"))
            return
        if path.startswith("/query-api/admin/"):
            if not self._admin_ok():
                self._send(401, dict(ok=False, error="unauthorized"))
                return
            action = path[len("/query-api/admin/"):].strip("/")
            data = self._query_json()
            qa = app.query_auth
            if action == "issue":
                result = qa.provision(str(data.get("identity") or ""))
            elif action == "reissue":
                result = qa.reissue_code(str(data.get("identity") or ""))
            elif action == "reset":
                result = qa.reset_user(str(data.get("identity") or ""))
            elif action == "disable":
                result = qa.disable_user(str(data.get("identity") or ""))
            elif action == "writes":
                if type(data.get("enabled")) is not bool:
                    raise ValueError("bad_enabled")
                result = qa.set_writes(str(data.get("identity") or ""), data["enabled"])
            elif action == "revoke-device":
                result = qa.revoke_device(str(data.get("token_id") or ""))
            elif action == "unlock":
                result = qa.unlock(str(data.get("key") or ""))
            else:
                self._send(404, dict(ok=False, error="not_found"))
                return
            status = 200 if result.get("ok") else int(result.get("status") or 500)
            self._send(status, {k: v for k, v in result.items() if k != "status"})
            return
        self._send(404, dict(ok=False, error="not_found"))
