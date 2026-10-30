"""On-demand private ZIP of eligible original replays; never writes a fact copy."""
import hashlib
import io
import time
import zipfile
from .share import safe_replay_file
from .statistics_build_detail import query
from .statistics_recording_names import safe_filename


def archive(db,root,now,player,digest=None,scope='build',match=None,clock=time.time,allowed_digests=None,history_digests=None):
    result=query(db,root,now,player,digest,scope,allowed_digests=allowed_digests,history_digests=history_digests)
    if result is None:raise ValueError('no_eligible_build')
    games=result['matches']
    if match is not None:
        groups=[g for g in result['match_groups']if g['id']==match]
        if not groups:raise ValueError('match_not_in_scope')
        games=groups[0]['games']
    if not games or len(games)>1000:raise ValueError('empty_or_too_many_sources')
    output=io.BytesIO();seen=set();names=set();size=0
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED)as package:
        for game in sorted(games,key=lambda r:(r['observed_date'],r['observed_match_no'],r['observed_game_no']or 999,r['digest'])):
            identity=game['digest']
            if identity in seen:continue
            seen.add(identity)
            current=max(now,float(clock()))
            if game['expires_at']<=current:raise ValueError('source_expired_during_archive')
            if db.execute("SELECT 1 FROM stat_exclusions WHERE digest=? AND (player='' OR player=?) AND scope='default'",(identity,player)).fetchone():raise ValueError('source_excluded')
            candidates=db.execute('SELECT r.rel_path FROM replays r JOIN stat_files f ON f.replay_id=r.id WHERE f.digest=? ORDER BY r.id',(identity,)).fetchall()
            path=next((p for r in candidates if(p:=safe_replay_file(root,r['rel_path']))is not None),None)
            if path is None:raise ValueError('source_missing')
            blob=path.read_bytes()
            if hashlib.sha256(blob).hexdigest()!=identity:raise ValueError('source_changed')
            size+=len(blob)
            if size>128*1024*1024:raise ValueError('archive_too_large')
            name=game['download_filename']
            if name in names:name=safe_filename(game['recording_title']+' '+identity[:12])
            names.add(name)
            folder=game['observed_date']+f" 第{game['observed_match_no']}场/"if match is None else''
            package.writestr(folder+name,blob)
    if any(g['expires_at']<=max(now,float(clock()))for g in games):raise ValueError('source_expired_during_archive')
    label=player+' '+('整场已收录录像'if match else'该构筑全部已收录录像')
    return output.getvalue(),safe_filename(label,'.zip'),len(seen)
