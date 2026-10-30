from .top100 import eligible_views
from .share import safe_replay_file


def details(db,replay_root,now,player,digest):
    if not isinstance(player,str) or not player or len(player)>128 or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):return None
    basis=eligible_views(db,replay_root,now,player=player)
    row=next((r for r in basis['views'] if r['player']==player and r['digest']==digest),None)
    if row is None:return None
    files=db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f ON f.replay_id=r.id WHERE f.digest=? ORDER BY r.id',(digest,)).fetchall()
    file=next((path for r in files if (path:=safe_replay_file(replay_root,r['rel_path'])) is not None),None)
    if file is None:return None
    from .statistics_build_detail import query
    build=query(db,replay_root,now,player,digest)
    recording=next((r for r in (build or {}).get('matches',[])if r['digest']==digest),{})
    return dict(person=dict(player=player,rank=row['rank'],deck_type=row['deck_type'],series=row['series']),
        recording_title=recording.get('recording_title'),download_filename=recording.get('download_filename'),
        cards=[dict(r) for r in db.execute('SELECT partition,code,copies FROM stat_build_cards WHERE build=? ORDER BY partition,code',(row['build'],))],
        completeness={p:bool(row[p+'_complete']) for p in ('main','side','extra')},
        source=dict(digest=digest,recorded_at=row['recorded_at'],expires_at=row['expires_at'],outcome=row['outcome'],first_player=row['first_player']),
        roster_version=basis['roster']['version']),file


def exclusions(db,now):
    return [dict(r) for r in db.execute("SELECT e.digest,e.player,e.reason,g.recorded_at,g.expires_at FROM stat_exclusions e JOIN stat_games g ON g.digest=e.digest JOIN stat_current_top100 m ON m.player=e.player WHERE e.scope='default' AND g.expires_at>? ORDER BY g.recorded_at DESC",(now,))]


def set_exclusion(db,now,player,digest,excluded):
    if not player or len(player)>128 or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest) or type(excluded) is not bool:raise ValueError('invalid_exclusion')
    valid=db.execute('SELECT 1 FROM stat_views v JOIN stat_games g ON g.digest=v.digest JOIN stat_current_top100 m ON m.player=v.player WHERE v.digest=? AND v.player=? AND g.expires_at>?',(digest,player,now)).fetchone()
    if not valid:raise ValueError('source_no_longer_eligible')
    if excluded:
        db.execute("INSERT INTO stat_exclusions VALUES(?,?,'default','owner_web') ON CONFLICT(digest,player,scope) DO UPDATE SET reason=excluded.reason",(digest,player))
    else:db.execute("DELETE FROM stat_exclusions WHERE digest=? AND player=? AND scope='default'",(digest,player))
