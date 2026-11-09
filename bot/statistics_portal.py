"""同一HTTP服务上的本机私有统计入口，独立于QQ分享链接权限。"""
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from http.cookies import SimpleCookie, CookieError
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,quote as url_quote
from .card_search import card_name

COOKIE='qqstats_owner'
SESSION_SECONDS=5*3600
QUERY_WAIT_SECONDS=8.0
QUERY_CACHE_MAX_BYTES = 8 * 1024 * 1024
CARD_SAMPLES_LIMIT = 200
CARD_METRICS_CACHE_SECONDS = 600.0
PRECOMPUTED_ROSTER_SECONDS = 900.0
PRECOMPUTED_CARD_REPORT_SECONDS = 3600.0
CARD_REPORT_PRECOMPUTED_SCOPES = (('100', '100'), ('200', '200'))
CARD_REPORT_PRECOMPUTED_PHASES = ('main_first', 'main_second', 'side_first', 'side_second')


def card_report_precomputed_scope(args):
    """页面默认参数才吃预计算；任何筛选/自定义/分组请求都走实时计算。"""
    for key in ('code', 'exclude', 'combo', 'deck', 'opponent', 'series', 'opponent_series'):
        if args.get(key):
            return None
    if args.get('phase_none', ['0'])[0] == '1':
        return None
    scope = (args.get('self_range', ['100'])[0], args.get('opponent_range', ['all'])[0])
    if scope not in CARD_REPORT_PRECOMPUTED_SCOPES:
        return None
    defaults = dict(metric='opening', threshold='1', min='0', sort='rate',
                    partition='auto', limit='500', seat='all', internal='0', deck_match='contains')
    for key, expected in defaults.items():
        value = args.get(key, [expected])[0]
        if value != expected:
            return None
    phases = args.get('phase', [])
    if len(phases) != len(CARD_REPORT_PRECOMPUTED_PHASES) or sorted(phases) != sorted(CARD_REPORT_PRECOMPUTED_PHASES):
        return None
    return scope



def _trim_heap():
    """Return freed arenas to the OS after a heavy build (glibc only)."""
    try:
        import ctypes
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except (OSError, AttributeError):
        pass
CACHED_API_PATHS=frozenset('/statistics/api/'+name for name in (
    'recordings','deck-naming/groups','card-table','cards-extra','card-combo',
    'card-report','roster','build','matrix','side-analysis','side-recent'))


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
    def __init__(self, database, replay_root, clock, secret, background=False):
        self.database=Path(database);self.replay_root=Path(replay_root);self.clock=clock;self.secret=secret
        self._query_cache={}
        self._query_cache_lock=threading.Lock()
        self._card_metrics_cache={}
        self._card_metrics_lock=threading.Lock()
        self._query_compute_lock=threading.RLock()
        self._refresh=None
        if background:
            from .statistics_refresh import QueryRefresh
            self._refresh=QueryRefresh(database)
        self._small_world_catalog_lock=threading.Lock()
        from .player_history import PlayerHistory
        self.player_history=PlayerHistory()
        from .statistics_pt import InteractiveHistory
        self.interactive_history=InteractiveHistory()

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
        api_path=urlsplit(path).path
        # Cached queries own their gate: cache hits must never queue behind a cold query.
        if api_path.startswith('/statistics/api/') and api_path not in CACHED_API_PATHS and api_path not in (
                '/statistics/api/devices','/statistics/api/card-search','/statistics/api/card-info',
                '/statistics/api/small-world-cards'):
            if not self._query_compute_lock.acquire(timeout=QUERY_WAIT_SECONDS):
                return self._query_busy_reply()
            try:
                return self._route(path,headers,client)
            finally:
                self._query_compute_lock.release()
        return self._route(path,headers,client)

    def _route(self,path,headers,client):
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
            # 只允许跳到固定的本机后台页，避免成为开放重定向。
            requested=parse_qs(parsed.query).get('next',[''])[0]
            target=requested if requested in ('/statistics/deck-naming','/statistics/devices') else '/statistics'
            return self._reply(303,'',extra={'Location':target,'Set-Cookie':f'{COOKIE}={value}; Path=/statistics; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'})
        if not self._authenticated(headers.get('Cookie')):return self._reply(403,'请使用本机“打开统计页面”入口。')
        if parts==['statistics','small-world']:
            return self._reply(200,Path(__file__).with_name('statistics_small_world.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if len(parts)==3 and parts[:2]==['statistics','assets'] and parts[2] in ('statistics_small_world.js','small_world_engine.js','statistics_small_world.css'):
            mime='text/css; charset=utf-8' if parts[2].endswith('.css') else 'text/javascript; charset=utf-8'
            return self._reply(200,Path(__file__).with_name(parts[2]).read_text(encoding='utf8'),mime,{'Cache-Control':'private, max-age=3600'})
        if parts==['statistics','api','small-world-cards']:
            from .small_world import read_cards
            from .statistics_local_cards import library_root
            if not self._small_world_catalog_lock.acquire(blocking=False):
                return self._reply(429,'卡库正在读取，请稍后重试。',extra={'Retry-After':'2'})
            try:
                root=library_root(self.database.parent) or self.database.parent.parent/'ygopro'
                cards=read_cards(root)
                payload={'cards':[[c.code,c.name,c.kind,*c.properties] for c in cards.values()]}
                return self._reply(200,json.dumps(payload,ensure_ascii=False,separators=(',',':')),'application/json; charset=utf-8')
            except (OSError,sqlite3.Error,ValueError):
                return self._reply(503,'小世界卡库暂不可用，请稍后重试。')
            finally:
                self._small_world_catalog_lock.release()
        if len(parts)==3 and parts[:2]==['statistics','assets'] and parts[2] in ('statistics_autocomplete.js','statistics_theme.css','statistics_ui.css','statistics_card_metrics.js','statistics_cards_report.js','statistics_cards_report.css','statistics_cards_report_combo.css','statistics_build.js','statistics_pt.js','statistics_filter_controls.js','statistics_nav.css','statistics_nav.js','statistics_busy.js','statistics_refresh.js'):
            mime='text/css; charset=utf-8' if parts[2].endswith('.css') else 'text/javascript; charset=utf-8'
            return self._reply(200,Path(__file__).with_name(parts[2]).read_text(encoding='utf8'),mime,{'Cache-Control':'private, max-age=3600'})
        if parts==['statistics','devices']:
            return self._reply(200,Path(__file__).with_name('statistics_devices.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','api','devices']:
            dashboard=getattr(self,'device_dashboard',None)
            if dashboard is None:return self._reply(503,'设备看板尚未配置。')
            args=parse_qs(parsed.query)
            if 'before_id' in args:
                try:result=dashboard.recording_history(int(args['before_id'][0]))
                except (ValueError,TypeError):return self._reply(400,'录像游标无效。')
                except (OSError,sqlite3.Error):return self._reply(503,'录像读取失败，请重试。')
                return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
            return self._reply(200,json.dumps(dashboard.snapshot(),ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics']:
            return self._reply(200,Path(__file__).with_name('statistics_portal.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','build']:
            return self._reply(200,Path(__file__).with_name('statistics_build.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','decks']:
            # 卡组统计页已下线：旧链接（含 QQ“统计 卡组”）回到玩家排名。
            return self._reply(302,'',extra={'Location':'/statistics'})
        if parts==['statistics','recordings']:
            return self._reply(200,Path(__file__).with_name('statistics_recordings.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','api','recordings']:
            from .statistics_recording_filters import query as recordings
            args=parse_qs(parsed.query)
            if any(len(v)>2000 for v in args.values()):return self._reply(400,'筛选项过多。')
            def run(db):
                return recordings(db,self.replay_root,float(self.clock()),args)
            try:
                return self._cached_query_reply(('recordings',parsed.query),run,ttl=30.0)
            except ValueError as exc:
                return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','recording-zip']:
            from .statistics_recording_archive import archive
            from urllib.parse import quote
            args=parse_qs(parsed.query);player=args.get('player',[''])[0];digest=args.get('digest',[None])[0];match=args.get('match',[None])[0];scope=args.get('scope',['build'])[0]
            if not player or len(player)>128 or scope not in ('build','player')or(match is not None and(len(match)!=16 or any(c not in '0123456789abcdef'for c in match))):return self._reply(400,'打包条件无效。')
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=5))as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    body,filename,count=archive(db,self.replay_root,float(self.clock()),player,digest,scope,match,clock=self.clock)
                status,kind,_,headers=self._reply(200,'','application/zip',{'Content-Disposition':"attachment; filename=replays.zip; filename*=UTF-8''"+quote(filename),'X-Replay-Count':str(count),'Cache-Control':'no-store'})
                return status,kind,body,headers
            except(ValueError,sqlite3.Error,OSError):return self._reply(409,'有效录像范围已变化、为空或过大，请刷新并缩小范围后重试。')
        if parts==['statistics','recordings-zip']:
            # 录像页"按当前全部筛选条件打包下载"：和 api/recordings 同一套条件与口径。
            from .statistics_recording_archive_filters import filtered_archive
            from urllib.parse import quote
            args=parse_qs(parsed.query)
            if any(len(v)>2000 for v in args.values()):return self._reply(400,'筛选项过多。')
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=5))as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    body,filename,files,views=filtered_archive(db,self.replay_root,float(self.clock()),args,clock=self.clock)
                status,kind,_,headers=self._reply(200,'','application/zip',{'Content-Disposition':"attachment; filename=replays.zip; filename*=UTF-8''"+quote(filename),'X-Replay-Count':str(files),'X-View-Count':str(views),'Cache-Control':'no-store'})
                return status,kind,body,headers
            except ValueError as exc:
                message={'empty_scope':'当前筛选没有可打包的录像，请调整条件后重试。',
                         'scope_too_large':'筛选范围过大（超过 5000 个玩家小局视角），请缩小条件后重试。',
                         'archive_too_many_files':'命中的录像文件过多（超过 2000 个），请缩小条件后重试。',
                         'archive_too_large':'打包内容超过 256MB，请缩小范围后重试。',
                         'source_expired_during_archive':'有录像在打包过程中到期，请刷新后重试。',
                         'source_excluded':'有录像已被排除，请刷新后重试。',
                         'source_missing':'有原始录像文件缺失，请刷新后重试。',
                         'source_changed':'有原始录像字节已变化，请刷新后重试。'}.get(str(exc),'打包条件无效，请刷新后重试。')
                return self._reply(409,message)
            except(sqlite3.Error,OSError):return self._reply(409,'有效录像范围已变化、为空或过大，请刷新并缩小范围后重试。')
        if parts==['statistics','deck-naming']:
            return self._reply(200,Path(__file__).with_name('statistics_deck_naming.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','api','deck-naming','groups']:
            from .series_naming_admin import list_groups
            args=parse_qs(parsed.query)
            query=args.get('q',[''])[0]
            try:
                limit=int(args.get('limit',['50'])[0]);offset=int(args.get('offset',['0'])[0])
            except ValueError:return self._reply(400,'分页参数无效。')
            if len(query)>60:return self._reply(400,'搜索词过长。')
            # 配置一写（删除/加卡/阈值）就换了缓存键，避免删完立刻回读仍命中旧列表；
            # 页面写入后的刷新会带 _ 时间戳，按完整查询串分键即可绕过旧结果。
            try:
                state=(self.database.parent/'series-naming.json').stat();config_stamp=(state.st_mtime_ns,state.st_size)
            except OSError:config_stamp=None
            return self._cached_query_reply(('groups',parsed.query,config_stamp), lambda db:list_groups(db,self.database.parent,float(self.clock()),query,limit,offset), ttl=60.0)
        if len(parts)==5 and parts[:4]==['statistics','api','deck-naming','groups']:
            from .series_naming_admin import group_detail
            from urllib.parse import unquote
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    result=group_detail(db,self.database.parent,float(self.clock()),unquote(parts[4]))
                return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
            except LookupError:return self._reply(404,'找不到这个命名组。')
            except (sqlite3.Error,OSError):return self._reply(503,'组合数据暂时不可用，请稍后刷新。')
            except ValueError as exc:return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','api','deck-naming']:
            from .statistics_deck_naming import detail,list_rows
            args=parse_qs(parsed.query);player=args.get('player',[''])[0]
            scope=args.get('scope',['100'])[0];query=args.get('q',[''])[0];deck_query=args.get('deck',[''])[0]
            def run(db):
                return detail(db,self.database.parent,float(self.clock()),player,self.replay_root) if player else list_rows(db,float(self.clock()),scope,query,self.replay_root,deck_query=deck_query)
            try:return self._query_reply(run)
            except LookupError:return self._reply(404,'没有找到这个玩家的有效构筑或名单记录。')
            except ValueError as exc:return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','api','pt-history']:
            player=parse_qs(parsed.query).get('player',[''])[0]
            if not player.strip() or len(player)>100:return self._reply(400,'玩家名无效。')
            return self._reply(200,json.dumps(self.interactive_history.query(player),ensure_ascii=False),'application/json; charset=utf-8')
        if parts in (['statistics','api','player-history'],['statistics','pt-chart']):
            player=parse_qs(parsed.query).get('player',[''])[0]
            if not player.strip() or len(player)>100:return self._reply(400,'玩家名无效。')
            reply=self.player_history.handle(player)
            image=getattr(reply,'image_url','')
            if parts[1]=='api':
                return self._reply(200,json.dumps(dict(text=str(reply),has_chart=image.startswith('base64://')),ensure_ascii=False),'application/json; charset=utf-8')
            if not image.startswith('base64://'):return self._reply(404,str(reply))
            import base64
            blob=base64.b64decode(image[9:],validate=True)
            status,kind,_,headers=self._reply(200,'','image/png')
            return status,kind,blob,headers
        if parts==['statistics','api','card-info']:
            from .statistics_local_cards import card_details
            value=parse_qs(parsed.query).get('code',[''])[0]
            if not value.isdigit() or len(value)>10 or not 0<int(value)<=4294967295:return self._reply(400,'卡号无效。')
            return self._reply(200,json.dumps(card_details(self.database.parent,int(value)),ensure_ascii=False),'application/json; charset=utf-8')
        if len(parts)==3 and parts[:2]==['statistics','card-thumb']:
            from .statistics_local_cards import web_thumb, THUMB_REMOTE
            value=parts[2]
            if not value.isdigit() or len(value)>10 or not 0<int(value)<=4294967295:
                return self._reply(400,'卡号无效。')
            image=web_thumb(self.database.parent,int(value))
            if image is None:
                if int(value)>=100_000_000:
                    from .card_image_cache import full_url
                    target=full_url(self.database.parent,int(value))
                else:
                    target=THUMB_REMOTE % int(value)
                return self._reply(302,'',extra={'Location':target})
            mime={'.webp':'image/webp','.jpg':'image/jpeg','.png':'image/png'}[image.suffix]
            status,kind,_,extra=self._reply(200,'',mime,{'Cache-Control':'private, max-age=86400'})
            return status,kind,image.read_bytes(),extra
        if parts==['statistics','api','deck-pie.png']:
            # 网站自己生成并缓存饼图 PNG；QQ「前百饼图」直接复用这张图。
            from .statistics_pie_image import render_from_roster
            try:
                reply=self._roster_reply()
                blob=render_from_roster(self.database, json.loads(reply[2]))
            except ValueError:
                return self._reply(503,'暂无卡组档案。')
            except Exception:
                return self._reply(500,'饼图暂时无法生成，请稍后重试。')
            status,kind,_,extra=self._reply(200,'','image/png',{'Cache-Control':'private, max-age=60'})
            return status,kind,blob,extra
        if len(parts)==3 and parts[:2]==['statistics','card-image']:
            from .statistics_local_cards import picture
            if not parts[2].isdigit() or len(parts[2])>10:return self._reply(400,'卡号无效。')
            image=picture(self.database.parent,int(parts[2]))
            if image is None:return self._reply(404,'本地暂无卡图。')
            file,mime=image
            status,kind,_,extra=self._reply(200,'',mime,{'Cache-Control':'public, max-age=86400'})
            return status,kind,file.read_bytes(),extra
        if parts in (['statistics','api','build-detail'],['statistics','build-ydk']):
            from .statistics_build_detail import query as build_detail,ydk,ydk_filename
            args=parse_qs(parsed.query);player=args.get('player',[''])[0];digest=args.get('digest',[None])[0];scope=args.get('scope',['build'])[0]
            if digest is not None and (len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):return self._reply(400,'来源编号无效。')
            if not player or len(player)>128 or scope not in ('build','player'):return self._reply(400,'玩家或范围无效。')
            def run(db):
                result=build_detail(db,self.replay_root,float(self.clock()),player,digest,scope)
                if result:
                    from .statistics_card_names import index
                    names=index(self.database.parent)
                    for card in result['cards']+result['inferred_side']['cards']:
                        info=names.get(str(card['code']));card['name']=card_name(info) if info else '卡名暂缺'
                    for match in result['matches']:
                        for card in match['changes']['added']+match['changes']['removed']:
                            info=names.get(str(card['code']));card['name']=card_name(info) if info else '卡名暂缺'
                return result
            if parts[1]=='api':return self._query_reply(run)
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN');result=run(db)
                    if result is None:return self._reply(404,'构筑来源当前不可用。')
                    inferred=args.get('inferred',['0'])[0]=='1'
                    try:
                        body=ydk(result,inferred)
                    except ValueError:
                        if not inferred:raise
                        # 推测 Side 冲突时不再挡下载：回落到直接记录的 Side。
                        inferred=False;body=ydk(result,False)
                    name,fallback=ydk_filename(result,inferred)
                    header = f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{url_quote(name)}'
                    return self._reply(200,body,'text/plain; charset=utf-8',{'Content-Disposition':header})
            except ValueError:return self._reply(409,'构筑导出失败。')
            except (sqlite3.Error,OSError):return self._reply(503,'构筑暂不可用。')
        if parts==['statistics','api','player-investment']:
            from .statistics_investment import query as player_investment
            args=parse_qs(parsed.query)
            try:
                code=int(args.get('code',['0'])[0]);basis=args.get('basis',['observed'])[0]
                values=[[json.loads(v) for v in args.get(k,[])] for k in ('deck','series')]
                if not 0<code<=4294967295 or basis not in ('observed','initial') or any(len(v)>100 or any(x is not None and (not isinstance(x,str) or len(x)>128) for x in v) for v in values):raise ValueError()
            except (ValueError,TypeError):return self._reply(400,'卡号或筛选条件无效。')
            return self._query_reply(lambda db:player_investment(db,self.replay_root,code,float(self.clock()),*(tuple(v) for v in values),basis=basis))
        if parts==['statistics','investment']:
            return self._reply(200,Path(__file__).with_name('statistics_investment.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','groups']:
            return self._reply(200,Path(__file__).with_name('statistics_groups.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','card-table']:
            # 2026-10-31 合并：卡片对比升级为唯一的卡片统计页，旧入口 303 过去（书签/旧链接继续可用）。
            return self._reply(303,'',extra={'Location':'/statistics/cards-report'})
        if parts==['statistics','api','card-table']:
            from .statistics_card_overview import query as overview
            args=parse_qs(parsed.query,keep_blank_values=True)
            basis=args.get('basis',['observed'])[0]
            if basis not in ('observed','initial'):return self._reply(400,'选源无效。')
            try:
                from .statistics_rank_scope import parse_ranges,scoped_query
                ranges=parse_ranges(args)
                decks=tuple(json.loads(v) for v in args.get('deck',[]))
                if len(decks)>100 or any(v is not None and (not isinstance(v,str) or not v.strip() or len(v)>128) for v in decks):raise ValueError()
            except (ValueError,TypeError):
                return self._reply(400,json.dumps({'error':'卡组条件无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            return self._cached_query_reply(('card-table',parsed.query),lambda db:scoped_query(ranges,lambda:overview(db,self.replay_root,float(self.clock()),basis,decks)))
        if parts==['statistics','cards-report']:
            return self._reply(200,Path(__file__).with_name('statistics_cards_report.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','cards-extra']:
            return self._reply(200,Path(__file__).with_name('statistics_cards_extra.html').read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','api','cards-extra']:
            from .statistics_extra_report import query as extra_report
            from .statistics_rank_scope import parse_ranges,scoped_query
            from .statistics_card_report import Filters
            args=parse_qs(parsed.query)
            try:
                ranges=parse_ranges(args)
                seat=args.get('seat',['all'])[0]
                if seat not in ('all','first','second'):raise ValueError()
                internal=args.get('internal',['0'])[0]=='1'
                phases=args.get('phase',[])
                phase_none=args.get('phase_none',['0'])[0]=='1'
                sort=args.get('sort',['carry'])[0]
                if sort not in ('carry','players','appearance','win','code'):raise ValueError()
                decks=tuple(json.loads(v) for v in args.get('deck',[]))
                opponents=tuple(json.loads(v) for v in args.get('opponent',[]))
                if any(len(v)>100 or any(s is not None and (not isinstance(s,str) or not s.strip() or len(s)>128) for s in v)
                       for v in (decks,opponents)):
                    raise ValueError()
            except (ValueError,TypeError):
                return self._reply(400,json.dumps({'error':'额外卡筛选条件无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            filters=Filters(decks,opponents,(),(),None if seat=='all' else seat=='first',internal,deck_contains=args.get('deck_match',['contains'])[0]!='exact')
            try:
                return self._cached_query_reply(('cards-extra',parsed.query),lambda db:scoped_query(ranges,lambda:extra_report(
                    db,self.replay_root,float(self.clock()),filters=filters,phases=phases,phase_none=phase_none,sort=sort)))
            except ValueError as exc:
                return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','api','card-combo']:
            from .statistics_card_report import Filters
            from .statistics_combo_report import query as combo_report
            args=parse_qs(parsed.query,keep_blank_values=True)
            try:
                from .statistics_rank_scope import parse_ranges,scoped_query
                ranges=parse_ranges(args)
                values=[[json.loads(v) for v in args.get(k,[])] for k in ('deck','opponent','series','opponent_series')]
                if any(len(v)>100 or any(s is not None and (not isinstance(s,str) or not s.strip() or len(s)>128) for s in v) for v in values):
                    raise ValueError()
                seat=args.get('seat',['all'])[0]
                if seat not in ('all','first','second'):raise ValueError()
                combo=args.get('combo',[])
            except (ValueError,TypeError):
                return self._reply(400,json.dumps({'error':'组合筛选条件无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            filters=Filters(*(tuple(v) for v in values),None if seat=='all' else seat=='first',args.get('internal',['0'])[0]=='1',deck_contains=args.get('deck_match',['contains'])[0]!='exact')
            try:
                return self._cached_query_reply(('card-combo',parsed.query),lambda db:scoped_query(ranges,lambda:combo_report(
                    db,self.replay_root,float(self.clock()),combo,filters=filters,
                    phases=args.get('phase',[]),phase_none=args.get('phase_none',['0'])[0]=='1')))
            except ValueError as exc:
                return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','api','card-report']:
            from .card_statistics import Filters
            from .statistics_card_report import query as card_report
            args=parse_qs(parsed.query,keep_blank_values=True)
            try:
                from .statistics_rank_scope import parse_ranges,scoped_query
                ranges=parse_ranges(args)
                codes=args.get('code',[])
                values=[[json.loads(v) for v in args.get(k,[])] for k in ('deck','opponent','series','opponent_series')]
                if any(len(v)>100 or any(s is not None and (not isinstance(s,str) or not s.strip() or len(s)>128) for s in v) for v in values):raise ValueError()
                seat=args.get('seat',['all'])[0]
                if seat not in ('all','first','second'):raise ValueError()
            except (ValueError,TypeError):
                return self._reply(400,json.dumps({'error':'筛选条件无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            precomputed=self._precomputed_card_report(args)
            if precomputed is not None:
                return precomputed
            filters=Filters(*(tuple(v) for v in values),None if seat=='all' else seat=='first',args.get('internal',['0'])[0]=='1',deck_contains=args.get('deck_match',['contains'])[0]!='exact')
            try:
                return self._cached_query_reply(('card-report',parsed.query),lambda db:scoped_query(ranges,lambda:card_report(
                    db,self.replay_root,float(self.clock()),codes=codes,filters=filters,
                    metric=args.get('metric',['drawn'])[0],threshold=args.get('threshold',['1'])[0],
                    min_games=args.get('min',['0'])[0],sort=args.get('sort',['people'])[0],
                    partition=args.get('partition',['auto'])[0],limit=args.get('limit',['500'])[0],
                    exclude=args.get('exclude',[]),phases=args.get('phase',[]),phase_none=args.get('phase_none',['0'])[0]=='1',
                    combos=args.get('combo',[]))))
            except ValueError as exc:
                return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts in (['statistics','cards'],['statistics','matrix']):
            name='statistics_card_metrics.html' if parts[-1]=='cards' else 'statistics_matrix.html'
            return self._reply(200,Path(__file__).with_name(name).read_text(encoding='utf8'),'text/html; charset=utf-8')
        if parts==['statistics','assets','statistics_samples.js']:
            return self._reply(200,Path(__file__).with_name('statistics_samples.js').read_text(encoding='utf8'),'text/javascript; charset=utf-8',{'Cache-Control':'private, max-age=3600'})
        if parts==['statistics','api','card-metrics']:
            from .card_statistics import query as card_query,Filters
            args=parse_qs(parsed.query)
            try:
                from .statistics_rank_scope import parse_ranges,scoped_query
                ranges=parse_ranges(args)
                code=int(args.get('code',['0'])[0]);partition=args.get('partition',['main'])[0]
                if not 0<code<=4294967295 or partition not in ('main','side','extra'):raise ValueError()
                order=args.get('first',['all'])[0]
                if order not in ('all','first','second'):raise ValueError()
                investment_basis=args.get('basis',['observed'])[0]
                if investment_basis not in ('observed','initial'):raise ValueError()
                values=[[json.loads(v) for v in args.get(k,[])] for k in ('deck','opponent','series','opponent_series')]
                if any(len(v)>100 or any(s is not None and (not isinstance(s,str) or len(s)>128) for s in v) for v in values):raise ValueError()
            except ValueError:return self._reply(400,'卡号或筛选条件无效。')
            filters=Filters(*(tuple(v) for v in values),None if order=='all' else order=='first',args.get('internal',['0'])[0]=='1')
            full_request=(args.get('full',['0'])[0]=='1'
                          or 'Python-urllib' not in (headers.get('User-Agent') or ''))
            def run(db):
                from .statistics_investment import query as player_investment
                snapshot_now=float(self.clock())
                result=card_query(db,self.replay_root,code,partition,snapshot_now,filters)
                if full_request:
                    result['investment']=player_investment(db,self.replay_root,code,snapshot_now,filters.deck_types,filters.series,basis=investment_basis)
                digests=sorted({s['digest'] for s in result['samples']})
                times={}
                for start in range(0,len(digests),400):
                    chunk=digests[start:start+400]
                    marks=','.join('?' for _ in chunk)
                    times.update({r['digest']:r['recorded_at'] for r in db.execute(
                    f'SELECT digest,recorded_at FROM stat_games WHERE digest IN ({marks})',chunk)})
                for row in result['samples']:row['recorded_at']=times[row['digest']]
                result['generated_at']=snapshot_now;result['samples'].sort(key=lambda s:s['recorded_at'],reverse=True)
                if args.get('full',['0'])[0]!='1' and len(result['samples'])>CARD_SAMPLES_LIMIT:
                    result['samples']=result['samples'][:CARD_SAMPLES_LIMIT]
                    result['samples_truncated']=True
                return result
            cached=self._card_metrics_cache_get(parsed.query)
            if cached is not None:
                return cached
            reply=self._query_reply(lambda db:scoped_query(ranges,lambda:run(db)))
            if reply[0]==200:
                self._card_metrics_cache_put(parsed.query,reply)
            _trim_heap()
            return reply
        if parts==['statistics','api','matrix']:
            from .statistics_matrix import query as matrix
            from .statistics_rank_scope import parse_ranges,scoped_query
            args=parse_qs(parsed.query)
            try:
                row=[json.loads(v) for v in args.get('row',[])];column=[json.loads(v) for v in args.get('column',[])]
                if len(row)>100 or len(column)>100 or any(v is not None and (not isinstance(v,str) or len(v)>128) for v in row+column):raise ValueError()
                order=args.get('first',['all'])[0]
                if order not in ('all','first','second'):raise ValueError()
                ranges=parse_ranges(args)
            except (ValueError,TypeError):return self._reply(400,'矩阵筛选条件无效。')
            return self._cached_query_reply(('matrix',parsed.query),lambda db:scoped_query(ranges,lambda:matrix(db,self.replay_root,float(self.clock()),row,column,None if order=='all' else order=='first',args.get('internal',['0'])[0]=='1')),ttl=30.0)
        if parts==['statistics','api','matrix-cell']:
            from .statistics_matrix_cell import query as matrix_cell
            from .statistics_rank_scope import parse_ranges,scoped_query
            args=parse_qs(parsed.query)
            try:
                row=json.loads(args.get('row',['null'])[0]);column=json.loads(args.get('column',['null'])[0])
                if not (row is None or isinstance(row,str)) or not (column is None or isinstance(column,str)) or len(row or '')>128 or len(column or '')>128:raise ValueError()
                order=args.get('first',['all'])[0]
                if order not in ('all','first','second'):raise ValueError()
                ranges=parse_ranges(args)
            except (ValueError,TypeError):return self._reply(400,'矩阵格子条件无效。')
            try:
                return self._query_reply(lambda db:scoped_query(ranges,lambda:matrix_cell(db,self.replay_root,float(self.clock()),row,column,None if order=='all' else order=='first',args.get('internal',['0'])[0]=='1',threshold=args.get('threshold',['10'])[0])))
            except ValueError as exc:
                return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','api','side-analysis']:
            # 玩家排行/构筑页与对战矩阵共用的 Side 换入换出证据查询；只读。
            from .statistics_side_analysis import query as side_analysis
            args=parse_qs(parsed.query,keep_blank_values=True)
            mode=args.get('mode',['player'])[0]
            player=(args.get('player',[''])[0] or '').strip()
            deck=(args.get('deck',[''])[0] or '').strip()
            opponent_deck=(args.get('opponent_deck',[''])[0] or '').strip()
            algorithm=args.get('algorithm',['1'])[0]
            first=args.get('first',['all'])[0]
            cell_token=(args.get('cell_token',[''])[0] or '').strip()
            digest=(args.get('digest',[''])[0] or '').strip()
            scope=(args.get('scope',['build'])[0] or 'build').strip()
            if mode not in ('player','matrix') or algorithm not in ('0','1') or first not in ('all','first','second'):
                return self._reply(400,json.dumps({'error':'Side 分析入口无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            if digest and (len(digest)!=64 or any(char not in '0123456789abcdef' for char in digest)):
                return self._reply(400,json.dumps({'error':'来源编号无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            if scope not in ('build','player'):
                return self._reply(400,json.dumps({'error':'Side 范围无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            if len(cell_token)>64:
                return self._reply(400,json.dumps({'error':'矩阵格样本标记无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            if any(len(value)>128 for value in (player,deck,opponent_deck)):
                return self._reply(400,json.dumps({'error':'玩家或卡组条件过长。'},ensure_ascii=False),'application/json; charset=utf-8')
            if mode=='player' and not player:
                return self._reply(400,json.dumps({'error':'需要玩家名称。'},ensure_ascii=False),'application/json; charset=utf-8')
            if mode=='matrix' and not deck:
                return self._reply(400,json.dumps({'error':'矩阵分析需要我方卡组。'},ensure_ascii=False),'application/json; charset=utf-8')
            def side_run(db):
                if mode=='matrix':
                    from .statistics_rank_scope import parse_ranges,scoped_query
                    ranges=parse_ranges(args)
                    result=scoped_query(ranges,lambda:side_analysis(
                        db,self.replay_root,float(self.clock()),None,deck,opponent_deck or None,
                        mode='matrix',internal_only=args.get('internal',['0'])[0]=='1',
                        algorithm=False,first=first,cell_token=cell_token))
                else:
                    result=side_analysis(
                        db,self.replay_root,float(self.clock()),player,None,opponent_deck or None,
                        mode='player',algorithm=algorithm=='1',first=first,
                        digest=digest or None,scope=scope)
                from .statistics_card_names import index as name_index
                names=name_index(self.database.parent)
                def decorate(cards):
                    for card in cards:
                        info=names.get(str(card['code']))
                        card['name']=card_name(info) if info else ('#%d' % int(card['code']))
                for side in result['sides']:
                    for key in ('added','removed'):
                        decorate(side['observed'][key])
                    decorate(side['observed']['direct_side']['cards'])
                    decorate(side['observed']['unknown_outcome']['cards'])
                    decorate(side['algorithm']['cards'])
                    decorate(side['algorithm']['latent_cards'])
                    side['algorithm']['observed_only']=[
                        dict(code=code, name=(card_name(names.get(str(code))) if names.get(str(code)) else '#%d' % code))
                        for code in side['algorithm']['observed_only']]
                    for session in side['sessions']:
                        for key in ('added','removed'):
                            decorate(session[key])
                return result
            try:
                return self._cached_query_reply(('side-analysis',parsed.query),side_run,ttl=30.0)
            except ValueError as exc:
                return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','api','side-recent']:
            # 换备：当前构筑按对手场次取前 N，每个对手最近 M 场，先/后攻分列。
            from .statistics_side_recent import query as side_recent
            args=parse_qs(parsed.query,keep_blank_values=True)
            player=(args.get('player',[''])[0] or '').strip()
            digest=(args.get('digest',[''])[0] or '').strip()
            try:
                top=int(args.get('top',['6'])[0]);recent=int(args.get('recent',['5'])[0])
            except (TypeError,ValueError):
                return self._reply(400,json.dumps({'error':'换备参数无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            if not player or len(player)>128:
                return self._reply(400,json.dumps({'error':'需要玩家名称。'},ensure_ascii=False),'application/json; charset=utf-8')
            if digest and (len(digest)!=64 or any(char not in '0123456789abcdef' for char in digest)):
                return self._reply(400,json.dumps({'error':'来源编号无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            if not 1<=top<=12 or not 1<=recent<=10:
                return self._reply(400,json.dumps({'error':'换备范围无效。'},ensure_ascii=False),'application/json; charset=utf-8')
            def side_recent_run(db):
                result=side_recent(db,self.replay_root,float(self.clock()),player,
                                   digest=digest or None,top=top,recent=recent)
                if result is None:return None
                from .statistics_card_names import index as name_index
                names=name_index(self.database.parent)
                def decorate(cards):
                    for card in cards:
                        info=names.get(str(card['code']))
                        card['name']=card_name(info) if info else ('#%d' % int(card['code']))
                for opponent in result.get('opponents') or []:
                    for seat in ('first','second'):
                        decorate(opponent[seat]['added']);decorate(opponent[seat]['removed'])
                    for session in opponent.get('sessions') or []:
                        decorate(session['added']);decorate(session['removed'])
                return result
            try:
                return self._cached_query_reply(('side-recent',parsed.query),side_recent_run,ttl=30.0)
            except ValueError as exc:
                return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
        if parts==['statistics','api','sample'] or len(parts)==3 and parts[:2]==['statistics','sample']:
            from .statistics_samples import details
            args=parse_qs(parsed.query);player=args.get('player',[''])[0];digest=args.get('digest',[''])[0] if parts[1]=='api' else parts[2]
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN');result=details(db,self.replay_root,float(self.clock()),player,digest)
                    if result is None:return self._reply(404,'样本已失效、被排除或不属于当前榜单。')
                    detail,file=result
                    if parts[1]=='sample':
                        from urllib.parse import quote
                        filename=detail.get('download_filename')or f'sample-{digest[:16]}.yrp'
                        status,kind,_,headers=self._reply(200,'','application/octet-stream',{'Content-Disposition':f"attachment; filename=sample-{digest[:16]}.yrp; filename*=UTF-8''"+quote(filename),'Cache-Control':'no-store'})
                        return status,kind,file.read_bytes(),headers
                from .statistics_card_names import index
                names=index(self.database.parent)
                for card in detail['cards']:
                    info=names.get(str(card['code']),{});card['name']=card_name(info) if info else '卡名暂缺'
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
        if len(parts)==3 and parts[:2]==['statistics','replay']:
            from .share import safe_replay_file
            value=parts[2]
            if not value.isdigit() or len(value)>12:return self._reply(400,'录像编号无效。')
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    row=db.execute('SELECT id,digest,rel_path FROM replays WHERE id=?',(int(value),)).fetchone()
                if row is None:return self._reply(404,'录像不存在或已过期。')
                path=safe_replay_file(self.replay_root,row['rel_path'])
                if path is None or not path.is_file():return self._reply(404,'录像文件不可用。')
                blob=path.read_bytes()
                digest=str(row['digest'] or '')
                filename=f"replay-{int(row['id'])}-{digest[:12]}.yrp"
                status,kind,_,extra=self._reply(200,'','application/octet-stream',
                    {'Content-Disposition':f'attachment; filename="{filename}"','Cache-Control':'no-store'})
                return status,kind,blob,extra
            except (sqlite3.Error,OSError):return self._reply(503,'录像暂时不可用，请稍后重试。')
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
            if len(deck)>100 or len(series)>100 or any(len(v)>128 for v in deck+series):return self._reply(400,'筛选条件过长。')
            try:
                with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                    db.row_factory=sqlite3.Row;db.execute('BEGIN')
                    result=investment(db,self.replay_root,code,float(self.clock()),deck,series)
                return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
            except (sqlite3.Error,OSError):return self._reply(503,'统计暂时不可用，请稍后刷新。')
        if parts==['statistics','api','player-search']:
            from .statistics_dashboard import search_players
            text=parse_qs(parsed.query).get('q',[''])[0].strip()
            if not text or len(text)>128:return self._reply(400,'请输入玩家名称。')
            return self._query_reply(lambda db:search_players(db,self.replay_root,float(self.clock()),text))
        if parts in (['statistics','api','roster'],['statistics','api','build']):
            from .statistics_dashboard import query
            args=parse_qs(parsed.query)
            player=args.get('player',[None])[0] if parts[-1]=='build' else None
            if parts[-1]=='build' and (not player or len(player)>128):return self._reply(400,'需要玩家名称。')
            if player is None:
                return self._roster_reply()
            precomputed=self._precomputed_build(player)
            if precomputed is not None:
                return precomputed
            def build_with_names(db):
                result=query(db,self.replay_root,float(self.clock()),player)
                if result and player:
                    from .statistics_card_names import index
                    names=index(self.database.parent)
                    for card in result.get('cards',[]):
                        info=names.get(str(card['code']),{})
                        card['name']=card_name(info) if info else '卡名暂缺'
                return result
            return self._cached_query_reply(('deck-detail',player), build_with_names, ttl=300.0, stamp_seconds=86400)
        return self._reply(404,'找不到此统计页面。')

    def warmup(self):
        """后台预热最常用的统计查询，走真实 route，确保缓存键与用户请求完全一致。"""
        headers = {"Host": "127.0.0.1:8788",
                   "Cookie": f"qqstats_owner={self._session(int(self.clock()) + 600)}"}
        for path in ("/statistics/api/roster", "/statistics/api/deck-naming?scope=100", "/statistics/api/deck-naming/groups?limit=50"):
            try:
                self.route(path, headers, "127.0.0.1")
            except Exception:
                pass

    def _precomputed_roster(self):
        """Serve the roster built by tools/roster_precompute.py.

        Keeps the 30-90 s build and its 250-300 MB intermediate out of this
        process, so _query_compute_lock is not held and query 503s drop.
        """
        try:
            path = self.database.parent / 'cache' / 'roster.json'
            item = json.loads(path.read_text(encoding='utf8'))
            saved = float(item['saved'])
            if not 0.0 <= float(self.clock()) - saved <= PRECOMPUTED_ROSTER_SECONDS:
                return None
            if int(item['status']) != 200:
                return None
            body = str(item['body'])
            if not body or len(body) > 4 * 1024 * 1024:
                return None
            headers = dict(item.get('headers') or {})
            headers['X-Statistics-State'] = 'precomputed'
            headers['X-Statistics-Saved-At'] = str(int(saved))
            return 200, str(item['kind']), body.encode('utf8'), headers
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _precomputed_card_report(self, args):
        """Serve the default card-report views built by tools/precompute_card_report.py.

        低峰由独立进程算好、原子写盘；这里只读文件，命中时不进单 worker 队列。
        """
        scope = card_report_precomputed_scope(args)
        if scope is None:
            return None
        try:
            path = self.database.parent / 'cache' / ('card-report-%s-%s.json' % scope)
            item = json.loads(path.read_text(encoding='utf8'))
            saved = float(item['saved'])
            if not 0.0 <= float(self.clock()) - saved <= PRECOMPUTED_CARD_REPORT_SECONDS:
                return None
            if int(item['status']) != 200:
                return None
            body = str(item['body'])
            if not body or len(body) > 4 * 1024 * 1024:
                return None
            return self._reply(200, body, 'application/json; charset=utf-8',
                               {'X-Statistics-State': 'precomputed',
                                'X-Statistics-Saved-At': str(int(saved))})
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _precomputed_build(self, player):
        """Serve the build detail precomputed by tools/precompute_query_cache.py."""
        try:
            from .query_precompute import read_precomputed, cache_path, MISS
            latest = 0.0
            with closing(sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro',
                                         uri=True, timeout=2)) as db:
                row = db.execute('SELECT MAX(recorded_at) FROM replays WHERE focus_player=?',
                                 (player,)).fetchone()
                if row and row[0] is not None:
                    latest = float(row[0])
            payload = read_precomputed(self.database.parent, player, latest)
            if payload is MISS or not isinstance(payload, dict):
                return None
            saved = cache_path(self.database.parent, player).stat().st_mtime
            return self._reply(200, json.dumps(payload, ensure_ascii=False),
                               'application/json; charset=utf-8',
                               {'X-Statistics-State': 'precomputed',
                                'X-Statistics-Saved-At': str(int(saved))})
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            return None

    def _roster_reply(self, force=False, background=False):
        """榜首查询的唯一入口：route 与后台保活共用，保证缓存键一致。"""
        if not force:
            precomputed = self._precomputed_roster()
            if precomputed is not None:
                return precomputed
        from .statistics_dashboard import query
        return self._cached_query_reply(('roster',), lambda db:query(db,self.replay_root,float(self.clock()),None), ttl=300.0, stamp_seconds=86400, force=force, background=background)

    def keep_roster_warm(self):
        """后台保活让位于已请求的任务；按结果年龄与上次耗时退避。"""
        return self._roster_reply(force=getattr(self,'_refresh',None) is None, background=True)

    def _cached_query_reply(self,key,callback,ttl=10.0,stamp_seconds=30,force=False,background=False):
        if getattr(self,'_refresh',None) is not None and key and key[0] in ('roster','card-report','recordings','matrix','cards-extra','deck-detail'):
            from urllib.parse import parse_qs,urlencode
            # deck-detail 的 key[1] 是玩家名，不是查询串：不能按参数归一化，否则不同玩家会撞成同一个 key。
            if len(key)>1 and key[0]!='deck-detail':key=(key[0],urlencode(sorted(parse_qs(key[1],keep_blank_values=True).items()),doseq=True))
            from .statistics_roster_version import snapshot_version
            wait=4.0 if key[0]=='deck-detail' else 1.0
            if key[0]=='deck-detail':
                # 构筑页计算本身是亚秒级：不要排进全局 _query_compute_lock，
                # 否则会被 roster/matrix 的长计算堵到 8 秒超时。
                compute=lambda:self._query_reply(callback)
            else:
                compute=lambda:self._cached_query_reply_sync(key,callback,ttl,stamp_seconds,force)
            return self._refresh.get(key,compute,lambda:snapshot_version(self.database),ttl,background=background,wait=wait)
        return self._cached_query_reply_sync(key,callback,ttl,stamp_seconds,force)

    def _cached_query_reply_sync(self,key,callback,ttl=10.0,stamp_seconds=30,force=False):
        card_summary=bool(key and key[0]=='card-report')
        # Summary validity includes WAL, evidence and naming versions. The old
        # process-local cache only checked a coarse database mtime bucket.
        hit=None if force or card_summary else self._query_cache_hit(key,ttl,stamp_seconds)
        if hit is not None:return hit
        if not force and key and key[0]=='card-report':
            from .statistics_report_intermediate import response_get,response_identity
            try:hit=response_get(response_identity(self.database,key),ttl)
            except OSError:hit=None
            if hit is not None:return hit
        if not self._query_compute_lock.acquire(timeout=QUERY_WAIT_SECONDS):
            # The owner may have finished just as our wait expired.
            hit=None if force or card_summary else self._query_cache_hit(key,ttl,stamp_seconds)
            if card_summary and not force:
                try:hit=response_get(response_identity(self.database,key),ttl)
                except OSError:hit=None
            return hit if hit is not None else self._query_busy_reply()
        try:
            return self._cached_query_reply_locked(key,callback,ttl,stamp_seconds,force)
        finally:
            self._query_compute_lock.release()

    def _card_metrics_cache_get(self,key,ttl=CARD_METRICS_CACHE_SECONDS):
        now=time.monotonic()
        with self._card_metrics_lock:
            item=self._card_metrics_cache.get(key)
            if item is not None and 0.0<=now-item[0]<=ttl:
                return item[1]
            if item is not None:
                self._card_metrics_cache.pop(key,None)
        return None

    def _card_metrics_cache_put(self,key,reply,limit=32,max_bytes=12*1024*1024):
        body=reply[2] if len(reply)>2 else b''
        size=len(body) if isinstance(body,(bytes,bytearray)) else 0
        if size>max_bytes//4:return
        with self._card_metrics_lock:
            self._card_metrics_cache[key]=(time.monotonic(),reply,size)
            total=sum(item[2] for item in self._card_metrics_cache.values())
            while len(self._card_metrics_cache)>limit or (total>max_bytes and len(self._card_metrics_cache)>1):
                oldest=min(self._card_metrics_cache,key=lambda k:self._card_metrics_cache[k][0])
                total-=self._card_metrics_cache.pop(oldest)[2]

    def _query_busy_reply(self):
        return self._reply(503,json.dumps(dict(error='统计正在计算，请稍后重试。',
            code='query_busy',retry_after=3),ensure_ascii=False),
            'application/json; charset=utf-8',extra={'Retry-After':'3'})

    def _query_cache_hit(self,key,ttl,stamp_seconds):
        try:
            stamp=self._query_cache_stamp(key,stamp_seconds)
        except OSError:return None
        with self._query_cache_lock:
            hit=self._query_cache.get(key)
            if hit and hit[0]==stamp and time.monotonic()-hit[1]<=ttl:
                return hit[2]
        return None

    def _query_cache_stamp(self,key,stamp_seconds):
        if key and key[0] in ('roster','matrix'):
            from .statistics_roster_version import snapshot_version
            return snapshot_version(self.database)
        return int(self.database.stat().st_mtime_ns//(stamp_seconds*1_000_000_000))

    def _cached_query_reply_locked(self,key,callback,ttl=10.0,stamp_seconds=30,force=False):
        """相同条件 + 相同数据库文件版本时复用完整响应；DB 一写就失效。

        force=True 跳过命中直接重算并覆盖缓存（供后台保活续期，命中本身不会续期）。
        """
        card_summary=bool(key and key[0]=='card-report')
        if card_summary and not force:
            from .statistics_report_intermediate import response_get,response_identity
            try:hit=response_get(response_identity(self.database,key),ttl)
            except OSError:hit=None
            if hit is not None:return hit
        try:
            # 观战进程会持续写库，按文件 mtime 精确比对会让缓存永远失效；
            # 用 30 秒粒度做数据版本，最多容忍半分钟旧数据。
            stamp=self._query_cache_stamp(key,stamp_seconds)
        except OSError:
            return self._query_reply(callback)
        now=time.monotonic()
        with self._query_cache_lock:
            hit=self._query_cache.get(key)
            if hit and hit[0]==stamp and now-hit[1]<=ttl and not force and not card_summary:
                return hit[2]
        from .statistics_report_intermediate import response_identity,response_put
        try:disk_identity=response_identity(self.database,key)
        except OSError:disk_identity=None
        result=self._query_reply(callback)
        if key and key[0] in ('roster','matrix','card-report','recordings'):
            _trim_heap()
        try:
            if disk_identity is not None and disk_identity==response_identity(self.database,key):
                response_put(disk_identity,result)
        except OSError:pass
        if not card_summary and result[0]==200 and len(result[2]) <= QUERY_CACHE_MAX_BYTES:
            with self._query_cache_lock:
                self._query_cache[key]=(stamp,time.monotonic(),result)
                while (len(self._query_cache)>32 or
                       sum(len(entry[2][2]) for entry in self._query_cache.values()) > QUERY_CACHE_MAX_BYTES):
                    oldest=min(self._query_cache,key=lambda k:self._query_cache[k][1])
                    self._query_cache.pop(oldest,None)
        return result

    def _query_reply(self,callback):
        try:
            with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                db.row_factory=sqlite3.Row;db.execute('BEGIN');result=callback(db)
            return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
        except (sqlite3.Error,OSError):return self._reply(503,'统计暂时不可用，请稍后刷新。')

    def post(self,path,headers,client,body):
        # 网页默认仍只提供查询与下载；唯一例外是本机 owner 的“卡组命名/组合编辑”后台。
        # 要求：本机回环 + 本机 Host + owner cookie + 自定义请求头 + 同源 Origin + 有界请求体。
        parsed=urlsplit(path)
        if parsed.path not in ('/statistics/api/deck-naming','/statistics/api/deck-naming/groups'):
            return self._reply(403,'用户统计页面仅提供查询与下载，不支持管理操作。')
        if parsed.path == '/statistics/api/deck-naming':
            # 旧人工命名已整体下线：名字只由“组合编辑”的规则统一生成。
            return self._reply(410,'旧人工命名已停用；卡组名由组合编辑规则统一生成。')
        try:host=urlsplit('//'+headers.get('Host','')).hostname
        except ValueError:return self._reply(403,'本机入口地址无效。')
        try:local=ip_address(client).is_loopback
        except ValueError:local=False
        if (not local or host not in ('127.0.0.1','localhost','::1')
                or any(headers.get(k) for k in ('Forwarded','X-Forwarded-For','X-Forwarded-Host'))):
            return self._reply(403,'仅允许从本机入口访问。')
        if not self._authenticated(headers.get('Cookie')):return self._reply(403,'请使用本机“打开统计页面”入口。')
        if headers.get('X-Deck-Naming')!='1':return self._reply(403,'缺少本页操作标记。')
        if headers.get('Origin','')!='http://'+headers.get('Host',''):return self._reply(403,'来源校验失败。')
        limit=16384 if parsed.path.endswith('/groups') else 4096
        if len(body)>limit:return self._reply(400,'请求内容过大。')
        try:
            payload=json.loads(body.decode('utf-8'))
            if not isinstance(payload,dict):raise ValueError()
        except (UnicodeDecodeError,ValueError):
            return self._reply(400,'参数无效。')
        if parsed.path.endswith('/groups'):
            from .series_naming_admin import update_group
            try:
                with closing(sqlite3.connect(str(self.database),timeout=10)) as db:
                    db.row_factory=sqlite3.Row;db.execute('PRAGMA busy_timeout=5000')
                    result=update_group(db,self.database.parent,float(self.clock()),payload)
            except LookupError:return self._reply(404,'找不到这个命名组。')
            except ValueError as exc:return self._reply(400,json.dumps({'error':str(exc)},ensure_ascii=False),'application/json; charset=utf-8')
            except sqlite3.Error:return self._reply(409,'统计库正忙，请稍后重试。')
            except OSError:return self._reply(500,'命名组配置保存失败，请检查数据目录。')
            return self._reply(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')

    @staticmethod
    def _reply(status,body,content_type='text/plain; charset=utf-8',extra=None):
        headers={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' https://cdntx.moecube.com https://cdn.233.momobako.com data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}
        headers.update(extra or {})
        return status,content_type,body.encode('utf8'),headers
