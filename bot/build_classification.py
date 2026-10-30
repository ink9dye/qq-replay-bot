"""构筑类型与系列多标签独立存储；分类变更不改小局事实或保留期。"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
from collections import Counter
from pathlib import Path


def initialize(db):
    fields={r[1] for r in db.execute('PRAGMA table_info(stat_builds)')}
    for name,definition in [('classification_status',"TEXT NOT NULL DEFAULT 'unknown'"),
                            ('classification_source',"TEXT NOT NULL DEFAULT 'unclassified'"),
                            ('series_status',"TEXT NOT NULL DEFAULT 'unknown'"),
                            ('classification_score','REAL')]:
        if name not in fields:db.execute('ALTER TABLE stat_builds ADD COLUMN '+name+' '+definition)
    db.executescript('''
CREATE TABLE IF NOT EXISTS stat_build_series (
 build TEXT NOT NULL REFERENCES stat_builds(fingerprint) ON DELETE CASCADE,
 series TEXT NOT NULL, PRIMARY KEY(build,series)
);
CREATE TABLE IF NOT EXISTS stat_classification_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO stat_classification_state VALUES(1,0);
''')
    db.commit()


def assign(db,build,deck_type,series,version,source='manual',score=None):
    previous=db.execute('SELECT * FROM stat_builds WHERE fingerprint=?',(build,)).fetchone()
    if previous is None:return False
    if deck_type is not None and (not isinstance(deck_type,str) or not deck_type.strip() or len(deck_type)>120):
        raise ValueError('invalid_deck_type')
    if not isinstance(series,(list,tuple)) or any(not isinstance(s,str) or not s.strip() or len(s)>80 for s in series):
        raise ValueError('invalid_series')
    tags=sorted({s.strip() for s in series})
    label=deck_type.strip() if deck_type is not None else None
    status='recognized' if label else 'unknown'
    series_status='known' if tags else 'unknown'
    old_tags=[r[0] for r in db.execute('SELECT series FROM stat_build_series WHERE build=? ORDER BY series',(build,))]
    if (previous['deck_type'],previous['classification_version'],previous['classification_source'],
            previous['classification_status'],previous['classification_score'],previous['series_status'],old_tags)==(label,version,source,status,score,series_status,tags):
        return False
    db.execute('UPDATE stat_builds SET deck_type=?,classification_version=?,classification_source=?,classification_status=?,classification_score=?,series_status=? WHERE fingerprint=?',
               (label,version,source,status,score,series_status,build))
    db.execute('DELETE FROM stat_build_series WHERE build=?',(build,))
    db.executemany('INSERT INTO stat_build_series VALUES(?,?)',[(build,s) for s in tags])
    db.execute('UPDATE stat_classification_state SET revision=revision+1 WHERE singleton=1')
    return True


def rules(reference_path,overrides_path=None):
    raw=Path(reference_path).read_bytes()
    document=json.loads(raw.decode('utf-8-sig'))
    if not isinstance(document,dict):raise ValueError('invalid_reference_document')
    refs=document.get('decks',[])
    extra=Path(overrides_path).read_bytes() if overrides_path and Path(overrides_path).exists() else b'{}'
    cfg=json.loads(extra.decode('utf-8-sig'))
    if not isinstance(cfg,dict) or not isinstance(refs,list) or not isinstance(cfg.get('references',[]),list):
        raise ValueError('invalid_rule_document')
    threshold=float(cfg.get('minimum_similarity',0.80));margin=float(cfg.get('minimum_margin',0.08))
    if not 0.6<=threshold<=1 or not 0<=margin<=1:raise ValueError('invalid_classification_threshold')
    refs=refs+cfg.get('references',[])
    parsed={}
    for ref in refs:
        if not isinstance(ref,dict):raise ValueError('invalid_reference_rule')
        label=ref.get('label');main=ref.get('main',[]);extra_cards=ref.get('extra',[]);series=ref.get('series',[])
        if (not isinstance(label,str) or not label.strip() or not isinstance(main,list) or not main or not isinstance(extra_cards,list)
                or any(type(c) is not int or c<=0 for c in main+extra_cards)
                or not isinstance(series,list) or any(not isinstance(s,str) or not s.strip() for s in series)):
            raise ValueError('invalid_reference_rule')
        cards=Counter(main)+Counter(extra_cards)
        priority=ref.get('name_priority',0)
        if type(priority)is not int or not 0<=priority<=99999999:raise ValueError('invalid_name_priority')
        parsed[(label.strip(),tuple(sorted(cards.items())))]=dict(label=label.strip(),cards=cards,series=sorted(set(series)),name_priority=priority)
    version='reference-v5:'+hashlib.sha256(raw+b'\0'+extra).hexdigest()
    return list(parsed.values()),threshold,margin,version


def normalize_references(parsed,identities):
    groups={}
    for ref in parsed:
        cards=Counter()
        for code,copies in ref['cards'].items():cards[identities.get(code,(code,))[0]]+=copies
        key=tuple(sorted(cards.items()))
        groups.setdefault(key,[]).append(dict(ref,cards=cards))
    normalized=[]
    for key,refs in groups.items():
        # User permits either label for an identical reference deck. Seeded choice is
        # repeatable across refreshes and independent of input ordering or player name.
        newest=max(r.get('name_priority',0)for r in refs)
        by_label={r['label']:r for r in refs if r.get('name_priority',0)==newest}
        labels=sorted(by_label)
        chosen=random.Random(hashlib.sha256(repr(key).encode()).digest()).choice(labels)
        normalized.append(by_label[chosen])
    return normalized


def apply_rules(db,parsed,threshold,margin,version):
    from .card_identity import reference
    identities,identity_version,identity_status=reference(db)
    version=version+':identity:'+(identity_version or identity_status)
    normalize=lambda code:identities.get(code,(code,))[0]
    normalized=normalize_references(parsed,identities)
    from .statistics_match_identity import classification_builds
    allowed_builds=classification_builds(db)
    changed=examined=0
    builds=db.execute("SELECT * FROM stat_builds WHERE classification_source!='manual' AND (classification_version IS NULL OR classification_version!=?)",(version,)).fetchall()
    for build in builds:
        if build['fingerprint']not in allowed_builds:continue
        examined+=1;observed=Counter();candidates={}
        for row in db.execute("SELECT code,copies FROM stat_build_cards WHERE build=? AND partition IN ('main','extra')",(build['fingerprint'],)):
            observed[normalize(row['code'])]+=row['copies']
        if build['main_complete'] and build['extra_complete']:
            for ref in normalized:
                total=sum(observed.values())+sum(ref['cards'].values())
                score=2*sum((observed&ref['cards']).values())/total if total else 0
                old=candidates.get(ref['label'])
                priority=ref.get('name_priority',0)
                if old is None or score>old[0]or score==old[0]and priority>old[2]:candidates[ref['label']]=(score,ref['series'],priority)
        ordered=sorted(candidates.items(),key=lambda item:(-item[1][0],-item[1][2],item[0]))
        label=None;tags=[];score=ordered[0][1][0] if ordered else None
        # A unique exact full main+extra match is stronger than a nearby alternative.
        # Identical reference label ties were resolved above; fuzzy thresholds stay.
        exact_unique=bool(ordered and score==1 and (len(ordered)==1 or ordered[1][1][0]<1))
        peers=[item for item in ordered[1:]if item[1][2]>=ordered[0][1][2]]if ordered else[]
        # A newer best match competes with equally new references, not obsolete names.
        newer_clear=bool(ordered and ordered[0][1][2]>0 and (not peers or score>peers[0][1][0]and score-peers[0][1][0]>=margin))
        if ordered and score>=threshold and (exact_unique or newer_clear or len(ordered)==1 or (score>ordered[1][1][0] and score-ordered[1][1][0]>=margin)):
            label=ordered[0][0];tags=ordered[0][1][1]
        changed+=assign(db,build['fingerprint'],label,tags,version,'reference',score)
    return dict(examined=examined,changed=changed,rule_version=version,
                revision=db.execute('SELECT revision FROM stat_classification_state WHERE singleton=1').fetchone()[0])


def main():
    from .store import Store
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--refresh',action='store_true');p.add_argument('--unknown',action='store_true')
    p.add_argument('--set',dest='build');p.add_argument('--deck-type');p.add_argument('--series',action='append',default=[])
    args=p.parse_args();root=args.root.resolve()
    cfg=json.loads((root/'config.json').read_text(encoding='utf-8-sig'));data=Path(cfg.get('data_dir') or root/'data')
    store=Store(data/'bot.sqlite3')
    try:
        result={}
        if args.refresh:result['refresh']=store.refresh_classifications(root/'reference_decks.json',data/'classification-rules.json')
        if args.build:
            if not args.deck_type:p.error('--set requires --deck-type')
            result['changed']=store.classify_build(args.build,args.deck_type,args.series,'manual')
        if args.unknown:result['unknown']=store.unknown_builds(time.time())
        print(json.dumps(result,ensure_ascii=False))
    finally:store.close()


if __name__=='__main__':main()
