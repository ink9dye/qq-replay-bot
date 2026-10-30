"""Request-local side populations; official rank cache contains no game facts."""
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor, wait
from threading import Lock
import time
from .arena_profile import fetch_profile,parse_profile

_CURRENT=ContextVar('card_side_population',default=None)
_POOL=ThreadPoolExecutor(max_workers=4,thread_name_prefix='statistics-rank')
_LOCK=Lock();_CACHE={};_PENDING={};_RETRY={}


def parse_ranges(args):
    result=[]
    for key,default in [('self_range','100'),('opponent_range','all')]:
        values=args.get(key,[default])
        if len(values)!=1 or values[0] not in ('100','200','all'):raise ValueError('双方范围只支持前100、前200或全部，且不可重复。')
        result.append(values[0])
    if args.get('internal',['0'])==['1']:
        if any(key in args and value!='100' for key,value in zip(('self_range','opponent_range'),result)):
            raise ValueError('前100内战要求双方范围均为前100。')
        result=['100','100']
    return tuple(result)


def current():return _CURRENT.get()


@contextmanager
def population_scope(own='100',opponent='all'):
    if own not in ('100','200','all') or opponent not in ('100','200','all'):raise ValueError('invalid_side_range')
    token=_CURRENT.set(dict(own=own,opponent=opponent,unknown=set(),pending=set()))
    try:yield _CURRENT.get()
    finally:_CURRENT.reset(token)


@contextmanager
def investment_scope():
    scope=current()
    if scope is None:yield;return
    # Latest build per person must not be replaced by an older matching opponent.
    token=_CURRENT.set(dict(scope,opponent='all'))
    try:yield
    finally:_CURRENT.reset(token)


def scoped_query(ranges,call):
    with population_scope(*ranges) as scope:
        result=call()
        result['range_filter_version']=1
        result['rank_scope']=dict(self_range=scope['own'],opponent_range=scope['opponent'],
            unknown_rank_players=len(scope['unknown']),pending_rank_players=len(scope['pending']),
            complete=not scope['unknown'],rank_source='official_top100_and_per_player',rank_max_age_seconds=120)
        return result


def ranks_for(players,now):
    """Bounded queued lookups, at most one second waiting; fail closed, no disk."""
    now=time.time()  # Profile freshness uses the actual clock, not replay timestamps.
    with _LOCK:
        for name,(future,started) in list(_PENDING.items()):
            if not future.done():continue
            del _PENDING[name]
            try:
                payload=future.result()
                if isinstance(payload,list):payload=payload[0] if len(payload)==1 else None
                if not isinstance(payload,dict) or ('username' in payload and payload['username']!=name):raise ValueError()
                _CACHE[name]=(parse_profile(payload,name,started)['rank'],started)
                _RETRY[name]=now+120
            except Exception:_CACHE.pop(name,None);_RETRY[name]=now+60
        for name in list(_CACHE):
            if not 0<=now-_CACHE[name][1]<120:del _CACHE[name]
        for name in list(_RETRY):
            if now-_RETRY[name]>600:del _RETRY[name]
        futures=[]
        for name in sorted(players,key=lambda n:(_RETRY.get(n,0),n)):
            if name in _CACHE:continue
            if name not in _PENDING and now>=_RETRY.get(name,0) and len(_PENDING)<32:
                _PENDING[name]=(_POOL.submit(fetch_profile,name),now);_RETRY[name]=now+120
            if name in _PENDING:futures.append(_PENDING[name][0])
        if len(_CACHE)>10000:
            for name in sorted(_CACHE,key=lambda n:_CACHE[n][1])[:len(_CACHE)-10000]:del _CACHE[name]
        result={n:_CACHE[n][0] for n in players if n in _CACHE}
    if futures:
        wait(futures,timeout=1)
        # Harvest finished results without scheduling more requests this time.
        with _LOCK:
            for name in players:
                item=_PENDING.get(name)
                if not item or not item[0].done():continue
                future,started=item;del _PENDING[name]
                try:
                    payload=future.result()
                    if isinstance(payload,list):payload=payload[0] if len(payload)==1 else None
                    if not isinstance(payload,dict) or ('username' in payload and payload['username']!=name):raise ValueError()
                    rank=parse_profile(payload,name,started)['rank']
                    if 0<=time.time()-started<120:_CACHE[name]=(rank,started);result[name]=rank
                except Exception:_RETRY[name]=now+60
    return result


def filter_rows(db,rows,state,now):
    scope=current()
    if scope is None:return rows
    known={r['player']:r['rank'] for r in state['members']}
    opponents={}
    for r in db.execute('SELECT digest,player FROM stat_views'):
        opponents.setdefault(r['digest'],set()).add(r['player'])
    candidates=set()
    for row in rows:
        if scope['own']=='200':candidates.add(row['player'])
        if scope['opponent']=='200':candidates.update(opponents.get(row['digest'],set())-{row['player']})
    candidates-=known.keys()
    extra=ranks_for(candidates,now) if candidates else {}
    scope['unknown'].update(candidates-extra.keys())
    with _LOCK:scope['pending'].update(candidates&_PENDING.keys())
    def accept(name,choice):
        if choice=='all':return True
        if choice=='100':return name in known
        value=known.get(name,extra.get(name))
        return value is not None and 0<value<=200
    result=[]
    for row in rows:
        other=opponents.get(row['digest'],set())-{row['player']}
        if not accept(row['player'],scope['own']):continue
        if scope['opponent']!='all' and (len(other)!=1 or not accept(next(iter(other)),scope['opponent'])):continue
        row['rank']=known.get(row['player'],extra.get(row['player']))
        result.append(row)
    return result
