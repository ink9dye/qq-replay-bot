"""通过PyCharm官方本机MCP启动唯一watch运行配置；连接失败不后台观战。"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
RUN_NAME = 'watch · 全部观战'


class IDEError(RuntimeError): pass


class IDEClient:
    def __init__(self, url, timeout=8):
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','localhost','::1')
                or parsed.username or parsed.password or parsed.path.rstrip('/') not in ('/mcp','/stream')
                or parsed.query or parsed.fragment):
            raise IDEError('PyCharm自动启动接口必须是本机的HTTP /mcp 或 /stream 地址')
        self.url, self.timeout, self.session, self.sequence = url, timeout, None, 0
        self.protocol='2025-03-26'
        self.opener = build_opener(ProxyHandler({}))

    def request(self, method, params=None, notification=False):
        self.sequence += 1
        payload = {'jsonrpc':'2.0','method':method}
        if params is not None: payload['params'] = params
        if not notification: payload['id'] = self.sequence
        headers = {'Content-Type':'application/json','Accept':'application/json, text/event-stream',
                   'MCP-Protocol-Version':self.protocol}
        if self.session: headers['Mcp-Session-Id'] = self.session
        request = Request(self.url,data=json.dumps(payload).encode('utf-8'),headers=headers,method='POST')
        with self.opener.open(request,timeout=self.timeout) as response:
            self.session = response.headers.get('Mcp-Session-Id') or self.session
            if response.status == 202 or notification: return {}
            if 'text/event-stream' in response.headers.get('Content-Type',''):
                data=[]
                for _ in range(10000):
                    line=response.readline(65537)
                    if len(line)>65536: raise IDEError('IDE接口消息过大')
                    if not line: break
                    line=line.decode('utf-8').rstrip('\r\n')
                    if line.startswith('data:'): data.append(line[5:].lstrip())
                    elif not line and data:
                        event=json.loads('\n'.join(data));data=[]
                        if event.get('id') == payload.get('id'):
                            return self._result(event)
                raise IDEError('IDE接口没有返回操作结果')
            body=response.read(2*1024*1024+1)
            if len(body)>2*1024*1024: raise IDEError('IDE接口响应过大')
            return self._result(json.loads(body))

    @staticmethod
    def _result(message):
        if 'error' in message: raise IDEError('PyCharm拒绝了自动启动请求')
        return message.get('result',{})

    def initialize(self):
        result=self.request('initialize',{'protocolVersion':'2025-03-26','capabilities':{},
            'clientInfo':{'name':'qq-replay-bot-local-launcher','version':'1'}})
        self.protocol=result.get('protocolVersion',self.protocol)
        self.request('notifications/initialized',notification=True)
        tools=self.request('tools/list').get('tools',[])
        self.tools={tool['name']:tool for tool in tools}
        return self

    def call(self,name,arguments):
        if name not in self.tools: raise IDEError('PyCharm未开放所需工具：'+name)
        result=self.request('tools/call',{'name':name,'arguments':arguments})
        if result.get('isError'): raise IDEError('PyCharm未能执行：'+name)
        return result

    def start_watch(self,root):
        project=str(root.resolve())
        self.call('open_file_in_editor',{'filePath':'watch.py','projectPath':project})
        tool=self.tools.get('execute_run_configuration',{})
        properties=tool.get('inputSchema',{}).get('properties',{})
        # 兼容已安装版本与官方新版本的配置名字段；只执行固定配置，不传动态参数。
        name_field='configurationName' if 'configurationName' in properties else 'name'
        args={name_field:RUN_NAME,'projectPath':project}
        if 'waitForExit' in properties: args['waitForExit']=False
        if 'timeout' in properties: args['timeout']=1000
        return self.call('execute_run_configuration',args)


def launch(root=ROOT, *, open_ide=True, client_factory=IDEClient, pause=time.sleep):
    config_path=root/'data/pycharm-link.json'
    try: config=json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        raise IDEError('尚未完成PyCharm自动运行授权；请先在PyCharm运行watch.py。') from None
    executable=Path(config.get('executable',''))
    url=config.get('mcp_url','')
    # 先验证地址，再启动IDE；不连接公网，不下载插件或程序。
    client=client_factory(url)
    for attempt in range(15):
        try:
            client.initialize()
            break
        except (OSError,ValueError,IDEError):
            if attempt==14: raise IDEError('无法在PyCharm启动watch；未启动任何后台观战。请检查IDE接口与执行授权。') from None
            if attempt==0 and open_ide:
                if not executable.is_file(): raise IDEError('找不到已安装的PyCharm')
                subprocess.Popen([str(executable),str(root/'watch.py')],cwd=root)
            pause(2)
    # 执行请求只能发一次，避免IDE确认或慢启动期间重复运行。
    existing = watch_status(root)
    if existing:
        client.call('open_file_in_editor', {'filePath':'watch.py','projectPath':str(root.resolve())})
        print('PyCharm中的watch.py已运行，继续使用原Run窗口，不重复启动。',flush=True)
        return True
    client.start_watch(root)
    print('已请求PyCharm打开并运行watch.py；请在Run窗口查看全部观战日志。',flush=True)
    return True


def watch_status(root):
    try:
        config=json.loads((root/'config.json').read_text(encoding='utf-8-sig'))
        port=int((config.get('share') or {}).get('bind_port',8787))
        if not 1 <= port <= 65535: return None
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:'+str(port)+'/health',timeout=1) as response:
            result=json.loads(response.read(4096))
        if (result.get('service')=='qq-replay-bot' and result.get('watch_entry')=='pycharm/watch.py'
                and type(result.get('watch_pid')) is int and result['watch_pid']>0):
            return result
    except (OSError,ValueError,TypeError): pass
    return None


def main():
    parser=argparse.ArgumentParser(description='在PyCharm启动全部观战')
    parser.add_argument('--check',action='store_true',help='只检查本机IDE接口，不打开文件或运行程序')
    args=parser.parse_args()
    try:
        if args.check:
            config=json.loads((ROOT/'data/pycharm-link.json').read_text(encoding='utf-8'))
            client=IDEClient(config['mcp_url']).initialize()
            print('PyCharm本机接口可用，工具：'+', '.join(name for name in
                ('open_file_in_editor','execute_run_configuration') if name in client.tools))
        else: launch()
    except (IDEError,OSError,ValueError) as exc:
        print(str(exc),flush=True)
        raise SystemExit(2)


if __name__=='__main__': main()
