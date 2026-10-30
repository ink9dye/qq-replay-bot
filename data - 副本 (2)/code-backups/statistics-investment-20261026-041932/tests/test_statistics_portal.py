import json
import unittest
from pathlib import Path
from unittest.mock import patch
from bot.statistics_portal import StatisticsPortal,owner_secret
from bot.statistics_dashboard import query
import test_card_statistics as C
import test_top100 as T


class StatisticsPortalTests(unittest.TestCase):
    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)
        self.now=C.NOW
        self.portal=StatisticsPortal(self.store.path,self.replays,lambda:self.now,'x'*43)
        reply=self.call('/statistics/access/'+'x'*43)
        self.cookie=reply[3]['Set-Cookie'].split(';',1)[0]

    def call(self,path,cookie=None,host='127.0.0.1:8787',client='127.0.0.1',extra=None):
        headers={'Host':host}
        if cookie:headers['Cookie']=cookie
        headers.update(extra or {})
        return self.portal.route(path,headers,client)

    def test_cookie_required_tamper_expiry_and_share_token_not_authorized(self):
        for cookie in (None,'token='+'x'*43,self.cookie+'bad'):
            self.assertEqual(self.call('/statistics/api/roster',cookie)[0],403)
        self.assertEqual(self.call('/statistics/api/roster',self.cookie)[0],200)
        self.now+=18000
        self.assertEqual(self.call('/statistics/api/roster',self.cookie)[0],403)

    def test_tunnel_remote_rebinding_and_malformed_host_blocked(self):
        for host,client,extra in [('example.test','127.0.0.1',{}),('127.0.0.1','192.0.2.1',{}),
                ('[','127.0.0.1',{}),('localhost','127.0.0.1',{'X-Forwarded-For':'127.0.0.1'})]:
            self.assertEqual(self.call('/statistics/api/roster',self.cookie,host,client,extra)[0],403)
        headers=self.call('/statistics',self.cookie)[3]
        self.assertEqual(headers['Referrer-Policy'],'no-referrer')
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])

    def test_readonly_all100_missing_unidentified_and_latest_before_filter(self):
        before=self.store._db.total_changes
        result=json.loads(self.call('/statistics/api/roster',self.cookie)[2])
        self.assertEqual((result['current_players'],result['covered_players'],result['missing_players']),(100,2,98))
        self.assertEqual(len(result['people']),100)
        latest=max(self.store.current_top100_views(self.replays,C.NOW)['views'],key=lambda r:(r['recorded_at'],r['received_at'],r['digest']))
        person=next(p for p in result['people'] if p['player']==latest['player'])
        self.assertEqual(person['recorded_at'],latest['recorded_at'])
        self.assertEqual(before,self.store._db.total_changes)

    def test_build_source_scope_missing_file_expiry_dropout_and_exclusion(self):
        from urllib.parse import quote
        path='/statistics/api/build?player='+quote('甲')
        result=json.loads(self.call(path,self.cookie)[2])
        self.assertFalse(result['completeness']['side'])
        self.assertEqual(result['source']['expires_at'],self.store._db.execute('SELECT expires_at FROM stat_games WHERE digest=?',(result['source']['digest'],)).fetchone()[0])
        self.store.replace_top100(T.roster(['乙']),C.NOW)
        self.assertEqual(self.call(path,self.cookie)[0],404)
        self.store.replace_top100(T.roster(['甲']),C.NOW)
        for row in self.rows:self.store.set_fact_exclusion(row['digest'],'甲',reason='test')
        self.assertEqual(self.call(path,self.cookie)[0],404)
        for row in self.rows:self.store.set_fact_exclusion(row['digest'],'甲',reason=None)
        for file in self.replays.iterdir():file.unlink()
        self.assertEqual(self.call(path,self.cookie)[0],404)
        self.now=C.NOW+14*86400
        # 新会话不让鉴权到期掩盖事实到期。
        self.cookie=self.call('/statistics/access/'+'x'*43)[3]['Set-Cookie'].split(';',1)[0]
        self.assertEqual(self.call(path,self.cookie)[0],404)

    def test_stale_roster_retained_and_unknown_evidence_not_first_game(self):
        self.store.top100_failed(C.NOW,'timeout')
        result=json.loads(self.call('/statistics/api/roster',self.cookie)[2])
        self.assertTrue(result['roster']['stale']);self.assertEqual(len(result['people']),100)
        self.assertEqual({p.get('observation_kind') for p in result['people'] if p['observed']},{'initial_status_unknown'})

    def test_secret_created_once_invalid_not_replaced(self):
        path=self.root/'owner.key';secret=owner_secret(path)
        self.assertEqual(owner_secret(path),secret)
        path.write_text('invalid')
        with self.assertRaises(ValueError):owner_secret(path)
        self.assertEqual(path.read_text(),'invalid')

    def test_http_integration_bootstrap_redirect_cookie_and_share_unchanged(self):
        from bot.share import ShareApp,ShareServer
        from urllib.request import build_opener,HTTPCookieProcessor,Request
        from urllib.error import HTTPError
        from http.cookiejar import CookieJar
        app=ShareApp(self.store,self.replays,lambda:self.now,bind_port=0)
        app.statistics_portal=self.portal
        server=ShareServer(app);server.start();self.addCleanup(server.stop)
        base='http://127.0.0.1:'+str(server.httpd.server_address[1])
        opener=build_opener(HTTPCookieProcessor(CookieJar()))
        with opener.open(base+'/statistics/access/'+'x'*43) as r:
            self.assertEqual(r.status,200);self.assertEqual(r.url,base+'/statistics')
        with opener.open(base+'/statistics/api/roster') as r:self.assertEqual(json.load(r)['current_players'],100)
        with self.assertRaises(HTTPError) as caught:opener.open(base+'/s/'+'x'*43)
        self.assertEqual(caught.exception.code,404)
        caught.exception.close()
