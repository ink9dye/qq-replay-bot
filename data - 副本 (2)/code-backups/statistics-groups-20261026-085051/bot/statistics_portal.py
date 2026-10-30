"""同一HTTP服务上的本机私有统计入口，独立于QQ分享链接权限。"""
import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import closing
from http.cookies import SimpleCookie, CookieError
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit,parse_qs

COOKIE='qqstats_owner'
SESSION_SECONDS=5*3600


def owner_secret(path):
    path=Path(path)
    try:
        with path.open('x',encoding='ascii') as stream:stream.write(secrets.token_urlsafe(32))
    except FileExistsError:pass
    secret=path.read_text(encoding='ascii').strip()
    if len(secret)!=43 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in secret):
        raise ValueError('statistics owner credential invalid')
    return secret


class StatisticsPortal:
    def __init__(self, database, replay_root, clock, secret):
        self.database=Path(database);self.replay_root=Path(replay_root);self.clock=clock;self.secret=secret

    def _session(self,expires):
        value=str(expires)
        return value+'.'+hmac.new(self.secret.encode(),value.encode(),hashlib.sha256).hexdigest()

    def _authenticated(self,cookie):
        try:
            cookies=SimpleCookie();cookies.load(cookie or '')
            value=cookies[COOKIE].value;expires=int(value.split('.',1)[0]);now=int(self.clock())
            return now<expires<=now+SESSION_SECONDS and hmac.compare_digest(value,self._session(expires))
        except (KeyError,ValueError,CookieError):return False

    def route(self,path,headers,client):
        try:host=urlsplit('//'+headers.get('Host','')).hostname
        except ValueError:return self._reply(403,'本机入口地址无效。')
        try:local=ip_address(client).is_loopback
        except ValueError:local=False
        # 隧道转发也来自本机，但Host及转发头不属于本机入口。
        if not local or host not in ('127.0.0.1','localhost','::1') or any(headers.get(k) for k in ('Forwarded','X-Forwarded-For','X-Forwarded-Host')):
            return self._reply(403,'仅允许从本机入口访问。')
        parsed=urlsplit(path);parts=parsed.path.strip('/').split('/')
        if len(parts)==3 and parts[:2]==['statistics','access']:
            if not hmac.compare_digest(parts[2],self.secret):return self._reply(403,'入口凭据无效。')
            value=self._session(int(self.clock())+SESSION_SECONDS)
            return self._reply(303,'',extra={'Location':'/statistics','Set-Cookie':f'{COOKIE}={value}; Path=/statistics; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'})
        if not self._authenticated(headers.get('Cookie')):return self._reply(403,'请使用本机“打开统计页面”入口。')
        if parts==['statistics']:
            return self._reply(200,Path(__file__).with_name('statistics_portal.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','investment']:
            return self._reply(200,Path(__file__).with_name('statistics_investment.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','api','card-search']:
            term=parse_qs(parsed.query).get('q',[''])[0].strip()
            if not term or len(term)>80:return self._reply(400,'请输入卡名或卡号。')
            try:names=json.loads((self.database.parent/'cache/cards.json').read_text(encoding='utf-8-sig'))
            except (OSError,ValueError):return self._reply(503,'本机卡名库暂不可用。')
            found=[]
            for code,info in names.items():
                name=info.get('cn_name') or info.get('sc_name') or code
                if term==code or term.casefold() in name.casefold():found.append(dict(code=int(code),name=name))
                if len(found)>=20:break
            return self._reply(200,json.dumps(dict(cards=found,limit=20),ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','api','investment']:
            from .player_statistics import query as investment
            args=parse_qs(parsed.query)
            try:
                code=int(args.get('code',['0'])[0])
                if not 0<code<=4294967295:raise ValueError()
            except ValueError:return self._reply(400,'卡号无效。')
            deck=args.get('deck',[]);series=args.get('series',[])
            if len(deck)>5 or len(series)>5 or any(len(v)>128 for v in deck+series):return self._reply(400,'筛选条件过长。')
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    result=investment(db,self.replay_root,code,float(self.clock()),deck,series)
                return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
            except (sqlite3.Error,OSError):return self._reply(503,'统计暂时不可用，请稍后刷新。')
        if parts in (['statistics','api','roster'],['statistics','api','build']):
            from .statistics_dashboard import query
            args=parse_qs(parsed.query)
            player=args.get('player',[None])[0] if parts[-1]=='build' else None
            if parts[-1]=='build' and (not player or len(player)>128):return self._reply(400,'需要玩家名称。')
            try:
                # 独立只读连接，不抢服务Store事务、不触发迁移；本次响应固定一个快照。
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    result=query(db,self.replay_root,float(self.clock()),player)
                if result is None:return self._reply(404,'暂无当前有效构筑。')
                if player:
                    try:names=json.loads((self.database.parent/'cache/cards.json').read_text(encoding='utf-8-sig'))
                    except (OSError,ValueError):names={}
                    for card in result['cards']:
                        info=names.get(str(card['code']),{});card['name']=info.get('cn_name') or info.get('sc_name') or str(card['code'])
                return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
            except (sqlite3.Error,OSError):return self._reply(503,'统计暂时不可用，请稍后刷新。')
        return self._reply(404,'找不到此统计页面。')

    @staticmethod
    def _reply(status,body,content_type='text/plain; charset=utf-8',extra=None):
        headers={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}
        headers.update(extra or {})
        return status,content_type,body.encode('utf8'),headers
