import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from bot.statistics_gateway import StatisticsGateway
from bot import statistics_proxy


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        key=Path(self.temp.name)/'key';key.write_text('x'*43)
        self.gateway=StatisticsGateway(key)

    def test_signer_matches_existing_portal(self):
        from bot.statistics_portal import StatisticsPortal
        portal=StatisticsPortal('.', '.', lambda:1000, 'x'*43)
        self.assertEqual(self.gateway._session(1600),portal._session(1600))

    def test_untrusted_local_route_never_sends_owner_cookie(self):
        for headers,client in [({'Host':'evil.test'},'127.0.0.1'),
                ({'Host':'localhost','X-Forwarded-For':'1.2.3.4'},'127.0.0.1'),
                ({'Host':'localhost'},'1.2.3.4')]:
            with patch.object(self.gateway,'_request',side_effect=AssertionError('must reject')):
                self.assertEqual(self.gateway.route('/statistics',headers,client)[0],403)

    def test_web_down_has_no_inprocess_fallback_get_or_post(self):
        app=SimpleNamespace(clock=lambda:1000,statistics_portal=self.gateway,
            store=SimpleNamespace(get_stats_link=lambda token:dict(expires_at=2000)))
        with patch.object(statistics_proxy,'_fetch',side_effect=OSError()),patch.object(
                self.gateway,'route',side_effect=AssertionError('no fallback')),patch.object(
                self.gateway,'post',side_effect=AssertionError('no fallback')):
            self.assertEqual(statistics_proxy.route(app,'/stats/t',['stats','t'])[0],503)
            self.assertEqual(statistics_proxy.post(app,'/stats/t/statistics/api/deck-naming',
                ['stats','t','statistics','api','deck-naming'],b'{}')[0],503)

    def test_redirect_cookie_and_download_headers_survive_http_bridge(self):
        seen=[]
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path,self.headers.get('Cookie')))
                self.send_response(303);self.send_header('Location','/statistics/devices')
                self.send_header('Set-Cookie','qqstats_owner=test; HttpOnly')
                self.send_header('Content-Disposition','attachment; filename=test.yrp');self.end_headers()
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),H)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.gateway.base='http://127.0.0.1:'+str(server.server_port)
        result=self.gateway.route('/statistics/access/key',{'Host':'localhost','Cookie':'owner'},'127.0.0.1')
        self.assertEqual(result[0],303);self.assertIn('Set-Cookie',result[3]);self.assertIn('Content-Disposition',result[3])
        self.gateway.public('/s/test/file/1')
        self.assertEqual(seen,[('/statistics/access/key','owner'),('/s/test/file/1',None)])

    def test_share_routes_bypass_local_business_in_collector(self):
        from bot.share import make_handler
        app=SimpleNamespace(statistics_portal=self.gateway)
        handler=object.__new__(make_handler(app));handler.path='/s/token/build?scope=all'
        with patch.object(self.gateway,'public',return_value=(200,'text/plain',b'ok',{})) as p:
            self.assertEqual(handler._route(['s','token','build'])[0],200)
            p.assert_called_once_with(handler.path)

    def test_missing_signing_key_never_reactivates_local_share_business(self):
        from bot.share import make_handler
        app=SimpleNamespace(statistics_portal=None,web_remote_only=True)
        handler=object.__new__(make_handler(app));handler.path='/s/token/build'
        self.assertEqual(handler._route(['s','token','build'])[0],503)
