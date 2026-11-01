"""Display/download names from original observations; no stored replay is renamed."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from .statistics_match_identity import capture_metadata

CST=timezone(timedelta(hours=8))


def catalog(db,player):
    rows=[dict(r)for r in db.execute('SELECT v.digest,g.received_at FROM stat_views v JOIN stat_games g ON g.digest=v.digest WHERE v.player=?',(player,))]
    digests={r['digest']for r in rows};meta=capture_metadata(db,digests)
    explicit={r['digest']for r in db.execute("SELECT digest FROM stat_evidence WHERE payload_json LIKE '%session_id%' OR payload_json LIKE '%game_no%'")}
    legacy={}
    for r in db.execute('SELECT f.digest,s.session_id,s.game_no FROM stat_files f JOIN replay_sessions s ON s.replay_id=f.replay_id JOIN replays r ON r.id=f.replay_id WHERE r.focus_player=?',(player,)):
        if r['session_id']and type(r['game_no'])is int and r['game_no']>0:
            legacy.setdefault(r['digest'],set()).add((r['session_id'],r['game_no'],True))
    for digest,values in legacy.items():
        if digest not in meta and digest not in explicit and len(values)==1:meta[digest]=next(iter(values))
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
                    observed_match_no=number,observed_game_no=m[1]if m else None,
                    identity_first_digest=first[0]if len(first)==1 else None,
                    observed_match_id=hashlib.sha256((player+'\0'+key).encode()).hexdigest()[:20])
    return result



def catalog_many(db, players):
    """一次算多个玩家的录像编号；逻辑与 catalog 相同，只是共享全表查询。"""
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
    meta = capture_metadata(db, digests)
    explicit = {r['digest'] for r in db.execute(
        "SELECT digest FROM stat_evidence WHERE payload_json LIKE '%session_id%' OR payload_json LIKE '%game_no%'")}
    legacy = {}
    for r in db.execute(
            f'SELECT f.digest,r.focus_player,s.session_id,s.game_no FROM stat_files f '
            f'JOIN replay_sessions s ON s.replay_id=f.replay_id JOIN replays r ON r.id=f.replay_id '
            f'WHERE r.focus_player IN ({marks})', players):
        if r['session_id'] and type(r['game_no']) is int and r['game_no'] > 0:
            legacy.setdefault(r['focus_player'], {}).setdefault(r['digest'], set()).add(
                (r['session_id'], r['game_no'], True))
    pairs = {}
    for r in db.execute('SELECT digest,player FROM stat_views'):
        if r['digest'] in digests:
            pairs.setdefault(r['digest'], []).append(r['player'])
    result = {}
    for player in players:
        rows = rows_by_player.get(player, [])
        player_legacy = legacy.get(player, {})
        player_meta = dict(meta)
        for digest, values in player_legacy.items():
            if digest not in player_meta and digest not in explicit and len(values) == 1:
                player_meta[digest] = next(iter(values))
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
                        observed_match_no=number, observed_game_no=m[1] if m else None,
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


def decorate(db,rows,replay_root=None,now=None):
    catalogs=catalog_many(db,{row['player']for row in rows})
    ranks={r['player']:r['rank']for r in db.execute('SELECT player,rank FROM stat_current_top100')}
    first_ok={}
    for row in rows:
        player=row['player']
        info=catalogs.get(player,{}).get(row['digest'])
        if info:
            row.update(info);row.setdefault('current_rank',ranks.get(player));row.setdefault('opponent_current_rank',ranks.get(row.get('opponent')))
            if replay_root is not None and now is not None and info['identity_first_digest']:
                first=info['identity_first_digest']
                from .share import safe_replay_file
                if first not in first_ok:
                    files=db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f ON f.replay_id=r.id WHERE f.digest=?',(first,)).fetchall()
                    first_ok[first]=any(safe_replay_file(replay_root,r['rel_path'])for r in files)
                if first_ok[first]:
                    for who,field in ((player,'match_deck_type'),(row.get('opponent'),'opponent_match_deck_type')):
                        name=db.execute("SELECT b.deck_type FROM stat_views v JOIN stat_games g ON g.digest=v.digest JOIN stat_builds b ON b.fingerprint=v.build WHERE v.digest=? AND v.player=? AND g.expires_at>? AND NOT EXISTS(SELECT 1 FROM stat_exclusions e WHERE e.digest=v.digest AND(e.player='' OR e.player=v.player)AND e.scope='default')",(first,who,now)).fetchone()
                        if name and name[0]:row[field]=name[0]
            row['recording_title']=title(row);row['download_filename']=safe_filename(row['recording_title'])
