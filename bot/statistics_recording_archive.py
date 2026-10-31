"""On-demand private ZIP of eligible original replays; never writes a fact copy."""
import hashlib
import io
import time
import zipfile
from .share import safe_replay_file
from .statistics_build_detail import query
from .statistics_recording_names import safe_filename


def _rank_label(rank):
    if isinstance(rank, int) and rank > 0:
        return f"当前#{rank}"
    if rank == 0:
        return "当前榜外"
    return "排名暂缺"


def _clean(value, default):
    return safe_filename(value or default, '')


def _side(rank, name, deck):
    return f"{_rank_label(rank)} {_clean(name, '对手未知')} · {_clean(deck, '卡组未识别')}"


def match_folder(game):
    date = game.get('observed_date') or ''
    number = game.get('observed_match_no')
    head = f"{date} 第{number}场" if number is not None else date
    left = _side(game.get('current_rank'), game.get('player'), game.get('deck_type'))
    right = _side(game.get('opponent_current_rank'), game.get('opponent'), game.get('opponent_deck_type'))
    folder = f"{head} {left} vs {right}"
    while len(folder.encode('utf-16-le')) > 300:
        folder = folder[:-1]
    return folder + "/"


def archive(db,root,now,player,digest=None,scope='build',match=None,clock=time.time,allowed_digests=None,history_digests=None):
    result=query(db,root,now,player,digest,scope,allowed_digests=allowed_digests,history_digests=history_digests)
    if result is None:raise ValueError('no_eligible_build')
    games=result['matches']
    if match is not None:
        groups=[g for g in result['match_groups']if g['id']==match]
        if not groups:raise ValueError('match_not_in_scope')
        games=groups[0]['games']
    if not games or len(games)>1000:raise ValueError('empty_or_too_many_sources')
    output=io.BytesIO();seen=set();names=set();size=0;flat_names=set();flat_entries=[]
    match_folders={}
    for game in sorted(games,key=lambda r:(r['observed_date'],r['observed_match_no'],r['observed_game_no'] or 999)):
        key=(game['observed_date'],game['observed_match_no'])
        if key not in match_folders:
            match_folders[key]=match_folder(game)
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
            folder=match_folders[(game['observed_date'],game['observed_match_no'])]
            package.writestr(folder+name,blob)
            if match is None:
                flat=name
                if flat in flat_names:
                    stem,dot,suffix=flat.rpartition('.')
                    flat=f"{stem}-{identity[:8]}{dot}{suffix}" if dot else f"{flat}-{identity[:8]}"
                flat_names.add(flat)
                flat_entries.append((flat,blob))
        for flat,blob in flat_entries:
            package.writestr('总/'+flat,blob)
    if any(g['expires_at']<=max(now,float(clock()))for g in games):raise ValueError('source_expired_during_archive')
    label=player+' '+('整场已收录录像'if match else'该构筑全部已收录录像')
    return output.getvalue(),safe_filename(label,'.zip'),len(seen)
