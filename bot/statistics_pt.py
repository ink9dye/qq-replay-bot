"""Bounded, in-memory public recent PT history for the private interactive view."""
import threading
import time
import json
from collections import OrderedDict
from datetime import datetime
from .player_history import API, CST, athletic_stats
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlencode


def fetch_history(endpoint, params, timeout):
    # Public official endpoint only. System proxy stalls on this host; keep TLS validation.
    if endpoint!='history':raise ValueError('invalid_endpoint')
    request=Request(API+endpoint+'?'+urlencode(params),headers={'User-Agent':'qq-replay-bot/player-history'})
    with build_opener(ProxyHandler({})).open(request,timeout=timeout) as response:
        body=response.read(8*1024*1024+1)
    if len(body)>8*1024*1024:raise ValueError('history_too_large')
    result=json.loads(body)
    if not isinstance(result,dict):raise ValueError('invalid_history')
    return result


class InteractiveHistory:
    def __init__(self, fetch=None, now=None):
        self.fetch=fetch or fetch_history
        self.now=now or (lambda:datetime.now(CST))
        self.cache=OrderedDict()
        self.lock=threading.Lock()
        self.player_locks={}

    def query(self, player):
        if not isinstance(player,str) or not player.strip() or len(player)>100:
            raise ValueError('invalid_player')
        with self.lock:
            query_lock=self.player_locks.setdefault(player,threading.Lock())
        if not query_lock.acquire(timeout=26):
            return dict(status='busy',message='历史查询正在进行，请稍后重试。',games=[])
        try:
            now=self.now().astimezone(CST);key=(player,'recent')
            cached=self.cache.get(key)
            if cached and time.monotonic()-cached[0]<(60 if cached[1]['status']=='ok' else 15):
                self.cache.move_to_end(key)
                return cached[1]
            # 与 APK 同一口径：一次请求取最近 100 场，不再按自然月翻页。
            try:
                payload=self.fetch('history',dict(username=player,type=1,page_num=100),12)
                batch=payload.get('data')
                if not isinstance(batch,list):raise ValueError('invalid_history')
                stats=athletic_stats(player,batch,now);games=[]
                for n,(stamp,before,after,row) in enumerate(stats['games'],1):
                    opponent=row['usernameb'] if row['usernamea']==player else row['usernamea']
                    winner=row.get('winner')
                    outcome='win' if winner==player else 'draw' if winner=='none' else 'loss' if winner==opponent else 'unknown'
                    games.append(dict(number=n,recorded_at=stamp.timestamp(),opponent=opponent,
                                      before=before,after=after,delta=round(after-before,4),outcome=outcome))
                result=dict(status='ok',player=player,window='recent',window_label=f'最近{stats["count"]}场',
                            generated_at=now.timestamp(),games=games,count=stats['count'],wins=stats['wins'],
                            draws=stats['draws'],win_rate=stats['rate'],
                            initial_points=stats['points'][0] if games else None,
                            final_points=stats['points'][-1] if games else None,
                            semantics='official_recent_athletic_matches_not_recorded_small_games')
            except (TimeoutError,OSError):
                result=dict(status='unavailable',message='公开历史接口暂不可用或超时，暂无可核对的PT记录。',games=[])
            except (ValueError,TypeError,KeyError,OverflowError):
                result=dict(status='incomplete',message='未能取得有效历史，暂不展示不完整曲线。',games=[])
            self.cache[key]=(time.monotonic(),result)
            while len(self.cache)>16:self.cache.popitem(last=False)
            return result
        finally:query_lock.release()
