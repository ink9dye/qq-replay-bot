import importlib.util
import json
import tempfile
import threading
import unittest
import os
import sys
import types
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def module(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result
P=module('local_ide_link','bot/pycharm_link.py')
T=module('local_watch_targets','bot/watch_targets.py')


class IDETests(unittest.TestCase):
    def test_watch_runtime_requires_ide_and_uses_the_shared_single_instance_lock(self):
        # 不启动QQ或网络，用替身验证真实watch入口的控制关系。
        spec=importlib.util.spec_from_file_location('watch_runtime_under_test',ROOT/'watch.py')
        watch=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'watch_runtime_under_test':watch}):
            spec.loader.exec_module(watch)
            with patch.dict(os.environ,{},clear=True):
                with self.assertRaises(RuntimeError): watch.run_pycharm_watch()
            calls=[]
            fake_bot=types.ModuleType('bot');fake_bot.__path__=[]
            fake_launcher=types.ModuleType('bot.launcher')
            fake_main=types.ModuleType('bot.main')
            @contextmanager
            def lock(root,name):
                calls.append(('lock',name));yield True
            fake_launcher.startup_lock=lock
            fake_main.run_main=lambda **args:calls.append(('run',args))
            with patch.dict(os.environ,{'QQ_WATCH_IN_PYCHARM':'1'}), patch.dict(sys.modules,
                {'bot':fake_bot,'bot.launcher':fake_launcher,'bot.main':fake_main}):
                watch.run_pycharm_watch()
                self.assertEqual(calls,[('lock','bot-runtime.lock'),('run',{'controlled_by_pycharm':True})])
                self.assertIs(sys.modules['watch'],watch)
                calls.clear()
                @contextmanager
                def busy(root,name):yield False
                fake_launcher.startup_lock=busy
                watch.run_pycharm_watch()
                self.assertEqual(calls,[])

    def test_only_loopback_mcp_is_accepted(self):
        for url in ('https://example.com/mcp','http://example.com/mcp','http://127.0.0.1/file',
                    'http://user:secret@127.0.0.1/mcp','http://127.0.0.1/mcp?x=1'):
            with self.assertRaises(P.IDEError): P.IDEClient(url)
        P.IDEClient('http://127.0.0.1:64342/mcp')
        P.IDEClient('http://127.0.0.1:64342/stream')

    def test_existing_pycharm_watch_only_opens_file_without_rerunning(self):
        calls=[]
        class Client:
            def __init__(self,*a):pass
            def initialize(self):return self
            def call(self,*a):calls.append(a)
            def start_watch(self,*a):raise AssertionError('不能重启已有观战')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'data').mkdir()
            (root/'data/pycharm-link.json').write_text(json.dumps({'mcp_url':'http://127.0.0.1/stream'}))
            with patch.object(P,'watch_status',return_value={'watch_pid':123}), patch.object(P.subprocess,'Popen') as spawn:
                self.assertTrue(P.launch(root,open_ide=True,client_factory=Client))
                spawn.assert_not_called()
        self.assertEqual([c[0] for c in calls],['open_file_in_editor'])

    def test_official_rpc_session_and_fixed_configuration_for_both_schema_versions(self):
        for field in ('name','configurationName'):
            calls=[]
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    msg=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    calls.append(msg)
                    result={}
                    if msg['method']=='initialize': result={'protocolVersion':'2025-03-26'}
                    elif msg['method']=='tools/list':
                        result={'tools':[{'name':'open_file_in_editor'},
                            {'name':'execute_run_configuration','inputSchema':{'properties':{
                                field:{'type':'string'},'waitForExit':{'type':'boolean'},'timeout':{'type':'integer'}}}}]}
                    if 'id' not in msg:
                        self.send_response(202);self.end_headers();return
                    self.send_response(200)
                    self.send_header('Mcp-Session-Id','test-local-session')
                    self.send_header('Content-Type','text/event-stream')
                    self.end_headers()
                    self.wfile.write(('data: '+json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result})+'\n\n').encode())
                def log_message(self,*args):pass
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            try:
                client=P.IDEClient('http://127.0.0.1:'+str(server.server_address[1])+'/mcp').initialize()
                client.start_watch(ROOT)
                self.assertEqual(client.session,'test-local-session')
                executed=[r['params'] for r in calls if r['method']=='tools/call']
                self.assertEqual([r['name'] for r in executed],['open_file_in_editor','execute_run_configuration'])
                self.assertEqual(executed[-1]['arguments'][field],P.RUN_NAME)
                self.assertFalse(executed[-1]['arguments']['waitForExit'])
                self.assertNotIn('programArguments',executed[-1]['arguments'])
            finally:server.shutdown();server.server_close()

    def test_no_configuration_never_starts_a_background_watch(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(P.subprocess,'Popen') as spawn:
            with self.assertRaises(P.IDEError):P.launch(Path(temp))
            spawn.assert_not_called()

    def test_run_request_is_not_retried_when_ide_refuses_it(self):
        class Client:
            count=0
            def __init__(self,*a):pass
            def initialize(self):return self
            def start_watch(self,*a):
                Client.count+=1
                raise P.IDEError('confirmation required')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'data').mkdir()
            (root/'data/pycharm-link.json').write_text(json.dumps({'mcp_url':'http://127.0.0.1/mcp'}))
            with self.assertRaises(P.IDEError):P.launch(root,open_ide=False,client_factory=Client)
        self.assertEqual(Client.count,1)

    def test_independent_targets_merge_and_keep_last_good_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'watch-config.json'
            path.write_text(json.dumps({'watch_any':['甲'],'watch_names':['甲','乙'],
                'watch_vs_top':[{'name':'丙','top':100}, {'name':'乙','top':10}]}),encoding='utf-8')
            self.assertEqual(T.standalone_targets(path),['甲','乙','丙'])
            path.write_text('{',encoding='utf-8')
            self.assertEqual(T.standalone_targets(path),['甲','乙','丙'])

    def test_shared_run_configuration_has_one_watch_script(self):
        import xml.etree.ElementTree as ET
        config=ET.parse(ROOT/'.idea/runConfigurations/watch_all.xml').getroot().find('configuration')
        self.assertEqual(config.attrib['name'],P.RUN_NAME)
        options={o.attrib['name']:o.attrib['value'] for o in config.findall('option')}
        self.assertEqual(options['SCRIPT_NAME'],'$PROJECT_DIR$/watch.py')
        self.assertEqual(options['PARAMETERS'],'--unified')
        self.assertNotEqual(config.attrib.get('allowRunningInParallel'),'true')


if __name__=='__main__':unittest.main()
