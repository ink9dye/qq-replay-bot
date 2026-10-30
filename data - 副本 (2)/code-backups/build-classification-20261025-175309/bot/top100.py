"""官方当前竞技前100；成功全量替换，失败保留最后一次完整名单。"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE='https://sapi.moecube.com:444/ygopro/arena/users?o=pt'
REFRESH_SECONDS=120
STALE_SECONDS=600
SCHEMA='''
CREATE TABLE IF NOT EXISTS stat_roster_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL DEFAULT 0,
 updated_at REAL, last_attempt REAL, last_error TEXT, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_current_top100 (
 player TEXT PRIMARY KEY, rank INTEGER NOT NULL UNIQUE CHECK(rank BETWEEN 1 AND 100),
 points REAL NOT NULL, official_id TEXT
);
'''


def initialize(db):
    db.executescript(SCHEMA)
    db.execute('INSERT OR IGNORE INTO stat_roster_state(singleton,source) VALUES(1,?)',(SOURCE,))
    db.commit()


def parse(payload):
    if not isinstance(payload,list) or len(payload)!=100:
        raise ValueError('incomplete_roster')
    result=[];names=set();previous=math.inf
    for rank,row in enumerate(payload,1):
        if not isinstance(row,dict):raise ValueError('invalid_roster_row')
        name=row.get('username');score=row.get('pt')
        if (not isinstance(name,str) or not name.strip() or len(name)>128
                or any(c in name for c in '\0\r\n') or name in names or isinstance(score,bool)):
            raise ValueError('invalid_roster_identity')
        try:score=float(score)
        except (TypeError,ValueError):raise ValueError('invalid_roster_score') from None
        if not math.isfinite(score) or not 0<=score<=1e9 or score>previous:
            raise ValueError('invalid_roster_order')
        previous=score;names.add(name)
        official_id=row.get('id')
        result.append(dict(player=name,rank=rank,points=score,
                           official_id=str(official_id) if type(official_id) is int and official_id>0 else None))
    return result


def fetch():
    request=Request(SOURCE,headers={'User-Agent':'qq-replay-bot/current-top100'})
    with urlopen(request,timeout=8) as response:
        body=response.read(2*1024*1024+1)
        if len(body)>2*1024*1024:raise ValueError('roster_too_large')
        return json.loads(body)


def replace(db,payload,now):
    members=parse(payload)  # 必须先验证100名完整且有序，失败不能清空原名单。
    db.execute('DELETE FROM stat_current_top100')
    db.executemany('INSERT INTO stat_current_top100 VALUES(?,?,?,?)',
                   [(m['player'],m['rank'],m['points'],m['official_id']) for m in members])
    db.execute('UPDATE stat_roster_state SET version=version+1,updated_at=?,last_attempt=?,last_error=NULL WHERE singleton=1',
               (float(now),float(now)))
    return snapshot(db,now)


def failed(db,now,error):
    db.execute('UPDATE stat_roster_state SET last_attempt=?,last_error=? WHERE singleton=1',
               (float(now),str(error)[:80]))


def snapshot(db,now):
    state=dict(db.execute('SELECT * FROM stat_roster_state WHERE singleton=1').fetchone())
    state['members']=[dict(r) for r in db.execute('SELECT * FROM stat_current_top100 ORDER BY rank')]
    state['available']=len(state['members'])==100 and state['updated_at'] is not None
    state['stale']=not state['available'] or bool(state['last_error']) or float(now)-state['updated_at']>STALE_SECONDS
    state['age_seconds']=max(0,float(now)-state['updated_at']) if state['updated_at'] is not None else None
    return state


def eligible_views(db,replay_root,now,internal_only=False,scope='default'):
    """不聚合的动态有效视角：掉榜立即无效，重回恢复未到期事实。"""
    from .share import safe_replay_file
    state=snapshot(db,now)
    if not state['available']:return dict(roster=state,views=[])
    sql=('SELECT v.*,g.received_at,g.expires_at,m.rank FROM stat_views v '
         'JOIN stat_games g ON g.digest=v.digest JOIN stat_current_top100 m ON m.player=v.player '
         'WHERE g.expires_at>? AND NOT EXISTS(SELECT 1 FROM stat_exclusions e WHERE e.digest=v.digest '
         'AND (e.player=\'\' OR e.player=v.player) AND e.scope=?) ')
    if internal_only:
        sql+=('AND EXISTS(SELECT 1 FROM stat_views opponent JOIN stat_current_top100 other '
              'ON other.player=opponent.player WHERE opponent.digest=v.digest AND opponent.player!=v.player) ')
    rows=[dict(r) for r in db.execute(sql+'ORDER BY g.recorded_at,v.digest,v.player_index',(float(now),scope))]
    alive={}
    for row in rows:
        digest=row['digest']
        if digest not in alive:
            paths=db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f ON f.replay_id=r.id WHERE f.digest=?',(digest,)).fetchall()
            alive[digest]=any(safe_replay_file(replay_root,p['rel_path']) for p in paths)
    return dict(roster=state,views=[r for r in rows if alive[r['digest']]])


def main():
    from .store import Store
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--refresh',action='store_true',help='立即刷新一次官方完整榜单')
    parser.add_argument('--members',action='store_true',help='输出100名玩家及排名')
    parser.add_argument('--capture',action='store_true',help='同时显示当前进程最近的采集状态；updated_at标注新鲜度')
    args=parser.parse_args();root=args.root.resolve()
    cfg=json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
    data=Path(cfg.get('data_dir') or root/'data')
    store=Store(data/'bot.sqlite3')
    try:
        if args.refresh:
            try:store.replace_top100(fetch(),time.time())
            except (OSError,ValueError,TypeError):store.top100_failed(time.time(),'refresh_failed')
        state=store.top100_snapshot(time.time())
        if not args.members:state['member_count']=len(state.pop('members'))
        if args.capture:
            try:state['capture']=json.loads((data/'capture-status.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):state['capture']=None
        print(json.dumps(state,ensure_ascii=False))
        return 0 if state['available'] else 1
    finally:store.close()


if __name__=='__main__':raise SystemExit(main())
