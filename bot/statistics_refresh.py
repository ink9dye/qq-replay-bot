"""Bounded single-flight refresh for expensive web queries; no business rules."""
import hashlib
import json
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

MAX_KEYS = 16
MAX_BYTES = 8 * 1024 * 1024
MAX_AGE = 180
COOLDOWN = 5


class QueryRefresh:
    def __init__(self, database):
        self.directory=Path(database).parent/'query-cache'/'last-success'
        self.log=Path(database).parent/'logs'/'statistics-query-timings.jsonl'
        self.lock=threading.Lock();self.items=OrderedDict();self.pending={};self.errors=OrderedDict()
        self.jobs=queue.Queue(maxsize=8)
        threading.Thread(target=self._worker,name='statistics-refresh',daemon=True).start()

    def _path(self,key):
        name=hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()
        return self.directory/(name+'.json')

    def _read(self,key):
        try:
            path=self._path(key)
            if path.stat().st_size>MAX_BYTES:return None
            item=json.loads(path.read_text(encoding='utf8'))
            if not 0<=time.time()-item['saved']<=MAX_AGE:return None
            result=item['result'];result[2]=result[2].encode('utf8')
            if result[0]!=200 or not isinstance(result[3],dict) or not isinstance(json.loads(result[2]),dict):return None
            return dict(saved=item['saved'],stamp=None,result=tuple(result),size=len(result[2]))
        except (OSError,ValueError,TypeError,KeyError,IndexError):return None

    def _remember(self,key,item):
        self.items[key]=item;self.items.move_to_end(key)
        while len(self.items)>MAX_KEYS or sum(x['size'] for x in self.items.values())>MAX_BYTES:
            self.items.popitem(last=False)

    @staticmethod
    def _present(item,state):
        status,kind,body,headers=item['result']
        payload=json.loads(body)
        payload['_query_refresh']=dict(state=state,saved_at=item['saved'],refreshing=state=='refreshing')
        extra=dict(headers,**{'X-Statistics-State':state,'X-Statistics-Saved-At':str(item['saved'])})
        return status,kind,json.dumps(payload,ensure_ascii=False).encode('utf8'),extra

    def get(self,key,compute,version,ttl,wait=1.0):
        try:stamp=version()
        except OSError:stamp=None
        now=time.time()
        with self.lock:
            item=self.items.get(key)
            if item is None:
                item=self._read(key)
                if item:self._remember(key,item)
            age=now-item['saved'] if item else MAX_AGE+1
            if item and 0<=age<=ttl and stamp is not None and item['stamp']==stamp:
                return self._present(item,'fresh')
            event=self.pending.get(key)
            if event is None and (item is None or age>=COOLDOWN):
                event=threading.Event()
                try:
                    self.jobs.put_nowait((key,compute,version,event));self.pending[key]=event
                except queue.Full:event=None
            if item and 0<=age<=MAX_AGE:
                return self._present(item,'refreshing')
            error=self.errors.get(key)
            if error and now-error[0]<COOLDOWN:return error[1]
        if event:
            event.wait(wait)
            with self.lock:
                item=self.items.get(key)
                if item and 0<=time.time()-item['saved']<=MAX_AGE:
                    return self._present(item,'fresh' if key not in self.pending and item['stamp']==stamp else 'refreshing')
                error=self.errors.get(key)
                if error:return error[1]
        return 503,'application/json; charset=utf-8',json.dumps(dict(
            error='正在准备统计结果，请稍候。',code='query_busy',retry_after=3),ensure_ascii=False).encode(),{'Retry-After':'3'}

    def _persist(self,key,item):
        try:
            self.directory.mkdir(parents=True,exist_ok=True)
            result=list(item['result']);result[2]=result[2].decode('utf8')
            path=self._path(key);tmp=path.with_suffix('.tmp')
            tmp.write_text(json.dumps(dict(saved=item['saved'],result=result),ensure_ascii=False),encoding='utf8')
            tmp.chmod(0o600)
            tmp.replace(path)
            paths=sorted(self.directory.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
            total=0
            for i,p in enumerate(paths):
                total+=p.stat().st_size
                if i>=MAX_KEYS or total>MAX_BYTES:p.unlink(missing_ok=True)
        except OSError:pass

    def _worker(self):
        while True:
            key,compute,version,event=self.jobs.get();started=time.monotonic();status=500
            try:
                stamp=version();result=compute();status=result[0]
                if status==200 and len(result[2])<=MAX_BYTES//2 and isinstance(json.loads(result[2]),dict):
                    item=dict(saved=time.time(),stamp=stamp,result=result,size=len(result[2]))
                    with self.lock:self._remember(key,item)
                    self._persist(key,item)
                elif status!=200:
                    with self.lock:self.errors[key]=(time.time(),result)
            except Exception as exc:
                status=500
                if isinstance(exc,ValueError):status=400
                result=(status,'application/json; charset=utf-8',json.dumps(dict(
                    error=str(exc) if status==400 else '统计计算暂未完成，请稍后重试。'),ensure_ascii=False).encode(),{})
                with self.lock:self.errors[key]=(time.time(),result)
            finally:
                with self.lock:
                    self.pending.pop(key,None)
                    if status==200:self.errors.pop(key,None)
                    while len(self.errors)>MAX_KEYS:self.errors.popitem(last=False)
                event.set()
                try:
                    self.log.parent.mkdir(parents=True,exist_ok=True)
                    if self.log.exists() and self.log.stat().st_size>1024*1024:self.log.replace(self.log.with_suffix('.previous'))
                    with self.log.open('a',encoding='utf8') as f:f.write(json.dumps(dict(
                        at=time.time(),kind=key[0],status=status,elapsed_ms=round((time.monotonic()-started)*1000)))+'\n')
                except OSError:pass
                self.jobs.task_done()
