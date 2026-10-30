"""Reliable match evidence; post-side observations inherit only their own first game."""
import json


def capture_metadata(db,digests):
    captures={}
    for row in db.execute('SELECT digest,payload_json FROM stat_evidence'):
        if row['digest'] not in digests:continue
        try:info=json.loads(row['payload_json']).get('capture',{})
        except (ValueError,AttributeError):continue
        if type(info.get('game_no'))is int and info['game_no']>0 and (info.get('session_id')is None or isinstance(info.get('session_id'),str)):
            captures.setdefault(row['digest'],set()).add((info.get('session_id'),info['game_no'],info.get('captured_from_start')is True))
    return {digest:next(iter(values))for digest,values in captures.items()if len(values)==1}


def inherit_first_game_types(db,rows):
    metadata=capture_metadata(db,{r['digest']for r in rows})
    pairs={}
    for row in db.execute('SELECT digest,player FROM stat_views'):
        pairs.setdefault(row['digest'],set()).add(row['player'])
    def key(row):
        meta=metadata.get(row['digest']);pair=tuple(sorted(pairs.get(row['digest'],())))
        return (row['player'],meta[0],pair)if meta and meta[0]and len(pair)==2 else None
    first={}
    for row in rows:
        if metadata.get(row['digest'],(None,None,None))[1:]==(1,True)and key(row):first.setdefault(key(row),[]).append(row)
    for row in rows:
        meta=metadata.get(row['digest'])
        if not meta or meta[1]<=1:continue
        row['observed_deck_type']=row['deck_type'];initial=first.get(key(row),[])
        anchor=initial[0]if len(initial)==1 else None
        row['deck_type']=anchor['deck_type']if anchor else None
        row['classification_status']='recognized'if row['deck_type']else'unknown'
        row['classification_source']='same_match_first_game'if anchor else'post_side_first_game_missing'
        row['classification_source_digest']=anchor['digest']if anchor else None
        row['classification_reason']='post_side_not_classified_independently'


def classification_builds(db):
    rows=list(db.execute('SELECT digest,build FROM stat_views'));metadata=capture_metadata(db,{r['digest']for r in rows})
    # Legacy sources without reliable game numbers cannot be labelled "post-side".
    return {row['build']for row in rows if not metadata.get(row['digest'])or metadata[row['digest']][1]==1}
