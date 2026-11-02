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


_merged={}
_merged_lock=threading.RLock()


def index(data_dir):
    """公开卡名 + 本地卡名的合并索引；两张来源表都按文件指纹缓存，这里再缓存合并结果。

    返回的对象只读（调用方都只 get），避免每次请求重新复制两万条字典（约 0.5 秒）。
    """
    from .statistics_local_cards import local_index
    public=public_index(data_dir)
    local=local_index(data_dir)
    key=(id(public),id(local))
    with _merged_lock:
        hit=_merged.get(key)
        if hit is not None:
            return hit[2]
    result=dict(public)
    for code,local_info in local.items():
        result[code]=dict(result.get(code,{}),**local_info)
    with _merged_lock:
        _merged[key]=(public,local,result)
        if len(_merged)>8:
            _merged.pop(next(iter(_merged)))
    return result


def search(data_dir,term,limit=20):
    cards=index(data_dir);term=term.strip();needle=normalize(term)
    if term.isdigit() and 0<int(term)<=4294967295:
        code=int(term);info=cards.get(str(code))
        if not info:return [dict(code=code,name='卡号'+str(code)+'（名称暂无）')]
        name=card_name(info)
        minimum=min((int(other) for other,value in cards.items() if card_name(value)==name),default=code)
        return [dict(code=minimum,name=name)]
    found={}
    for code,info in cards.items():
        values=[info.get(k,'') for k in ('cn_name','sc_name','md_name','jp_name','en_name','nwbbs_n','cnocg_n')]
        values.append((info.get('text') or {}).get('name',''))
        if needle and any(needle in normalize(value) for value in values):
            name=card_name(info);code=int(code)
            if name not in found or code<found[name]:
                found[name]=code
    return [dict(code=code,name=name) for name,code in sorted(found.items(),key=lambda item:item[1])[:limit]]
