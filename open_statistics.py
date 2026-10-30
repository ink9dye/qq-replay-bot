"""本机打开已由固定watch启动的统计页面；不启动任何观战进程。"""
import json
import webbrowser
import subprocess,sys,time
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError,URLError


def main():
    root=Path(__file__).resolve().parent
    cfg=json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
    data=Path(cfg.get('data_dir') or root/'data');port=8788
    base=f'http://127.0.0.1:{port}'
    try:
        def ready():
            try:
                with urlopen(base+'/health',timeout=1) as response:health=json.load(response)
                return health.get('service')=='qq-statistics-web' and health.get('status')=='running'
            except (OSError,ValueError):return False
        if not ready():
            subprocess.Popen([sys.executable,'-m','bot.statistics_server','--root',str(root)],cwd=root,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for attempt in range(20):
                if ready():break
                time.sleep(.25)
            else:raise ValueError('unavailable')
        from bot.statistics_portal import owner_secret
        path=data/'statistics-owner.key'
        if not path.is_file():raise ValueError('pending')
        secret=owner_secret(path)
    except (OSError,ValueError,URLError,HTTPError):
        print('本机统计页面暂不可用，请检查统计服务端口或资料。观战进程未改动。')
        return 1
    webbrowser.open(base+'/statistics/access/'+secret)
    print('已打开本机统计页面。')
    return 0


if __name__=='__main__':raise SystemExit(main())
