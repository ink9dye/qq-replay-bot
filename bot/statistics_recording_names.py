"""Display/download names from original observations; no stored replay is renamed."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from .statistics_match_identity import identity_metadata

CST=timezone(timedelta(hours=8))


def catalog(db,player):
    rows=[dict(r)for r in db.execute('SELECT v.digest,g.received_at FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE v.player=?',(player,))]
    digests={r['digest']for r in rows};meta=identity_metadata(db,digests)
    pairs={}
    for r in db.execute('SELECT digest,player FROM stat_views'):
        if r['digest']in digests:pairs.setdefault(r['digest'],[]).append(r['player'])
    groups={}
    for row in rows:
        m=meta.get(row['digest']);pair=tuple(sorted(pairs.get(row['digest'],[])))
        key=(m[0],pair)if m and m[0]and len(pair)==2 else ('single:'+row['digest'],pair)
        groups.setdefault(key,[]).append(row)
    days={}
    for key,group in groups.items():
        stamp=min(r['received_at']for r in group);day=datetime.fromtimestamp(stamp,CST).date()
        days.setdefault(day,[]).append((stamp,repr(key),group))
    result={}
    for day,groups_in_day in days.items():
        for number,(_,key,group)in enumerate(sorted(groups_in_day,key=lambda g:(g[0],g[1])),1):
            first=[r['digest']for r in group if meta.get(r['digest'],(None,None,None))[1:]==(1,True)]
            for row in group:
                m=meta.get(row['digest'])
                result[row['digest']]=dict(observed_date=day.isoformat(),observed_day=f'{day.month:02}/{day.day:02}',
                    observed_match_no=number,observed_game_no=m[1]if m and m[1]>0 else None,
                    identity_first_digest=first[0]if len(first)==1 else None,
                    observed_match_id=hashlib.sha256((player+'\0'+key).encode()).hexdigest()[:20])
    return result



def catalog_many(db, players, identity=None):
    """一次算多个玩家的录像编号；逻辑与 catalog 相同，只是共享全表查询。

    identity（可选）来自同一次查询的 eligible_views/identity_context：命中时不再
    重复读证据表，缺失的 digest 才补算，结果与独立计算完全相同。
    """
    players = sorted({p for p in players if p})
    if not players:
        return {}
    marks = ','.join('?' for _ in players)
    rows_by_player = {}
    digests = set()
    for r in db.execute(
            f'SELECT v.player,v.digest,g.received_at FROM stat_views v JOIN stat_games g '
            f'ON g.digest=v.digest WHERE v.player IN ({marks})', players):
        rows_by_player.setdefault(r['player'], []).append(dict(r))
        digests.add(r['digest'])
    known = identity['metadata'] if identity else {}
    meta = {digest: known[digest] for digest in digests if digest in known}
    missing = [digest for digest in digests if digest not in meta]
    if missing:
        meta.update(identity_metadata(db, missing))
    pairs = {}
    for r in db.execute('SELECT digest,player FROM stat_views'):
        if r['digest'] in digests:
            pairs.setdefault(r['digest'], []).append(r['player'])
    result = {}
    for player in players:
        rows = rows_by_player.get(player, [])
        player_meta = meta
        groups = {}
        for row in rows:
            m = player_meta.get(row['digest'])
            pair = tuple(sorted(pairs.get(row['digest'], [])))
            key = (m[0], pair) if m and m[0] and len(pair) == 2 else ('single:' + row['digest'], pair)
            groups.setdefault(key, []).append(row)
        days = {}
        for key, group in groups.items():
            stamp = min(r['received_at'] for r in group)
            day = datetime.fromtimestamp(stamp, CST).date()
            days.setdefault(day, []).append((stamp, repr(key), group))
        player_result = {}
        for day, groups_in_day in days.items():
            for number, (_, key, group) in enumerate(
                    sorted(groups_in_day, key=lambda g: (g[0], g[1])), 1):
                first = [r['digest'] for r in group
                         if player_meta.get(r['digest'], (None, None, None))[1:] == (1, True)]
                for row in group:
                    m = player_meta.get(row['digest'])
                    player_result[row['digest']] = dict(
                        observed_date=day.isoformat(), observed_day=f'{day.month:02}/{day.day:02}',
                        observed_match_no=number, observed_game_no=m[1] if m and m[1]>0 else None,
                        identity_first_digest=first[0] if len(first) == 1 else None,
                        observed_match_id=hashlib.sha256((player + '\0' + key).encode()).hexdigest()[:20])
        result[player] = player_result
    return result


def title(row):
    rank=lambda n:'当前#'+str(n)if n else'当前排名暂缺'
    return (f"{row['observed_day']} {row['observed_match_no']} {row['observed_game_no'] or '未知'} "
        f"{rank(row.get('current_rank',row.get('rank')))} {row['player']} · {row.get('match_deck_type')or row.get('deck_type')or'卡组未识别'} vs "
        f"{rank(row.get('opponent_current_rank'))} {row.get('opponent')or'对手未知'} · {row.get('opponent_match_deck_type')or row.get('opponent_deck_type')or'卡组未识别'}")


def safe_filename(value,extension='.yrp'):
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f]','-',value).rstrip(' .')
    # Keep names comfortably below Windows component limits, including Unicode.
    while len(value.encode('utf-16-le'))>360:value=value[:-1]
    return (value or 'replay')+extension


def _match_deck_types(db, pairs, now):
    """(首个 digest, 玩家) -> 该玩家在那局的卡组名；批量查询，等价于逐条 fetchone。

    逐条版本每行最多两条 SQL（我方/对手），默认页面因此产生数千条小查询；
    这里先落到连接内临时表再一次性 JOIN，条件与逐条完全一致。
    """
    wanted = sorted({(digest, player) for digest, player in pairs if digest and player})
    if not wanted:
        return {}
    db.execute('DROP TABLE IF EXISTS temp._decorate_first')
    db.execute('CREATE TEMP TABLE _decorate_first(digest TEXT,player TEXT,PRIMARY KEY(digest,player))')
    try:
        db.executemany('INSERT OR IGNORE INTO _decorate_first VALUES(?,?)', wanted)
        rows = list(db.execute(
            'SELECT v.digest,v.player,b.deck_type FROM _decorate_first f '
            'JOIN stat_views v ON v.digest=f.digest AND v.player=f.player '
            'JOIN stat_games g ON g.digest=v.digest '
            'JOIN stat_builds b ON b.fingerprint=v.build '
            'WHERE g.expires_at>? AND NOT EXISTS(SELECT 1 FROM stat_exclusions e '
            "WHERE e.digest=v.digest AND (e.player='' OR e.player=v.player) AND e.scope='default')",
            (now,)))
    finally:
        db.execute('DROP TABLE IF EXISTS temp._decorate_first')
    return {(row['digest'], row['player']): row['deck_type'] for row in rows}


def decorate(db,rows,replay_root=None,now=None,identity=None,alive=None):
    """给录像行补编号/标题/首局卡组。

    identity/alive 由同一次查询的 eligible_views 传入：身份信息与文件存活判定只做一次，
    缺失部分仍按原路径补算，结果与独立调用一致。
    """
    catalogs=catalog_many(db,{row['player']for row in rows},identity=identity)
    ranks={r['player']:r['rank']for r in db.execute('SELECT player,rank FROM stat_current_top100')}
    enriched=[]
    for row in rows:
        player=row['player']
        info=catalogs.get(player,{}).get(row['digest'])
        if not info:
            continue
        row.update(info);row.setdefault('current_rank',ranks.get(player));row.setdefault('opponent_current_rank',ranks.get(row.get('opponent')))
        enriched.append(row)
    if replay_root is None or now is None:
        for row in enriched:
            row['recording_title']=title(row);row['download_filename']=safe_filename(row['recording_title'])
        return
    from .replay_alive import path_alive,new_state
    state=None;first_ok={};needed=set()
    for row in enriched:
        first=row.get('identity_first_digest')
        if not first:
            continue
        ok=first_ok.get(first)
        if ok is None:
            if alive is not None and first in alive:
                ok=bool(alive[first])
            else:
                # 首局 digest 可能不在本次候选视角里（例如已过期/未入选）；此时必须自己判定，
                # 不能把“不在活表里”当成不存活。
                if state is None:
                    state=new_state(replay_root)
                files=db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f ON f.replay_id=r.id WHERE f.digest=?',(first,)).fetchall()
                ok=any(path_alive(replay_root,r['rel_path'],state)for r in files)
            first_ok[first]=ok
        if not ok:
            continue
        if row['player']:needed.add((first,row['player']))
        if row.get('opponent'):needed.add((first,row['opponent']))
    lookup=_match_deck_types(db,needed,now) if needed else {}
    for row in enriched:
        first=row.get('identity_first_digest')
        if first and first_ok.get(first):
            name=lookup.get((first,row['player']))
            if name:row['match_deck_type']=name
            name=lookup.get((first,row.get('opponent')))
            if name:row['opponent_match_deck_type']=name
        row['recording_title']=title(row);row['download_filename']=safe_filename(row['recording_title'])
