"""Bounded, nonblocking official rank checks for visible room players only.

The public list endpoint exposes only 100 players. This is deliberately NOT a
complete top-200 roster, and does not change the statistics population.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from .arena_profile import fetch_profile,parse_profile
from .top100 import error_category


class RankWindow:
    def __init__(self,limit=200,fetch=None,clock=None,executor=None):
        if type(limit) is not int or not 100<limit<=200:raise ValueError('invalid automatic rank limit')
        self.limit=limit;self.fetch=fetch or fetch_profile;self.clock=clock or time.time
        self.executor=executor or ThreadPoolExecutor(max_workers=4,thread_name_prefix='official-room-rank')
        self.pending={};self.cache={};self.retry_at={};self.errors=0;self.last_error=None;self.closed=False
        self.pause_until=0.0;self.failure_threshold=8;self.cooldown=120.0;self.failure_retry=300.0

    def update(self,players,known,updated_at,stale):
        now=self.clock();players=set(players)
        # Never let delayed requests count as fresh after an outage or stale list.
        for name,(future,started) in list(self.pending.items()):
            if not future.done():continue
            del self.pending[name]
            try:
                payload=future.result()
                if isinstance(payload,list):payload=payload[0] if len(payload)==1 else None
                # The official endpoint normally omits identity fields. Its
                # exact-name HTTPS request is bound to this future/cache key.
                if not isinstance(payload,dict) or ('username' in payload and payload['username']!=name):raise ValueError('rank_identity_mismatch')
                profile=parse_profile(payload,name,started)
                self.cache[name]=(profile['rank'],started)
                self.errors=0;self.last_error=None;self.retry_at[name]=now+120
            except Exception as error:
                self.cache.pop(name,None);self.retry_at[name]=now+self.failure_retry
                if self.errors>=self.failure_threshold:self.pause_until=now+self.cooldown
                self.errors+=1;self.last_error=error_category(error)
        self.cache={name:value for name,value in self.cache.items() if name in players and 0<=now-value[1]<120}
        self.retry_at={name:value for name,value in self.retry_at.items() if name in players}
        fresh=not stale and updated_at is not None and 0<=now-updated_at<=600
        if not fresh or self.closed:return {}
        # At most 16 pending requests and four workers. Retries rotate fairly;
        # no request blocks the observation/recording event loop.
        if now<self.pause_until:return {name:value for name,value in self.cache.items() if name not in known and 0<value[0]<=self.limit}
        candidates=sorted((n for n in players if n not in known and n not in self.pending and now>=self.retry_at.get(n,0)),key=lambda n:(self.retry_at.get(n,0),n))
        budget=8 if self.errors>=4 else 16
        for name in candidates[:max(0,budget-len(self.pending))]:
            self.pending[name]=(self.executor.submit(self.fetch,name),now)
            self.retry_at[name]=now+120
        return {name:value for name,value in self.cache.items() if name not in known and 0<value[0]<=self.limit}

    def status(self):
        return dict(rank_samples=[dict(player=name,rank=value[0],checked_at=value[1]) for name,value in list(self.cache.items())[:256] if 0<=self.clock()-value[1]<120],
                    automatic_rank_limit=self.limit,automatic_rank_mode='visible_players_official_rank',
                    complete_top200_roster=False,rank_queries_pending=len(self.pending),rank_pause_seconds=max(0,int(self.pause_until-self.clock())),
                    rank_checks_cached=len(self.cache),rank_consecutive_failures=self.errors,rank_last_error=self.last_error)

    def close(self):
        self.closed=True
        for future,_ in self.pending.values():future.cancel()
        self.executor.shutdown(wait=False,cancel_futures=True)
