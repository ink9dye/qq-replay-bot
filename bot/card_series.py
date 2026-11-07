"""按实际主卡／额外中的静态卡库系列归属取多标签，不从卡组名称猜测。"""
import hashlib
import json
import time
from pathlib import Path

_cache={}
# key -> (series 元组, status, origin, ((tag, main_copies, extra_copies), ...))
# 用紧凑元组而不是嵌套 dict：5000+ 构筑时内存约为原来的 1/4。
_BUILD_CACHE={}
_BUILD_CACHE_LOADED=False
SEMANTICS='contains_official_series_in_observed_main_or_extra'
_CHUNK=250
_MAX_ENTRIES=20000


def setcodes(value):
    value=int(value)&((1<<64)-1)
    return sorted({(value>>shift)&0xffff for shift in range(0,64,16)}-{0})


def matches_setcode(card_setcode,family):
    return bool(card_setcode) and card_setcode&0xfff==family&0xfff and card_setcode&(family&0xf000)==family&0xf000


def reference(db):
    database=next((r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'),None)
    if not database:return dict(status='unavailable',revision=None,cards={})
    path=Path(database).parent/'card-series-reference.json'
    if not path.is_file():return dict(status='unavailable',revision=None,cards={})
    try:
        key=str(path.resolve());stamp=(path.stat().st_mtime_ns,path.stat().st_size)
        if key not in _cache or _cache[key][0]!=stamp:
            raw=path.read_bytes();data=json.loads(raw)
            if not isinstance(data,dict) or data.get('version')!=1 or data.get('semantics')!=SEMANTICS or not isinstance(data.get('cards'),dict):raise ValueError('invalid_series_reference')
            cards={}
            for code,row in data['cards'].items():
                if not str(code).isdigit() or int(code)<=0 or not isinstance(row,dict):raise ValueError('invalid_series_card')
                tags=row.get('tags')
                if not isinstance(tags,list) or any(not isinstance(t,str) or not t.strip() or len(t)>80 for t in tags) or type(row.get('complete')) is not bool:raise ValueError('invalid_series_tags')
                cards[int(code)]=dict(tags=sorted(set(tags)),complete=row['complete'])
            catalog=dict(status='ready',revision=hashlib.sha256(raw).hexdigest(),cards=cards)
            _cache[key]=(stamp,catalog)
        return _cache[key][1]
    except (OSError,ValueError,TypeError):return dict(status='invalid_reference',revision=None,cards={})


def _chunks(values, size=500):
    values = tuple(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _cache_file(db):
    try:
        database=next((r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'),None)
        return (Path(database).parent/'cache'/'build-series.json') if database else None
    except Exception:
        return None


def _load_build_cache(db):
    global _BUILD_CACHE_LOADED
    if _BUILD_CACHE_LOADED:return
    _BUILD_CACHE_LOADED=True
    path=_cache_file(db)
    if path is None or not path.is_file():return
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
        entries=payload.get('entries') if isinstance(payload,dict) else None
        if not isinstance(entries,dict):return
        for key,value in entries.items():
            try:
                key=tuple(json.loads(key))
                series,status,origin,membership=value
                if not isinstance(series,list) or not isinstance(membership,list):continue
                compact=(tuple(str(s) for s in series),str(status),str(origin),
                         tuple((str(tag),int(main),int(extra)) for tag,main,extra in membership))
            except (ValueError,TypeError):
                continue
            _BUILD_CACHE[key]=compact
    except (OSError,ValueError,TypeError):
        _BUILD_CACHE.clear()


def _save_build_cache(db, revision=None):
    path=_cache_file(db)
    if path is None or not _BUILD_CACHE:return
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        entries={}
        for key,value in _BUILD_CACHE.items():
            if not isinstance(key,tuple):continue
            if revision is not None and key and key[0]!=revision:continue
            series,status,origin,membership=value
            entries[json.dumps(list(key),ensure_ascii=False)]=[list(series),status,origin,
                                                               [list(item) for item in membership]]
            if len(entries)>=_MAX_ENTRIES:break
        tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(saved=time.time(),entries=entries),ensure_ascii=False),
                       encoding='utf-8')
        tmp.replace(path)
    except OSError:
        pass


def _expand(compact):
    series,status,origin,membership=compact
    return dict(series=list(series),series_status=status,series_origin=origin,
                series_membership={tag:dict(main_copies=main,extra_copies=extra)
                                   for tag,main,extra in membership})


def _compact(value):
    rows=[]
    for tag,item in (value.get('series_membership') or {}).items():
        if isinstance(item,dict):
            main=item.get('main_copies');extra=item.get('extra_copies')
        else:
            main,extra=(item[0],item[1]) if len(item)>=2 else (0,0)
        rows.append((str(tag),int(main or 0),int(extra or 0)))
    membership=tuple(sorted(rows))
    return (tuple(value.get('series') or ()),str(value.get('series_status') or 'unknown'),
            str(value.get('series_origin') or ''),membership)


def _compute(db, build, sample, catalog, revision):
    """单个构筑的标注；stored/cards 由调用方按块提供。"""
    stored=_STORED.get(build,()) if _STORED else ()
    if sample.get('classification_source')=='manual' or catalog['status']!='ready':
        value=dict(series=sorted(stored),series_status=sample.get('series_status','unknown'),
                   series_origin='manual' if sample.get('classification_source')=='manual' else 'stored_reference',
                   series_membership={})
        return value
    tags=set(stored);complete=bool(sample.get('main_complete')) and bool(sample.get('extra_complete'))
    counts={}
    for card in (_CARDS.get(build) or ()):
        item=catalog['cards'].get(card['code'])
        if item is None:complete=False;continue
        complete=complete and item['complete']
        for tag in item['tags']:
            tags.add(tag)
            entry=counts.setdefault(tag,[0,0])
            entry[0 if card['partition']=='main' else 1]+=card['copies']
    return dict(series=sorted(tags),series_status='known' if complete else 'partial' if tags else 'unknown',
                series_origin=SEMANTICS,series_membership=counts)


_STORED={}
_CARDS={}


def annotate(db,rows,catalog):
    """只读同一数据库快照；参考文件由查询入口固定一次，避免一请求混版本。

    分块计算（每块 250 个构筑）并即时写回行、只保留紧凑缓存，峰值内存有界。
    """
    global _STORED,_CARDS
    _load_build_cache(db)
    revision=catalog.get('revision')
    rows_by_build={}
    for row in rows:
        rows_by_build.setdefault(row['build'],[]).append(row)

    def build_key(row,build):
        return (revision,build,row.get('classification_source'),
                bool(row.get('main_complete')),bool(row.get('extra_complete')),
                row.get('series_status'))

    hits=[];misses=[]
    for build,group in rows_by_build.items():
        cached=_BUILD_CACHE.get(build_key(group[0],build)) if catalog['status']=='ready' else None
        (hits if cached is not None else misses).append((build,group,cached))
    dirty=False
    for build,group,cached in hits:
        value=_expand(cached)
        for row in group:row.update(value)
    for chunk in _chunks([build for build,_g,_c in misses],_CHUNK):
        _STORED={};_CARDS={}
        if catalog['status']=='ready':
            marks=','.join('?'*len(chunk))
            for row in db.execute('SELECT build,partition,code,copies FROM stat_build_cards '
                                  "WHERE partition IN ('main','extra') AND build IN ("+marks+')',chunk):
                _CARDS.setdefault(row['build'],[]).append(dict(row))
        marks=','.join('?'*len(chunk))
        for row in db.execute('SELECT build,series FROM stat_build_series WHERE build IN ('+marks+')',chunk):
            _STORED.setdefault(row['build'],[]).append(row['series'])
        for build in chunk:
            group=rows_by_build[build]
            value=_compute(db,build,group[0],catalog,revision)
            if catalog['status']=='ready' and group[0].get('classification_source')!='manual':
                if len(_BUILD_CACHE)>_MAX_ENTRIES:_BUILD_CACHE.clear()
                _BUILD_CACHE[build_key(group[0],build)]=_compact(value)
                dirty=True
            for row in group:row.update(value)
        _STORED={};_CARDS={}
    if dirty:_save_build_cache(db,revision)
    return rows
