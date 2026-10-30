"""公开卡名缓存以cid为资料键；所有统计与录像卡号必须使用条目id。"""
import json,threading
from pathlib import Path
from .card_search import card_name,normalize

_cache={}
_lock=threading.RLock()


def public_index(data_dir):
    path=Path(data_dir)/'cache/cards.json'
    try:
        stat=path.stat();stamp=(stat.st_mtime_ns,stat.st_size)
        with _lock:
            previous=_cache.get(str(path))
            if previous and previous[0]==stamp:return previous[1]
            raw=json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(raw,dict):return {}
            result={}
            for key,info in raw.items():
                if not isinstance(info,dict):continue
                # 兼容旧的按卡片密码直接索引、且不带cid/id字段的公开资料。
                # 只要有id/cid字段，就绝不能把资料字典键当作卡片密码。
                value=info.get('id') if 'id' in info or 'cid' in info else key
                if type(value) is int:code=value
                elif isinstance(value,str) and value.isdigit():code=int(value)
                else:continue
                if not 0<code<=4294967295:continue
                entry=dict(info,id=code)
                if not isinstance(entry.get('text'),dict):entry['text']={}
                result[str(code)]=entry
            _cache[str(path)]=(stamp,result)
            if len(_cache)>16:_cache.pop(next(iter(_cache)))
            return result
    except (OSError,ValueError):return {}


def index(data_dir):
    from .statistics_local_cards import local_index
    result=dict(public_index(data_dir))
    for code,local in local_index(data_dir).items():
        result[code]=dict(result.get(code,{}),**local)
    return result


def search(data_dir,term,limit=20):
    cards=index(data_dir);term=term.strip();needle=normalize(term)
    if term.isdigit() and 0<int(term)<=4294967295:
        code=int(term);info=cards.get(str(code))
        return [dict(code=code,name=card_name(info) if info else '卡号'+str(code)+'（名称暂无）')]
    found=[]
    for code,info in cards.items():
        values=[info.get(k,'') for k in ('cn_name','sc_name','md_name','jp_name','en_name','nwbbs_n','cnocg_n')]
        values.append((info.get('text') or {}).get('name',''))
        if needle and any(needle in normalize(value) for value in values):
            found.append(dict(code=int(code),name=card_name(info)))
        if len(found)>=limit:break
    return found
