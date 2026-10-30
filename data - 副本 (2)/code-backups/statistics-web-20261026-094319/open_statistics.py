"""本机打开已由固定watch启动的统计页面；不启动任何观战进程。"""
import json
import webbrowser
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError,URLError


def main():
    root=Path(__file__).resolve().parent
    cfg=json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
    data=Path(cfg.get('data_dir') or root/'data');port=int((cfg.get('share') or {}).get('bind_port',8787))
    base=f'http://127.0.0.1:{port}'
    try:
        with urlopen(base+'/health',timeout=3) as response:health=json.load(response)
        if not health.get('statistics_portal'):raise ValueError('pending')
        from bot.statistics_portal import owner_secret
        path=data/'statistics-owner.key'
        if not path.is_file():raise ValueError('pending')
        secret=owner_secret(path)
    except (OSError,ValueError,URLError,HTTPError):
        print('统计页面尚未加载。请等待当前录制结束，再通过PyCharm固定watch配置正常启动新版本。')
        return 1
    webbrowser.open(base+'/statistics/access/'+secret)
    print('已打开本机统计页面。')
    return 0


if __name__=='__main__':raise SystemExit(main())
