"""官方当前竞技前100；成功全量替换，失败保留最后一次完整名单。"""
from __future__ import annotations

import argparse
import json
import math
import time
import socket
import ssl
from urllib.error import HTTPError, URLError
from pathlib import Path
from urllib.request import Request, urlopen

from . import statistics_quality

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
    columns={r[1] for r in db.execute('PRAGMA table_info(stat_roster_state)')}
    if 'consecutive_failures' not in columns:
        db.execute('ALTER TABLE stat_roster_state ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0')
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


def error_category(error):
    """固定类别，不把可能包含代理认证信息的异常原文写入日志。"""
    if isinstance(error, HTTPError):return 'http_'+str(error.code)
    if isinstance(error, URLError):return error_category(error.reason)
    if isinstance(error, (TimeoutError, socket.timeout)):return 'timeout'
    if isinstance(error, socket.gaierror):return 'dns'
    if isinstance(error, ssl.SSLError):return 'tls'
    if isinstance(error, OSError):return 'connection'
    if isinstance(error, (ValueError,TypeError)):return 'invalid_response'
    return 'network_error'


def fetch():
    request=Request(SOURCE,headers={'User-Agent':'qq-replay-bot/current-top100'})
    for attempt in range(2):
        try:
            with urlopen(request,timeout=8) as response:
                body=response.read(2*1024*1024+1)
                if len(body)>2*1024*1024:raise ValueError('roster_too_large')
                payload=json.loads(body)
                parse(payload)
                return payload
        except OSError as error:
            if attempt or (isinstance(error,HTTPError) and error.code not in (408,429) and error.code<500):raise
            time.sleep(0.5)


def replace(db,payload,now):
    members=parse(payload)  # 必须先验证100名完整且有序，失败不能清空原名单。
    db.execute('DELETE FROM stat_current_top100')
    db.executemany('INSERT INTO stat_current_top100 VALUES(?,?,?,?)',
                   [(m['player'],m['rank'],m['points'],m['official_id']) for m in members])
    db.execute('UPDATE stat_roster_state SET version=version+1,updated_at=?,last_attempt=?,last_error=NULL,consecutive_failures=0 WHERE singleton=1',
               (float(now),float(now)))
    return snapshot(db,now)


def failed(db,now,error):
    db.execute('UPDATE stat_roster_state SET last_attempt=?,last_error=?,consecutive_failures=consecutive_failures+1 WHERE singleton=1',
               (float(now),str(error)[:80]))


def snapshot(db,now):
    state=dict(db.execute('SELECT * FROM stat_roster_state WHERE singleton=1').fetchone())
    state['members']=[dict(r) for r in db.execute('SELECT * FROM stat_current_top100 ORDER BY rank')]
    state['available']=len(state['members'])==100 and state['updated_at'] is not None
    state['stale']=not state['available'] or bool(state['last_error']) or float(now)-state['updated_at']>STALE_SECONDS
    state['age_seconds']=max(0,float(now)-state['updated_at']) if state['updated_at'] is not None else None
    return state


def eligible_views(db,replay_root,now,internal_only=False,scope='default',series_catalog=None,player=None,check_files=True,annotate_series=True):
    """不聚合的动态有效视角：掉榜立即无效，重回恢复未到期事实。"""
    from .share import safe_replay_file
    from .card_series import reference,annotate,SEMANTICS
    series_catalog=series_catalog or reference(db)
    series_reference=dict(status=series_catalog['status'],revision=series_catalog['revision'],semantics=SEMANTICS)
    state=snapshot(db,now)
    from .statistics_rank_scope import current,filter_rows
    population_scope=current()
    wide_population=population_scope is not None and population_scope['own']!='100'
    revision=db.execute('SELECT revision FROM stat_classification_state WHERE singleton=1').fetchone()[0]
    population=('FROM stat_views v JOIN stat_games g ON g.digest=v.digest '
                'JOIN stat_current_top100 m ON m.player=v.player WHERE g.expires_at>? ')
    diagnostics=dict(
        population='current_roster_before_optional_internal_filter',
        current_unexpired_views=db.execute('SELECT COUNT(*) '+population,(float(now),)).fetchone()[0],
        excluded_current_views=db.execute('SELECT COUNT(*) '+population+
            "AND EXISTS(SELECT 1 FROM stat_exclusions e WHERE e.digest=v.digest AND (e.player='' OR e.player=v.player) AND e.scope=?)",
            (float(now),scope)).fetchone()[0],
        expired_views=db.execute('SELECT COUNT(*) FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE g.expires_at<=?',(float(now),)).fetchone()[0],
        unexpired_outside_roster_views=db.execute('SELECT COUNT(*) FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE g.expires_at>? '
            'AND NOT EXISTS(SELECT 1 FROM stat_current_top100 m WHERE m.player=v.player)',(float(now),)).fetchone()[0])
    if not state['available'] and player is None and not wide_population:return dict(roster=state,classification_revision=revision,views=[],diagnostics=diagnostics,series_reference=series_reference)
    sql=('SELECT v.*,g.recorded_at,g.received_at,g.expires_at,m.rank,b.deck_type,b.classification_status,b.classification_source,b.series_status,b.main_complete,b.extra_complete,b.side_complete FROM stat_views v '
         'JOIN stat_games g ON g.digest=v.digest JOIN stat_current_top100 m ON m.player=v.player '
         'JOIN stat_builds b ON b.fingerprint=v.build '
         'WHERE g.expires_at>? AND NOT EXISTS(SELECT 1 FROM stat_exclusions e WHERE e.digest=v.digest '
         'AND (e.player=\'\' OR e.player=v.player) AND e.scope=?) ')
    if internal_only:
        sql+=('AND EXISTS(SELECT 1 FROM stat_views opponent JOIN stat_current_top100 other '
              'ON other.player=opponent.player WHERE opponent.digest=v.digest AND opponent.player!=v.player) ')
    params=(float(now),scope)
    if player is not None:
        sql=sql.replace('JOIN stat_current_top100 m ON m.player=v.player','LEFT JOIN stat_current_top100 m ON m.player=v.player')
        sql+='AND v.player=? '
        params+=(player,)
    if wide_population:sql=sql.replace('JOIN stat_current_top100 m ON m.player=v.player','LEFT JOIN stat_current_top100 m ON m.player=v.player')
    rows=[dict(r) for r in db.execute(sql+'ORDER BY g.recorded_at,v.digest,v.player_index',params)]
    if statistics_quality.enabled():
        excluded=statistics_quality.excluded_digests(db)
        before_quality=len(rows)
        if excluded:rows=[row for row in rows if row['digest'] not in excluded]
        diagnostics['quality_filter']=dict(enabled=True,excluded_digests=len(excluded),
            excluded_views=before_quality-len(rows))
    if check_files:
        file_paths={}
        for file_row in db.execute('SELECT f.digest,r.rel_path FROM stat_files f JOIN replays r ON r.id=f.replay_id'):
            file_paths.setdefault(file_row['digest'],[]).append(file_row['rel_path'])
        alive={}
        root_resolved=replay_root.resolve();dir_cache={}
        for row in rows:
            digest=row['digest']
            if digest not in alive:
                alive[digest]=any(safe_replay_file(replay_root,rel_path,root_resolved,dir_cache)
                                  for rel_path in file_paths.get(digest,()))
    else:
        alive={row['digest']: True for row in rows}
    if annotate_series:
        annotate(db,rows,series_catalog)
    from .statistics_match_identity import inherit_first_game_types
    # Only eligible, source-present first games may supply a later game's identity.
    inherit_first_game_types(db,[r for r in rows if alive[r['digest']]])
    revision=db.execute('SELECT revision FROM stat_classification_state WHERE singleton=1').fetchone()[0]
    if population_scope is not None:
        before_range=len(rows)
        rows=filter_rows(db,rows,state,now)
        diagnostics.update(population='selected_self_and_opponent_rank_ranges',before_rank_range_views=before_range,rank_range_excluded_views=before_range-len(rows))
    diagnostics.update(after_scope_and_internal_views=len(rows),missing_source_views=sum(not alive[r['digest']] for r in rows))
    return dict(roster=state,classification_revision=revision,views=[r for r in rows if alive[r['digest']]],diagnostics=diagnostics,series_reference=series_reference)


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
