"""Fixed-category SSH diagnostics and token-free /health probes."""
import json,os,re,socket,ssl,threading,time
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
from urllib.error import HTTPError,URLError


def ssh_error_category(line):
    text=line.lower()
    for needles,category in [
        (('host key verification failed','remote host identification has changed'),'host_key'),
        (('could not resolve hostname','name or service not known','getaddrinfo'),'dns'),
        (('connection timed out','operation timed out'),'timeout'),
        (('connection refused',),'connection_refused'),
        (('network is unreachable','no route to host'),'network_unreachable'),
        (('connection reset',),'connection_reset'),
        (('broken pipe',),'broken_pipe'),
        (('permission denied','authentication failed'),'authentication'),
        (('remote port forwarding failed','forwarding request failed'),'forward_rejected'),
        (('kex_exchange_identification','key exchange failed'),'handshake'),
        (('connection closed','disconnected from','received disconnect'),'remote_closed')]:
        if any(v in text for v in needles):return category
    return None


def probe(url):
    try:
        opener=build_opener(ProxyHandler({}))
        with opener.open(url,timeout=4) as response:
            raw=response.read(8193)
        if len(raw)>8192:return 'invalid_health'
        value=json.loads(raw)
        return None if isinstance(value,dict) and value.get('service')=='qq-replay-bot' else 'unexpected_service'
    except HTTPError as error:return 'http_'+str(error.code)
    except URLError as error:
        reason=error.reason
        if isinstance(reason,socket.gaierror):return 'dns'
        if isinstance(reason,ssl.SSLError):return 'tls'
        if isinstance(reason,(TimeoutError,socket.timeout)):return 'timeout'
        return 'connection'
    except (TimeoutError,socket.timeout):return 'timeout'
    except ssl.SSLError:return 'tls'
    except OSError:return 'connection'
    except (ValueError,TypeError):return 'invalid_health'


class Journal:
    def __init__(self,directory,limit=1024*1024):
        self.path=Path(directory)/'tunnel-diagnostics.jsonl';self.limit=limit;self.lock=threading.Lock()
    def emit(self,event,**fields):
        if not re.fullmatch('[a-z_]{1,48}',event):return
        row=dict(time=time.time(),version=2,pid=os.getpid(),event=event)
        for k,v in fields.items():
            if k not in ('category','exit_code','failures','retry_seconds','outage_seconds','available'):continue
            if type(v) in (int,float,bool) or v is None:row[k]=v
            elif isinstance(v,str) and re.fullmatch('[a-z_0-9]{1,64}',v):row[k]=v
        raw=json.dumps(row)+'\n'
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                files=[self.path.with_name(self.path.name+s) for s in ('','.1','.2')]
                for file in files:
                    if file.exists() and time.time()-file.stat().st_mtime>7*86400:file.unlink()
                if self.path.exists() and self.path.stat().st_size+len(raw)>self.limit:
                    files[2].unlink(missing_ok=True)
                    if files[1].exists():files[1].replace(files[2])
                    files[0].replace(files[1])
                with self.path.open('a',encoding='utf-8') as stream:stream.write(raw)
        except OSError:pass
