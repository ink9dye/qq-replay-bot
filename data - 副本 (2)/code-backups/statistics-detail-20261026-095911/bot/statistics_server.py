"""本机统计网页服务：不启动观战、QQ或数据库迁移。"""
import argparse,json,os,sys,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from .share import ReplayHTTPServer
from .statistics_portal import StatisticsPortal,owner_secret


def make_handler(portal):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def do_GET(self):
            if self.path=='/health':
                result=portal._reply(200,json.dumps(dict(service='qq-statistics-web',status='running',pid=os.getpid(),
                    read_only_queries=True,starts_watch=False,version=1)),'application/json; charset=utf-8')
            elif self.path.split('?',1)[0].startswith('/statistics'):
                result=portal.route(self.path,self.headers,self.client_address[0])
            else:result=portal._reply(404,'找不到此统计页面。')
            status,kind,body,headers=result
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            for key,value in headers.items():self.send_header(key,value)
            self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
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
        try:server.serve_forever()
        finally:server.server_close()
        return 0
    except (OSError,ValueError):
        print('本机统计网页未能启动：端口不可用、资料缺失或入口文件异常。',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
