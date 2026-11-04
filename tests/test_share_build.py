import io,json,threading,unittest,zipfile
from urllib.request import urlopen
from urllib.error import HTTPError
import test_card_statistics as C
from bot.share import ShareApp,ReplayHTTPServer,make_handler
from bot.share_build import route


class ShareBuildTests(unittest.TestCase):
    setUp=C.CardStatisticsTests.setUp
    def app_link(self,deck_extra=False):
        app=ShareApp(self.store,self.replays,lambda:C.NOW)
        link=app.create_link('owner','甲',[self.rows[0]['id']],C.NOW-100,
            deck_replay_ids=[self.rows[1]['id']] if deck_extra else [self.rows[0]['id']])
        return app,link
    def get(self,app,link,suffix=''):
        path='/s/'+link['token']+suffix
        return route(app,path,[p for p in path.split('?')[0].split('/') if p])
    def test_common_build_page_without_private_navigation_or_private_apis(self):
        app,link=self.app_link();page=self.get(app,link)
        self.assertEqual(page[0],200)
        text=page[2].decode();self.assertIn('本次实际构筑',text);self.assertIn('记录到的对局',text)
        self.assertNotIn('<nav',text);self.assertNotIn('/statistics/assets/',text)
        script=self.get(app,link,'/assets/statistics_build.js')[2].decode()
        self.assertNotIn('/statistics/',script)
        self.assertEqual(self.get(app,link,'/api/exclusions')[0],404)
    def test_shared_thumbnail_uses_disk_cache_and_honors_link_expiry(self):
        from pathlib import Path
        from unittest.mock import patch
        folder=self.store.path.parent/'card-thumbs';folder.mkdir(exist_ok=True)
        (folder/'123.jpg').write_bytes(b'jpeg')
        app,link=self.app_link()
        self.assertEqual(self.get(app,link,'/card-thumb/123')[:3],(200,'image/jpeg',b'jpeg'))
        with patch('bot.statistics_local_cards._schedule_thumb') as queue:
            self.assertEqual(self.get(app,link,'/card-thumb/124')[0],302)
            queue.assert_called_once()
        app.clock=lambda:link['expires_at']+1
        self.assertEqual(self.get(app,link,'/card-thumb/123')[0],410)

    def test_scope_cannot_expand_player_digest_or_inferred_side(self):
        app,link=self.app_link()
        result=json.loads(self.get(app,link,'/api/build-detail?player=乙&scope=player')[2])
        self.assertEqual(result['person']['player'],'甲')
        self.assertEqual([r['digest']for r in result['matches']],[self.rows[0]['digest']])
        self.assertEqual(result['all_player_games'],1)
        self.assertEqual(self.get(app,link,'/api/build-detail?digest='+self.rows[1]['digest'])[0],404)
        self.assertEqual(self.get(app,link,'/replay/'+self.rows[1]['digest'])[0],404)
        with zipfile.ZipFile(io.BytesIO(self.get(app,link,'/recording-zip?scope=player')[2]))as z:
            self.assertEqual(len(z.namelist()),1)
    def test_deck_only_permission_never_grants_recording_download(self):
        app,link=self.app_link(True)
        result=json.loads(self.get(app,link,'/api/build-detail?scope=player')[2])
        self.assertEqual(result['selected']['digest'],self.rows[1]['digest'])
        self.assertEqual([r['digest']for r in result['matches']],[self.rows[0]['digest']])
        self.assertEqual(self.get(app,link,'/replay/'+self.rows[1]['digest'])[0],404)
    def test_link_expiry_exclusion_and_changed_source_fail_closed(self):
        app,link=self.app_link();digest=self.rows[0]['digest']
        self.assertEqual(self.get(app,link,'/replay/'+digest)[0],200)
        (self.replays/self.rows[0]['rel_path']).write_bytes(b'changed')
        self.assertEqual(self.get(app,link,'/replay/'+digest)[0],409)
        self.store.set_fact_exclusion(digest,'甲',reason='test')
        self.assertIsNone(json.loads(self.get(app,link,'/api/build-detail')[2]))
        self.assertEqual(self.get(app,link,'/replay/'+digest)[0],404)
        app.clock=lambda:link['expires_at']
        for suffix in ('','/api/build-detail','/assets/statistics_build.js','/replay/'+digest):
            self.assertEqual(self.get(app,link,suffix)[0],410)
    def test_real_http_page_and_download_headers(self):
        app,link=self.app_link();server=ReplayHTTPServer(('127.0.0.1',0),make_handler(app))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base='http://127.0.0.1:'+str(server.server_port)+'/s/'+link['token']
            with urlopen(base)as r:
                self.assertNotIn(b'<nav',r.read());self.assertEqual(r.headers['Cache-Control'],'no-store')
            folder=self.store.path.parent/'card-thumbs';folder.mkdir(exist_ok=True)
            (folder/'123.jpg').write_bytes(b'jpeg')
            with urlopen(base+'/card-thumb/123')as r:
                self.assertEqual(r.read(),b'jpeg');self.assertEqual(r.headers['Cache-Control'],'private, max-age=86400')
            with urlopen(base+'/replay/'+self.rows[0]['digest'])as r:
                self.assertIn('attachment',r.headers['Content-Disposition']);self.assertIn("UTF-8''",r.headers['Content-Disposition'])
                self.assertEqual(r.read(),(self.replays/self.rows[0]['rel_path']).read_bytes())
        finally:server.shutdown();server.server_close();thread.join(2)
