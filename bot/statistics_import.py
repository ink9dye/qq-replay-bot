"""幂等导入现存解析及观战证据，不启动观战、不发送QQ、不自行重放全部录像。"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from .replay_analysis import VERSION, apply_live_evidence, sidecar
from .watch_core import decks_from_replay


def signature(path):
    result = []
    for item in (path, sidecar(path), sidecar(path, 'evidence')):
        try:
            s = item.stat()
            result.append((s.st_mtime_ns, s.st_size))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def load_report(path):
    path = Path(path)
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    decks = decks_from_replay(blob)
    names = [d[0] for d in decks]
    report = None
    try:
        candidate = json.loads(sidecar(path).read_text(encoding='utf-8'))
        if (candidate.get('digest') == digest and candidate.get('version') == VERSION
                and [p.get('name') for p in candidate.get('players', [])] == names
                and all(Counter(p.get('main',[])) == Counter(d[1]) and Counter(p.get('extra',[])) == Counter(d[2])
                        for p,d in zip(candidate['players'],decks))):
            report = candidate
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if report is None:
        report = dict(version=VERSION, digest=digest, winner=None, result='unknown',
            playback='not_analyzed', warnings=[], _facts_base_only=True,
            players=[dict(index=i, name=n, main=m, extra=x) for i, (n,m,x) in enumerate(decks)])
    valid_live = False
    try:
        e = json.loads(sidecar(path, 'evidence').read_text(encoding='utf-8'))
        valid_live = (e.get('digest') == digest and e.get('players') == names
                      and type(e.get('winner')) is int and e['winner'] in (0,1,2))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    report['_live_evidence_present'] = valid_live
    # Opening hands are draws too. Read their actual count from the replay rather
    # than leaving live evidence unable to prove that the opening hand was covered.
    try:
        from .replay_native import parse_replay, ReplayError
        report['start_hand']=parse_replay(path).start_hand
    except (OSError, ValueError, ReplayError):
        pass
    if report.get('result_source') == 'live_server' and not valid_live:
        report.update(winner=None, result='unknown', result_source=None, _facts_base_only=True)
    apply_live_evidence(report, path)
    return report


def main():
    from .store import Store
    from .share import safe_replay_file
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--id', type=int, help='只同步这一份索引；省略则同步现存未到期录像')
    parser.add_argument('--details', type=str, help='展开指定录像指纹的原子记录')
    args = parser.parse_args()
    root = args.root.resolve()
    cfg = json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
    data = Path(cfg.get('data_dir') or root/'data')
    replays = Path(cfg.get('replay_dir') or data/'replays')
    now = time.time()
    store = Store(data/'bot.sqlite3')
    changed = skipped = failed = 0
    try:
        for row in store.all_replays():
            if args.id is not None and row['id'] != args.id:
                continue
            path = safe_replay_file(replays, row['rel_path'])
            if path is None:
                skipped += 1
                continue
            try:
                if store.import_replay_facts(row,path,load_report(path),now):changed += 1
                else:skipped += 1
            except (OSError,ValueError,TypeError,KeyError):
                failed += 1
        removed = store.purge_expired_facts(now)
        result = dict(synced_sources=changed,skipped_sources=skipped,failed_sources=failed,
                      purged_games=removed,live_unique_games=len(store.fact_games(replays,now)))
        if args.details:
            result['details'] = store.fact_details(args.details,replays,now)
        print(json.dumps(result,ensure_ascii=False))
    finally:
        store.close()
    return int(failed > 0)


if __name__ == '__main__':
    raise SystemExit(main())
