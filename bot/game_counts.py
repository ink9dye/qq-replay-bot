"""Read-only, bounded small-game counts; no replay parsing or per-player network fanout."""
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from datetime import datetime,timezone,timedelta
from pathlib import Path
from retention_policy import RETENTION_SECONDS

TZ=timezone(timedelta(hours=8))
BASE='''WITH games AS (
 SELECT digest, MIN(COALESCE(saved_at,recorded_at)) AS received_at
 FROM replays WHERE recorded_at>=? AND recorded_at<=? GROUP BY digest
) '''


def game_digests(views):
    return {row['digest'] for row in views}


def ranked_counts(db, data_dir, now, midnight, limit, internal):
    from .statistics_rank_scope import population_scope
    from .top100 import eligible_views

    db.row_factory = sqlite3.Row
    rank_range = str(limit)
    opponent_range = rank_range if internal else 'all'
    with population_scope(rank_range, opponent_range, track_unresolved_games=limit == 200) as scope:
        basis = eligible_views(db, Path(data_dir) / 'replays', now,
                               annotate_series=False, with_diagnostics=False)
    roster = basis['roster']
    if limit == 100 and not roster['available']:
        raise ValueError('top100 roster unavailable')
    views = basis['views']
    digests = game_digests(views)
    today = sum(digest in digests and midnight <= received <= now
                for digest, received in db.execute(
                    BASE + 'SELECT digest,received_at FROM games',
                    (now - RETENTION_SECONDS, now)))
    return dict(count=len(digests), views=len(views), today=today,
                unknown=len(scope['unresolved_games'] - digests) if limit == 200 else 0,
                roster_stale=limit == 100 and roster['stale'])


class GameCounter:
    def __init__(self,database,data_dir,clock=time.time):
        self.database=Path(database);self.data_dir=Path(data_dir);self.clock=clock
        self._lock=threading.Lock();self._cache=OrderedDict()

    def query(self,keyword='',limit=None,internal=False):
        now=float(self.clock())
        midnight=datetime.fromtimestamp(now,TZ).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        key=(keyword,limit,internal,midnight)
        with self._lock:
            hit=self._cache.get(key)
            if hit and 0<=now-hit[0]<30:return hit[1]
            with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
                db.execute('PRAGMA cache_size=-2048')
                deadline=time.monotonic()+(60 if limit is not None else 3)
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
                db.execute('BEGIN')
                if limit is not None:
                    result = ranked_counts(db, self.data_dir, now, midnight, limit, internal)
                else:
                    params=[now-RETENTION_SECONDS,now,midnight,now]
                    tail=''
                    if keyword:
                        tail=(" WHERE EXISTS (SELECT 1 FROM stat_views v JOIN stat_builds b "
                              "ON b.fingerprint=v.build WHERE v.digest=games.digest "
                              "AND instr(lower(COALESCE(b.deck_type,'')),lower(?))>0)")
                        params.append(keyword)
                    count,today=db.execute(
                        BASE+'SELECT COUNT(*),COALESCE(SUM(received_at>=? AND received_at<=?),0) FROM games'+tail,
                        params).fetchone()
                    result=dict(count=count,today=today,unknown=0)
                result.update(at=now,keyword=keyword,limit=limit,internal=internal)
            self._cache[key]=(now,result);self._cache.move_to_end(key)
            while len(self._cache)>16:self._cache.popitem(last=False)
            return result


def message(service,command):
    counter=getattr(service,'_game_counter',None)
    if counter is None:
        counter=GameCounter(service.store.path,service.data_dir,service.clock);service._game_counter=counter
    try:r=counter.query(command.get('keyword',''),command.get('limit'),command.get('internal',False))
    except (sqlite3.Error,OSError,ValueError):return '小局统计暂时不可用或查询超时，请稍后重试。'
    title='小局统计'
    if r['keyword']:title+='｜卡组名包含“'+r['keyword'].replace('[','［').replace(']','］')+'”'
    if r['limit']:title+=f"｜当前前{r['limit']}"+('内战（双方）' if r['internal'] else '参与（至少一方）')
    qualifier='已确认至少 ' if r['unknown'] else ''
    lines=[title,f"当前保留：{qualifier}{r['count']} 个小局",f"今天首次入库：{r['today']} 个小局（北京时间00:00起）"]
    if 'views' in r:
        lines.append(f"对应 {r['views']} 个玩家视角；一局两边都符合时计 2 个视角。")
        if r['unknown']:
            lines.append(f"另有 {r['unknown']} 个符合录像资格的小局因名次未知暂未计入。")
        if r['roster_stale']:
            lines.append('前100榜单快照可能已过期，当前按最近一次有效榜单统计。')
        lines.append('与网页共用对局资格和名次判断；QQ 此处未筛卡组或先后攻。')
    if r['keyword']:lines.append('按已识别卡组名作文字包含匹配；双方都匹配也只计一个小局。')
    lines.append('统计截至北京时间 '+datetime.fromtimestamp(r['at'],TZ).strftime('%m-%d %H:%M:%S'))
    lines.append('按录像指纹去重；仅统计当前保留范围（28天），不是终身累计；已过期删除的不计。')
    return '\n'.join(lines)
