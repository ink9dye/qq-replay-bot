"""Read-only, bounded small-game counts; no replay parsing or per-player network fanout."""
import json
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
 SELECT digest, MIN(COALESCE(saved_at,recorded_at)) AS received_at, players_json
 FROM replays WHERE recorded_at>=? AND recorded_at<=? GROUP BY digest
) '''


def _read_json(path,limit):
    try:
        with Path(path).open('rb') as f:raw=f.read(limit+1)
        if len(raw)>limit:return {}
        value=json.loads(raw.decode('utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def current_ranks(db,data_dir,now):
    """Reuse fresh room checks and the official top100; never pretend this is a complete top200."""
    ranks={};times=[]
    capture=_read_json(Path(data_dir)/'capture-status.json',256*1024)
    if 0<=now-float(capture.get('updated_at') or 0)<=90:
        for row in (capture.get('rank_samples') or [])[:256]:
            rank=row.get('rank');checked=row.get('checked_at')
            if type(rank) is int and rank>=0 and isinstance(checked,(int,float)) and 0<=now-checked<120:
                ranks[str(row['player'])]=rank;times.append(checked)
    try:
        state=db.execute('SELECT updated_at,last_error FROM stat_roster_state WHERE singleton=1').fetchone()
        if state and state[0] is not None and not state[1] and 0<=now-state[0]<=600:
            members=db.execute('SELECT player,rank FROM stat_current_top100').fetchall()
            if len(members)==100:
                ranks.update({name:rank for name,rank in members});times.append(state[0])
    except sqlite3.Error:pass
    return ranks,min(times) if times else None


def membership(players,ranks,limit,internal):
    states=[None if p not in ranks else 0<ranks[p]<=limit for p in players]
    if internal:
        if len(set(players))!=2:return None
        if False in states:return False
        return True if all(s is True for s in states) else None
    if True in states:return True
    return None if not states or None in states else False


class GameCounter:
    def __init__(self,database,data_dir,clock=time.time,rank_provider=current_ranks):
        self.database=Path(database);self.data_dir=Path(data_dir);self.clock=clock
        self.rank_provider=rank_provider;self._lock=threading.Lock();self._cache=OrderedDict()

    def query(self,keyword='',limit=None,internal=False):
        now=float(self.clock())
        midnight=datetime.fromtimestamp(now,TZ).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        key=(keyword,limit,internal,midnight)
        with self._lock:
            hit=self._cache.get(key)
            if hit and 0<=now-hit[0]<30:return hit[1]
            with closing(sqlite3.connect(self.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
                db.execute('PRAGMA cache_size=-2048')
                deadline=time.monotonic()+3
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
                db.execute('BEGIN')
                params=[now-RETENTION_SECONDS,now]
                tail=''
                if keyword:
                    tail=' WHERE EXISTS (SELECT 1 FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build WHERE v.digest=games.digest AND instr(lower(COALESCE(b.deck_type,\'\')),lower(?))>0)'
                    params.append(keyword)
                unknown=0;rank_at=None
                if limit is None:
                    count,today=db.execute(BASE+'SELECT COUNT(*),COALESCE(SUM(received_at>=? AND received_at<=?),0) FROM games'+tail,
                        params[:2]+[midnight,now]+params[2:]).fetchone()
                else:
                    ranks,rank_at=self.rank_provider(db,self.data_dir,now)
                    count=today=0
                    for received,raw in db.execute(BASE+'SELECT received_at,players_json FROM games',params):
                        try: players=json.loads(raw)
                        except (ValueError,TypeError):players=[]
                        if not isinstance(players,list):players=[]
                        match=membership(players,ranks,limit,internal)
                        if match is None:unknown+=1
                        elif match:
                            count+=1;today+=int(midnight<=received<=now)
                result=dict(count=count,today=today,unknown=unknown,rank_at=rank_at,at=now,
                            keyword=keyword,limit=limit,internal=internal)
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
    if r['limit']:
        if r['unknown']:lines.append(f"另有 {r['unknown']} 个小局缺少新鲜名次，暂无法给出完整当前排名范围总数；今日数同样仅含已确认部分。")
        if r['rank_at']:lines.append('名次最早采样：'+datetime.fromtimestamp(r['rank_at'],TZ).strftime('%m-%d %H:%M:%S'))
        lines.append('复用房间玩家名次与当前榜单，不逐个联网补查历史玩家。')
    if r['keyword']:lines.append('按已识别卡组名作文字包含匹配；双方都匹配也只计一个小局。')
    lines.append('统计截至北京时间 '+datetime.fromtimestamp(r['at'],TZ).strftime('%m-%d %H:%M:%S'))
    lines.append('按录像指纹去重；仅统计当前保留范围（14天），不是终身累计；已过期删除的不计。')
    return '\n'.join(lines)
