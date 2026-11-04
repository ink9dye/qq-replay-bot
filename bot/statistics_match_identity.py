"""Reliable match evidence; post-side observations inherit only their own first game."""
import json
import sqlite3


def _read_capture_metadata(db, digests):
    """只读这些 digest 的证据行；避免为了几条证据扫描整张 stat_evidence。

    不做跨调用缓存：证据行随导入变化，缓存会让同一进程里先后变化的库读到旧值。
    优先用 SQLite JSON1 一次提取小 capture 对象；payload 很大，逐行 json.loads
    在 2C2G 上要 10 秒以上。JSON1 不可用或 payload 损坏时回退旧路径。
    """
    captures = {}
    explicit = set()
    values = list(dict.fromkeys(digests))
    for start in range(0, len(values), 400):
        chunk = values[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        try:
            evidence = [(r['digest'], r['capture']) for r in db.execute(
                "SELECT digest,json_extract(payload_json,'$.capture') capture "
                'FROM stat_evidence WHERE digest IN (%s)' % marks, chunk)]
        except sqlite3.DatabaseError:
            evidence = []
            for row in db.execute('SELECT digest,payload_json FROM stat_evidence '
                                  'WHERE digest IN (%s)' % marks, chunk):
                try:
                    payload = json.loads(row['payload_json'])
                    capture = payload.get('capture') if isinstance(payload, dict) else None
                    evidence.append((row['digest'], json.dumps(capture)))
                except (ValueError, TypeError):
                    continue
        parsed = []
        for digest, capture in evidence:
            try:
                info = json.loads(capture) if isinstance(capture, str) else None
            except ValueError:
                continue
            if not isinstance(info, dict):
                continue
            parsed.append((digest, info.get('game_no'), info.get('session_id'),
                           info.get('captured_from_start'), 'game_no' in info or 'session_id' in info))
        for digest, game_no, session_id, captured_from_start, is_explicit in parsed:
            if is_explicit:
                explicit.add(digest)
            if type(game_no) is int and game_no > 0 and (
                    session_id is None or isinstance(session_id, str)):
                captures.setdefault(digest, set()).add(
                    (session_id, game_no, captured_from_start is True))
    result = {digest: next(iter(found)) for digest, found in captures.items() if len(found) == 1}
    # Uploaded/legacy replays can have no capture payload but already have a
    # recorded session association. Use only unique associations, never timestamps.
    for start in range(0, len(values), 400):
        chunk = [d for d in values[start:start + 400] if d not in result and d not in explicit]
        if not chunk:
            continue
        marks = ','.join('?' for _ in chunk)
        legacy = {}
        for row in db.execute('SELECT f.digest,s.session_id,s.game_no FROM stat_files f '
                              'JOIN replay_sessions s ON s.replay_id=f.replay_id '
                              'WHERE f.digest IN (%s)' % marks, chunk):
            if isinstance(row['session_id'], str) and row['session_id'] and type(row['game_no']) is int and row['game_no'] > 0:
                legacy.setdefault(row['digest'], set()).add((row['session_id'], row['game_no'], True))
        result.update({d: next(iter(found)) for d, found in legacy.items() if len(found) == 1})
    return result


def capture_metadata(db, digests):
    wanted = {digest for digest in digests if digest}
    if not wanted:
        return {}
    return _read_capture_metadata(db, wanted)


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
                'WHERE build IN (%s) AND partition IN (\'main\',\'extra\')' % marks,
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
    for row in rows:
        row['observed_match_players'] = tuple(sorted(pairs.get(row['digest'], ())))
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
        if meta and meta[1] > 1:
            if key(row) in first_labeled:
                needed_builds.add(row.get('build'))
    cores = _core_signatures(db, needed_builds)
    for row in rows:
        meta=metadata.get(row['digest'])
        if not meta or meta[1]<=1:continue
        row['observed_deck_type']=row['deck_type']
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
