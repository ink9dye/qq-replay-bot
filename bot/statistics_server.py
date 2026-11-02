"""本机统计网页服务：不启动观战、QQ或数据库迁移。"""
import argparse,json,os,sys,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from .share import ReplayHTTPServer
from .statistics_portal import StatisticsPortal,owner_secret



_access_lock=threading.Lock()


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


def make_handler(portal):
    class Handler(BaseHTTPRequestHandler):
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
                if not 0<length<=2048:raise ValueError()
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
    return ReplayHTTPServer(('127.0.0.1',port),make_handler(portal))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--port',type=int,default=8788)
    args=parser.parse_args()
    try:
        server=create_server(args.root,args.port)
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
