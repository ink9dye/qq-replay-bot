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
    from .watch_upload_sessions import metadata as upload_metadata
    try:
        uploaded = upload_metadata(db, [d for d in values if d not in explicit])
    except (sqlite3.Error, OSError):
        uploaded = {}
    result.update(uploaded)
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
                # A filename counter is not globally unique, nor start evidence.
                item = (('unverified:'+row['digest'], 0, False)
                        if row['session_id'].startswith('prefix:') else
                        (row['session_id'], row['game_no'], False))
                legacy.setdefault(row['digest'], set()).add(item)
        result.update({d: next(iter(found)) for d, found in legacy.items() if len(found) == 1})
    return result


def capture_metadata(db, digests):
    wanted = {digest for digest in digests if digest}
    if not wanted:
        return {}
    return _read_capture_metadata(db, wanted)


def identity_metadata(db, digests, captures=None):
    """Tuple flag means reliable match identity, never complete draw capture.

    Keep capture_metadata unchanged for consumers that need capture completeness.
    Audited server notifications independently establish game numbers and matches.
    """
    wanted = {digest for digest in digests if digest}
    result = dict(captures) if captures is not None else capture_metadata(db, wanted)
    from .statistics_identity_confirmations import metadata
    try:
        confirmed = metadata(db, wanted)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        confirmed = {}
    for digest, proof in confirmed.items():
        current = result.get(digest)
        # Preserve native full-start identity. Positive conflicting numbers
        # remain uncertain instead of silently replacing explicit evidence.
        if current and (current[2] or (current[1] > 0 and current[1] != proof[1])):
            continue
        result[digest] = proof
    return result


def inherit_first_game_types(db,rows):
    digests = {r['digest'] for r in rows}
    captures=capture_metadata(db,digests)
    metadata=identity_metadata(db,digests,captures)
    for row in rows:
        meta=metadata.get(row['digest'])
        row['observed_session_id']=meta[0] if meta else None
        row['observed_game_no']=meta[1] if meta and meta[1] > 0 else None
        capture=captures.get(row['digest'])
        row['observed_captured_from_start']=bool(capture[2]) if capture else None
        row['observed_identity_confirmed']=bool(meta and meta[2])
        row['first_game_status']='confirmed' if meta and meta[1:]==(1,True) else 'unconfirmed'
        row['observed_deck_type']=row.get('deck_type')
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
    for row in rows:
        meta=metadata.get(row['digest'])
        if not meta or meta[1]<=1 or not meta[2]:continue
        row['observed_deck_type']=row['deck_type']
        initial=first.get(key(row),[])
        anchor=initial[0]if len(initial)==1 else None
        if anchor is None:
            # Missing a verified first game does not erase this observed build's
            # own field-based classification. It remains unsafe for Side inference.
            row['first_game_status']='unconfirmed'
            row['classification_reason']='first_game_unconfirmed_observed_build'
            continue
        row['first_game_status']='available'
        # Explicit same-match capture establishes identity. Side swaps change
        # main/extra membership; that change must not erase the match name.
        row['deck_type']=anchor['deck_type']if anchor else None
        row['classification_status']='recognized'if row['deck_type']else'unknown'
        row['classification_source']='same_match_first_game'if anchor else'post_side_first_game_missing'
        row['classification_source_digest']=anchor['digest']if anchor else None
        row['classification_reason']='post_side_not_classified_independently'


def classification_builds(db):
    rows=list(db.execute('SELECT digest,build FROM stat_views'));metadata=identity_metadata(db,{r['digest']for r in rows})
    # Legacy sources without reliable game numbers cannot be labelled "post-side".
    return {row['build']for row in rows if not metadata.get(row['digest'])
            or not metadata[row['digest']][2] or metadata[row['digest']][1]==1}
