"""按实际主卡／额外中的静态卡库系列归属取多标签，不从卡组名称猜测。"""
import hashlib
import json
import time
from pathlib import Path

_cache={}
_BUILD_CACHE={}
_BUILD_CACHE_LOADED=False
_BUILD_CACHE_LOCK=None
SEMANTICS='contains_official_series_in_observed_main_or_extra'


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
            if isinstance(value,dict) and isinstance(value.get('series'),list):
                _BUILD_CACHE[_tuple_key(key)]=value
    except (OSError,ValueError,TypeError):
        _BUILD_CACHE.clear()


def _tuple_key(value):
    # JSON 只有字符串键；这里保持与内存键一致的元组结构。
    if isinstance(value,tuple):return value
    try:
        return tuple(json.loads(value))
    except (ValueError,TypeError):
        return value


def _save_build_cache(db,revision=None):
    path=_cache_file(db)
    if path is None or not _BUILD_CACHE:return
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        entries={}
        for key,value in _BUILD_CACHE.items():
            if not isinstance(key,tuple):continue
            if revision is not None and key and key[0]!=revision:continue
            entries[json.dumps(list(key),ensure_ascii=False)]=value
        tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(saved=time.time(),entries=entries),ensure_ascii=False),encoding='utf-8')
        tmp.replace(path)
    except OSError:
        pass


def annotate(db,rows,catalog):
    """只读同一数据库快照；参考文件由查询入口固定一次，避免一请求混版本。"""
    _load_build_cache(db)
    dirty=False

    def build_key(row, build):
        return (catalog.get('revision'), build, row.get('classification_source'),
                bool(row.get('main_complete')), bool(row.get('extra_complete')),
                row.get('series_status'))

    # 全命中快路径：不再为已缓存的构筑查 stat_build_series/stat_build_cards。
    first_rows={}
    for row in rows:
        first_rows.setdefault(row['build'], row)
    hits={}
    misses=[]
    if catalog['status']=='ready':
        for build, row in first_rows.items():
            cached=_BUILD_CACHE.get(build_key(row, build))
            if cached is not None:
                hits[build]=cached
            else:
                misses.append(build)
    else:
        misses=list(first_rows)
    if not misses:
        for row in rows:
            cached=hits.get(row['build'])
            if cached is not None:
                row.update(cached)
        return rows
    builds=set(misses);stored={};cards={}
    # 只取本批构筑的行；全表扫描 46 万行在 2C2G 上要好几秒。
    for chunk in _chunks(builds):
        marks=','.join('?'*len(chunk))
        for row in db.execute('SELECT build,series FROM stat_build_series WHERE build IN ('+marks+')',chunk):
            stored.setdefault(row['build'],set()).add(row['series'])
    if catalog['status']=='ready':
        for chunk in _chunks(builds):
            marks=','.join('?'*len(chunk))
            for row in db.execute("SELECT build,partition,code,copies FROM stat_build_cards WHERE partition IN ('main','extra') AND build IN ("+marks+")",chunk):
                cards.setdefault(row['build'],[]).append(dict(row))
    results=dict(hits)
    revision=catalog.get('revision')
    for row in rows:
        build=row['build']
        if build not in results:
            key=build_key(row, build)
            if catalog['status']=='ready' and key in _BUILD_CACHE:
                results[build]=_BUILD_CACHE[key]
                row.update(results[build])
                continue
            saved=stored.get(build,set())
            if row.get('classification_source')=='manual' or catalog['status']!='ready':
                results[build]=dict(series=sorted(saved),series_status=row.get('series_status','unknown'),series_origin='manual' if row.get('classification_source')=='manual' else 'stored_reference',series_membership={})
            else:
                tags=set(saved);complete=bool(row.get('main_complete')) and bool(row.get('extra_complete'))
                counts={}
                for card in cards.get(build,[]):
                    item=catalog['cards'].get(card['code'])
                    if item is None:complete=False;continue
                    complete=complete and item['complete']
                    for tag in item['tags']:
                        tags.add(tag)
                        entry=counts.setdefault(tag,dict(main_copies=0,extra_copies=0))
                        entry[card['partition']+'_copies']+=card['copies']
                results[build]=dict(series=sorted(tags),series_status='known' if complete else 'partial' if tags else 'unknown',
                    series_origin=SEMANTICS,series_membership=counts)
                if revision is not None:
                    if len(_BUILD_CACHE)>50000:_BUILD_CACHE.clear()
                    _BUILD_CACHE[key]=results[build]
                    dirty=True
        row.update(results[build])
    if dirty:_save_build_cache(db,revision)
    return rows
