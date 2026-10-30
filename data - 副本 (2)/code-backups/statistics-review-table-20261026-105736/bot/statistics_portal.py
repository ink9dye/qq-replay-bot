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
from .card_search import card_name

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
        if parts==['statistics','groups']:
            return self._reply(200,Path(__file__).with_name('statistics_groups.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts in (['statistics','cards'],['statistics','matrix']):
            name='statistics_card_metrics.html' if parts[-1]=='cards' else 'statistics_matrix.html'
            return self._reply(200,Path(__file__).with_name(name).read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','assets','statistics_samples.js']:
            return self._reply(200,Path(__file__).with_name('statistics_samples.js').read_text(encoding='utf8'),'text/javascript; charset=utf-8')
        if parts==['statistics','api','card-metrics']:
            from .card_statistics import query as card_query,Filters
            args=parse_qs(parsed.query)
            try:
                code=int(args.get('code',['0'])[0]);partition=args.get('partition',['main'])[0]
                if not 0<code<=4294967295 or partition not in ('main','side','extra'):raise ValueError()
                order=args.get('first',['all'])[0]
                if order not in ('all','first','second'):raise ValueError()
                values=[[json.loads(v) for v in args.get(k,[])] for k in ('deck','opponent','series','opponent_series')]
                if any(len(v)>10 or any(s is not None and (not isinstance(s,str) or len(s)>128) for s in v) for v in values):raise ValueError()
            except ValueError:return self._reply(400,'卡号或筛选条件无效。')
            filters=Filters(*(tuple(v) for v in values),None if order=='all' else order=='first',args.get('internal',['0'])[0]=='1')
            def run(db):
                result=card_query(db,self.replay_root,code,partition,float(self.clock()),filters)
                times={r['digest']:r['recorded_at'] for r in db.execute('SELECT digest,recorded_at FROM stat_games')}
                for row in result['samples']:row['recorded_at']=times[row['digest']]
                result['generated_at']=float(self.clock());result['samples'].sort(key=lambda s:s['recorded_at'],reverse=True)
                return result
            return self._query_reply(run)
        if parts==['statistics','api','matrix']:
            from .statistics_matrix import query as matrix
            args=parse_qs(parsed.query)
            try:
                row=[json.loads(v) for v in args.get('row',[])];column=[json.loads(v) for v in args.get('column',[])]
                if len(row)>100 or len(column)>100 or any(v is not None and (not isinstance(v,str) or len(v)>128) for v in row+column):raise ValueError()
                order=args.get('first',['all'])[0]
                if order not in ('all','first','second'):raise ValueError()
            except (ValueError,TypeError):return self._reply(400,'矩阵筛选条件无效。')
            return self._query_reply(lambda db:matrix(db,self.replay_root,float(self.clock()),row,column,None if order=='all' else order=='first',args.get('internal',['0'])[0]=='1'))
        if parts==['statistics','api','exclusions']:
            from .statistics_samples import exclusions
            return self._query_reply(lambda db:dict(exclusions=exclusions(db,float(self.clock()))))
        if parts==['statistics','api','sample'] or len(parts)==3 and parts[:2]==['statistics','sample']:
            from .statistics_samples import details
            args=parse_qs(parsed.query);player=args.get('player',[''])[0];digest=args.get('digest',[''])[0] if parts[1]=='api' else parts[2]
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN');result=details(db,self.replay_root,float(self.clock()),player,digest)
                    if result is None:return self._reply(404,'样本已失效、被排除或不属于当前榜单。')
                    detail,file=result
                    if parts[1]=='sample':
                        status,kind,_,headers=self._reply(200,'','application/octet-stream',{'Content-Disposition':f'attachment; filename="sample-{digest[:16]}.yrp"'})
                        return status,kind,file.read_bytes(),headers
                from .statistics_card_names import index
                names=index(self.database.parent)
                for card in detail['cards']:
                    info=names.get(str(card['code']),{});card['name']=card_name(info) if info else str(card['code'])
                return self._reply(200,json.dumps(detail,ensure_ascii=False),'application/json; charset=utf-8')
            except (sqlite3.Error,OSError):return self._reply(503,'来源暂不可用。')
        if parts==['statistics','api','groups']:
            from .statistics_groups import query as groups
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    result=groups(db,self.replay_root,float(self.clock()))
                return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
            except (sqlite3.Error,OSError):return self._reply(503,'统计暂时不可用，请稍后刷新。')
        if len(parts)==3 and parts[:2]==['statistics','source']:
            from .statistics_groups import source_file
            player=parse_qs(parsed.query).get('player',[''])[0]
            if not player or len(player)>128:return self._reply(400,'需要玩家名称。')
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    file=source_file(db,self.replay_root,float(self.clock()),player,parts[2])
                    if file is None:return self._reply(404,'源录像已失效或不属于当前有效观测。')
                    blob=file.read_bytes()
                status,content_type,_,extra=self._reply(200,'','application/octet-stream',
                    {'Content-Disposition':f'attachment; filename="source-{parts[2][:16]}.yrp"'})
                return status,content_type,blob,extra
            except (sqlite3.Error,OSError):return self._reply(404,'源录像当前不可用。')
        if parts==['statistics','api','card-search']:
            term=parse_qs(parsed.query).get('q',[''])[0].strip()
            if not term or len(term)>80:return self._reply(400,'请输入卡名或卡号。')
            from .statistics_card_names import search
            found=search(self.database.parent,term)
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
                    from .statistics_card_names import index
                    names=index(self.database.parent)
                    for card in result['cards']:
                        info=names.get(str(card['code']),{});card['name']=card_name(info) if info else str(card['code'])
                return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
            except (sqlite3.Error,OSError):return self._reply(503,'统计暂时不可用，请稍后刷新。')
        return self._reply(404,'找不到此统计页面。')

    def _query_reply(self,callback):
        try:
            with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                db.row_factory=sqlite3.Row;db.execute('BEGIN');result=callback(db)
            return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
        except (sqlite3.Error,OSError):return self._reply(503,'统计暂时不可用，请稍后刷新。')

    def post(self,path,headers,client,body):
        try:host=urlsplit('//'+headers.get('Host','')).hostname;local=ip_address(client).is_loopback
        except ValueError:return self._reply(403,'仅允许本机操作。')
        if not local or host not in ('127.0.0.1','localhost','::1') or any(headers.get(k) for k in ('Forwarded','X-Forwarded-For','X-Forwarded-Host')) or not self._authenticated(headers.get('Cookie')):
            return self._reply(403,'仅允许本机私有会话操作。')
        if headers.get('Origin')!='http://'+headers.get('Host','') or headers.get('Content-Type','').split(';',1)[0]!='application/json':return self._reply(403,'请求来源无效。')
        if path!='/statistics/api/exclusion':return self._reply(404,'找不到此操作。')
        try:
            if len(body)>2048:raise ValueError()
            data=json.loads(body);player=data['player'];digest=data['digest'];excluded=data['excluded']
            if not isinstance(player,str) or not isinstance(digest,str):raise ValueError()
            from .statistics_samples import set_exclusion
            with closing(sqlite3.connect(self.database,timeout=3)) as db:
                db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
                try:set_exclusion(db,float(self.clock()),player,digest,excluded);db.commit()
                except BaseException:db.rollback();raise
            return self._reply(200,json.dumps(dict(ok=True,excluded=excluded)),'application/json; charset=utf-8')
        except (ValueError,KeyError,TypeError):return self._reply(400,'样本或排除条件无效。')
        except sqlite3.Error:return self._reply(503,'操作暂未保存，请稍后重试。')

    @staticmethod
    def _reply(status,body,content_type='text/plain; charset=utf-8',extra=None):
        headers={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}
        headers.update(extra or {})
        return status,content_type,body.encode('utf8'),headers
