"""Reliable match evidence; post-side observations inherit only their own first game."""
import json
import sqlite3


def capture_metadata(db,digests):
    captures={}
    for row in db.execute('SELECT digest,payload_json FROM stat_evidence'):
        if row['digest'] not in digests:continue
        try:info=json.loads(row['payload_json']).get('capture',{})
        except (ValueError,AttributeError):continue
        if type(info.get('game_no'))is int and info['game_no']>0 and (info.get('session_id')is None or isinstance(info.get('session_id'),str)):
            captures.setdefault(row['digest'],set()).add((info.get('session_id'),info['game_no'],info.get('captured_from_start')is True))
    return {digest:next(iter(values))for digest,values in captures.items()if len(values)==1}


def _core_signatures(db, builds):
    """build -> 主卡+额外（不含 Side）的多重集合签名。"""
    builds = {build for build in builds if build}
    result = {}
    if not builds:
        return result
    builds = list(builds)
    for start in range(0, len(builds), 400):
        chunk = builds[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        cards = {}
        try:
            rows = db.execute(
                'SELECT build,code,copies,partition FROM stat_build_cards '
                'WHERE build IN (%s) AND partition IN (\'main\',\'extra\') AND copies>0' % marks,
                chunk)
        except sqlite3.Error:
            return None
        for row in rows:
            pair = cards.setdefault(row['build'], ([], []))
            pair[0 if row['partition'] == 'main' else 1].append((int(row['code']), int(row['copies'])))
        for build, (main, extra) in cards.items():
            if main:
                result[build] = tuple(sorted(main + extra))
    return result


def inherit_first_game_types(db,rows):
    metadata=capture_metadata(db,{r['digest']for r in rows})
    for row in rows:
        meta=metadata.get(row['digest'])
        row['observed_session_id']=meta[0] if meta else None
        row['observed_game_no']=meta[1] if meta else None
        row['observed_captured_from_start']=bool(meta[2]) if meta else None
    pairs={}
    for row in db.execute('SELECT digest,player FROM stat_views'):
        pairs.setdefault(row['digest'],set()).add(row['player'])
    def key(row):
        meta=metadata.get(row['digest']);pair=tuple(sorted(pairs.get(row['digest'],())))
        return (row['player'],meta[0],pair)if meta and meta[0]and len(pair)==2 else None
    first={}
    for row in rows:
        if metadata.get(row['digest'],(None,None,None))[1:]==(1,True)and key(row):first.setdefault(key(row),[]).append(row)
    # 只有首局已有名字时才需要做核心比对；未知首局本来就不会继承。
    first_labeled = {key: values[0] for key, values in first.items()
                     if len(values) == 1 and values[0].get('deck_type')}
    needed_builds = {anchor.get('build') for anchor in first_labeled.values()}
    for row in rows:
        meta = metadata.get(row['digest'])
        if meta and meta[1] > 1 and row.get('classification_source') != 'manual':
            if key(row) in first_labeled:
                needed_builds.add(row.get('build'))
    cores = _core_signatures(db, needed_builds)
    for row in rows:
        meta=metadata.get(row['digest'])
        if not meta or meta[1]<=1:continue
        row['observed_deck_type']=row['deck_type']
        # 人工命名优先：换边后小局自己已经被手动命名过，就不能被首局标签覆盖。
        if row.get('classification_source')=='manual':continue
        initial=first.get(key(row),[])
        anchor=initial[0]if len(initial)==1 else None
        # Side 换边不改变主卡+额外；核心不同或缺失说明不能安全继承。
        current_core=cores.get(row.get('build')) if cores is not None else None
        anchor_core=cores.get(anchor.get('build')) if cores is not None and anchor else None
        if anchor and anchor.get('deck_type') and cores is not None and (not current_core or not anchor_core or current_core != anchor_core):
            row['deck_type']=None
            row['classification_status']='unknown'
            row['classification_source']='post_side_core_mismatch'
            row['classification_source_digest']=anchor['digest']
            row['classification_reason']='post_side_core_changed'
            continue
        row['deck_type']=anchor['deck_type']if anchor else None
        row['classification_status']='recognized'if row['deck_type']else'unknown'
        row['classification_source']='same_match_first_game'if anchor else'post_side_first_game_missing'
        row['classification_source_digest']=anchor['digest']if anchor else None
        row['classification_reason']='post_side_not_classified_independently'


def classification_builds(db):
    rows=list(db.execute('SELECT digest,build FROM stat_views'));metadata=capture_metadata(db,{r['digest']for r in rows})
    # Legacy sources without reliable game numbers cannot be labelled "post-side".
    return {row['build']for row in rows if not metadata.get(row['digest'])or metadata[row['digest']][1]==1}
