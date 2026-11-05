"""本机统计网页服务：不启动观战、QQ或数据库迁移。"""
import argparse,json,os,sys,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from .share import ReplayHTTPServer
from .statistics_portal import StatisticsPortal,owner_secret



_access_lock=threading.Lock()


class StatisticsHTTPServer(ReplayHTTPServer):
    def server_close(self):
        super().server_close()
        app=getattr(self,'share_app',None)
        if app is not None:
            app.store.close()


def _visitor_ip(handler):
    for name in ('CF-Connecting-IP','True-Client-IP','X-Real-IP'):
        value=(handler.headers.get(name) or '').strip()
        if value:return value[:64]
    forwarded=(handler.headers.get('X-Forwarded-For') or '').strip()
    if forwarded:return forwarded.split(',',1)[0].strip()[:64]
    return (handler.client_address[0] if handler.client_address else '')[:64]


def _safe_path(path):
    raw=str(path or '').split('?',1)[0]
    if raw.startswith('/s/'):return '/s/<redacted>'
    if raw.startswith('/stats/'):return '/stats/<redacted>'
    return raw[:300]


def _log_access(handler,portal,status,kind):
    raw=str(handler.path or '').split('?',1)[0]
    if raw in ('/health','/favicon.ico'):
        return
    try:
        record=dict(at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),ip=_visitor_ip(handler),
                    method=handler.command,path=_safe_path(handler.path),status=int(status),
                    kind=str(kind or '')[:80],country=(handler.headers.get('CF-IPCountry') or '')[:8],
                    ua=(handler.headers.get('User-Agent') or '')[:300],
                    ref=(handler.headers.get('Referer') or '')[:300])
        log=portal.database.parent/'logs'/'web-access.jsonl'
        line=json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n'
        with _access_lock:
            log.parent.mkdir(parents=True,exist_ok=True)
            with log.open('a',encoding='utf-8') as handle:
                handle.write(line)
    except OSError:
        pass


def make_handler(portal, share_app=None):
    from .share import make_handler as share_handler
    base = share_handler(share_app) if share_app is not None else BaseHTTPRequestHandler
    class Handler(base):
        protocol_version='HTTP/1.1'
        def _compress(self,kind,body,headers):
            """P11：文本/JSON 响应按需 gzip（>1KB 且客户端接受），不改任何统计口径。"""
            if not isinstance(body,bytes) or len(body)<1024:return body,headers
            if not (kind.startswith('text/') or kind.startswith('application/json')):return body,headers
            if 'gzip' not in (self.headers.get('Accept-Encoding','')or'').lower():return body,headers
            import gzip
            result=dict(headers);result['Content-Encoding']='gzip';result['Vary']='Accept-Encoding'
            return gzip.compress(body,6),result
        def _log_error(self,exc):
            import traceback
            try:
                log=portal.database.parent/'logs'/'web-errors.log'
                log.parent.mkdir(parents=True,exist_ok=True)
                with log.open('a',encoding='utf-8') as handle:
                    handle.write(time.strftime('%Y-%m-%dT%H:%M:%S')+'\t'+traceback.format_exc()+'\n')
            except OSError:pass
        def do_GET(self):
            if share_app is not None and self.path.split('?',1)[0].startswith('/s/'):
                return super().do_GET()
            try:
                if self.path=='/health':
                    result=portal._reply(200,json.dumps(dict(service='qq-statistics-web',status='running',pid=os.getpid(),
                        read_only_queries=True,starts_watch=False,version=3,features=['roster','groups','investment','cards','matrix','local_card_library','current_player_investment','build_detail','inferred_side'])),'application/json; charset=utf-8')
                elif self.path.split('?',1)[0].startswith('/statistics'):
                    result=portal.route(self.path,self.headers,self.client_address[0])
                else:result=portal._reply(404,'找不到此统计页面。')
            except Exception as exc:
                self._log_error(exc)
                result=portal._reply(500,'服务器内部错误，请稍后重试。')
            status,kind,body,headers=result
            body,headers=self._compress(kind,body,headers)
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            for key,value in headers.items():self.send_header(key,value)
            self.end_headers();self.wfile.write(body)
            _log_access(self,portal,status,kind)
        def log_message(self,*args):pass
        def do_POST(self):
            self.close_connection=True
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:raise ValueError()
                result=portal.post(self.path,self.headers,self.client_address[0],self.rfile.read(length))
            except ValueError:result=portal._reply(400,'请求内容无效。')
            except Exception as exc:
                self._log_error(exc)
                try:result=portal._reply(500,'服务器内部错误，请稍后重试。')
                except Exception:result=portal._reply(500,'服务器内部错误。')
            status,kind,body,headers=result
            body,headers=self._compress(kind,body,headers)
            self.send_response(status)
            self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            for key,value in headers.items():self.send_header(key,value)
            self.end_headers();self.wfile.write(body)
            _log_access(self,portal,status,kind)
    return Handler


def create_server(root,port=8788):
    root=Path(root).resolve();cfg=json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
    data=Path(cfg.get('data_dir') or root/'data');replays=Path(cfg.get('replay_dir') or data/'replays')
    if not (data/'bot.sqlite3').is_file():raise ValueError('statistics_database_missing')
    portal=StatisticsPortal(data/'bot.sqlite3',replays,time.time,owner_secret(data/'statistics-owner.key'))
    from .device_dashboard import DeviceDashboard
    remote=cfg.get('remote') or (cfg.get('watch') or {}).get('remote') or {}
    portal.device_dashboard=DeviceDashboard(data/'bot.sqlite3',replays,str(remote.get('token') or ''))
    from .share import ShareApp
    from .store import Store
    share_app=ShareApp(Store(data/'bot.sqlite3',readonly=True,migrate=False),replays,time.time)
    server=StatisticsHTTPServer(('127.0.0.1',port),make_handler(portal,share_app))
    server.portal=portal
    server.share_app=share_app
    return server


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--port',type=int,default=8788)
    args=parser.parse_args()
    try:
        server=create_server(args.root,args.port)
        def _warm():
            time.sleep(5)
            server.portal.warmup()
        threading.Thread(target=_warm,daemon=True).start()
        def _roster_keepwarm():
            # roster 缓存 ttl 300 秒，命中不续期；每 240 秒强制重算一次，留 60 秒余量。
            # 冷查询在 2C2G + 8 路录制下要 4-9 秒，放到后台线程里跑，用户侧只吃缓存。
            while True:
                time.sleep(240)
                try:
                    server.portal.keep_roster_warm()
                except Exception:
                    pass
        threading.Thread(target=_roster_keepwarm,daemon=True).start()
        def _side_loop():
            import json as _json
            while True:
                time.sleep(300)
                try:
                    status_path = server.portal.database.parent / 'capture-status.json'
                    status = _json.loads(status_path.read_text(encoding='utf-8'))
                    if int(status.get('active_observations') or 0) != 0:
                        continue
                    if time.time() - float(status.get('updated_at') or 0) > 180:
                        continue
                    from .side_inference import run as side_run
                    side_run(args.root, limit=20)
                except Exception:
                    pass

        threading.Thread(target=_side_loop, daemon=True).start()
        from .draw_analysis_worker import start_worker
        analysis_stop=start_worker(args.root)
        try:server.serve_forever()
        finally:
            analysis_stop.set()
            server.server_close()
        return 0
    except (OSError,ValueError):
        print('本机统计网页未能启动：端口不可用、资料缺失或入口文件异常。',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
