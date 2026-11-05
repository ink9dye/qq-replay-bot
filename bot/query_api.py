"""Mobile query routes, separate from watch authorization and task lifecycle."""
import hmac
import ipaddress
import json
import sqlite3
from urllib.parse import parse_qs, unquote
from .query_core import PortalUnavailable

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

    def _query_dispatch(self, parts, post=False):
        self.close_connection = True
        self.connection.settimeout(10)
        if getattr(self.server, "query_auth", None) is None:
            self._send(503, dict(ok=False, error="query_auth_disabled"))
            return
        if not self.server.query_gate.acquire(blocking=False):
            self._send(429, dict(ok=False, error="query_busy"))
            return
        try:
            try:
                return self._query_post(parts) if post else self._query_get(parts)
            except (ValueError, TypeError, UnicodeError):
                self._send(400, dict(ok=False, error="invalid_query_request"))
            except (PortalUnavailable, sqlite3.Error, OSError, TimeoutError):
                self._send(503, dict(ok=False, error="query_unavailable"))
        finally:
            self.server.query_gate.release()

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
                # /s/ 链接按身份建（share_links.user_id），与 QQ 端同机制。
                self._passthrough(lambda: core.player_replays(name, info["identity"]))
                return
            self._passthrough(lambda: fn(name))
            return
        self._send(404, dict(ok=False, error="not_found"))

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
