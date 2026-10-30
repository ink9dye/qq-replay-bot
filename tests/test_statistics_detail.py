import json,unittest
from urllib.parse import urlencode
import test_card_statistics as C
import test_top100 as T
from bot.statistics_portal import StatisticsPortal
from bot.statistics_matrix import query as matrix
from bot.card_statistics import Filters


class StatisticsDetailTests(unittest.TestCase):
    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)
        self.portal=StatisticsPortal(self.store.path,self.replays,lambda:C.NOW,'x'*43)
        self.headers={'Host':'127.0.0.1:8788'}
        self.headers['Cookie']=self.portal.route('/statistics/access/'+'x'*43,self.headers,'127.0.0.1')[3]['Set-Cookie'].split(';',1)[0]

    def get(self,path):return self.portal.route(path,self.headers,'127.0.0.1')
    def post(self,row,excluded,headers=None):
        h=dict(self.headers,Origin='http://127.0.0.1:8788',**{'Content-Type':'application/json'})
        h.update(headers or {})
        return self.portal.post('/statistics/api/exclusion',h,'127.0.0.1',json.dumps(dict(player='甲',digest=row['digest'],excluded=excluded)).encode())

    def maintain_exclusion(self,row,excluded):
        from bot.statistics_samples import set_exclusion
        with self.store._db:
            set_exclusion(self.store._db,C.NOW,'甲',row['digest'],excluded)

    def test_card_api_matches_verified_core_including_order_and_opponent(self):
        args=urlencode(dict(code=1,partition='main',first='first',opponent=json.dumps('对手B')))
        response=self.get('/statistics/api/card-metrics?'+args);self.assertEqual(response[0],200)
        result=json.loads(response[2]);expected=self.store.card_statistics(self.replays,1,'main',C.NOW,Filters(opponent_types=('对手B',),first_player=True))
        expected=json.loads(json.dumps(expected))
        for key in ('metrics','first','second','data_quality','filters','card_identity'):self.assertEqual(result[key],expected[key])
        self.assertTrue(all(s['first_player']==1 for s in result['samples']))
        self.assertTrue(all('recorded_at' in s for s in result['samples']))

    def test_extra_metrics_have_no_draw_use_denominators(self):
        result=json.loads(self.get('/statistics/api/card-metrics?code=9&partition=extra')[2])
        self.assertIn('appearance_rate',result['metrics']);self.assertNotIn('draw_rate',result['metrics']);self.assertNotIn('use_rate',result['metrics'])
        self.assertEqual(self.get('/statistics/api/card-metrics?code=0')[0],400)
        self.assertEqual(self.get('/statistics/api/matrix?first=guessed')[0],400)
        self.assertEqual(self.get('/statistics/api/matrix?row=%5B1%5D')[0],400)

    def test_matrix_direction_and_raw_total_draw_unknown_denominators(self):
        result=matrix(self.store._db,self.replays,C.NOW)
        self.assertEqual((result['perspectives'],result['small_games']),(10,5))
        self.assertEqual((result['total']['wins'],result['total']['losses'],result['total']['draws'],result['total']['unknown_samples']),(3,3,2,2))
        self.assertEqual(result['total']['denominator'],8)
        cell=next(c for c in result['cells'] if c['row']=='甲类型' and c['column']=='对手B')
        self.assertEqual((cell['metrics']['wins'],cell['metrics']['losses'],cell['perspectives']),(2,1,3))
        self.assertTrue(all(s['player']=='甲' for s in cell['samples']))
        self.assertEqual(sum(c['perspectives'] for c in result['cells']),result['perspectives'])

    def test_matrix_unknown_group_selection_and_internal_membership(self):
        result=matrix(self.store._db,self.replays,C.NOW,rows=[None])
        self.assertEqual(result['perspectives'],1)
        self.store.replace_top100(T.roster(['甲']),C.NOW)
        self.assertEqual(matrix(self.store._db,self.replays,C.NOW,internal_only=True)['perspectives'],0)
        self.assertEqual(matrix(self.store._db,self.replays,C.NOW)['perspectives'],5)

    def test_exclusion_restore_recomputes_both_views_without_extending_ttl(self):
        before=tuple(self.store._db.execute('SELECT COUNT(*),SUM(expires_at) FROM stat_games').fetchone())
        self.maintain_exclusion(self.rows[0],True)
        card=json.loads(self.get('/statistics/api/card-metrics?code=1')[2]);grid=json.loads(self.get('/statistics/api/matrix')[2])
        self.assertEqual((card['metrics']['player_samples'],grid['perspectives']),(9,9))
        self.assertEqual(self.get('/statistics/api/exclusions')[0],404)
        self.maintain_exclusion(self.rows[0],True)
        self.maintain_exclusion(self.rows[0],False)
        self.assertEqual(json.loads(self.get('/statistics/api/matrix')[2])['perspectives'],10)
        self.assertEqual(tuple(self.store._db.execute('SELECT COUNT(*),SUM(expires_at) FROM stat_games').fetchone()),before)

    def test_cross_origin_share_cookie_and_non_current_mutation_rejected(self):
        self.assertEqual(self.post(self.rows[0],True,{'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.post(self.rows[0],True,{'Cookie':'token='+'x'*43})[0],403)
        self.assertEqual(self.post(self.rows[0],True,{'X-Forwarded-For':'127.0.0.1'})[0],403)
        self.store.replace_top100(T.roster(['乙']),C.NOW)
        self.assertEqual(self.post(self.rows[0],True)[0],403)
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM stat_exclusions').fetchone()[0],0)

    def test_historical_source_eligible_but_excluded_or_missing_unavailable(self):
        args=urlencode(dict(player='甲',digest=self.rows[0]['digest']));path='/statistics/api/sample?'+args
        response=self.get(path);self.assertEqual(response[0],200)
        detail=json.loads(response[2]);self.assertEqual(detail['source']['digest'],self.rows[0]['digest'])
        download='/statistics/sample/'+self.rows[0]['digest']+'?'+urlencode(dict(player='甲'))
        self.assertEqual(self.get(download)[2],(self.replays/self.rows[0]['rel_path']).read_bytes())
        self.maintain_exclusion(self.rows[0],True);self.assertEqual(self.get(path)[0],404)
        self.maintain_exclusion(self.rows[0],False);(self.replays/self.rows[0]['rel_path']).unlink()
        self.assertEqual(self.get(download)[0],404)


class ReadOnlyViewerTests(unittest.TestCase):
    setUp = StatisticsDetailTests.setUp
    get = StatisticsDetailTests.get
    post = StatisticsDetailTests.post
    maintain_exclusion = StatisticsDetailTests.maintain_exclusion
    def test_authenticated_viewer_cannot_manage_or_restore_existing_exclusions(self):
        self.maintain_exclusion(self.rows[0],True)
        before=list(map(tuple,self.store._db.execute('SELECT * FROM stat_exclusions')))
        games=list(map(tuple,self.store._db.execute('SELECT * FROM stat_games')))
        builds=list(map(tuple,self.store._db.execute('SELECT * FROM stat_builds')))
        h=dict(self.headers,Origin='http://127.0.0.1:8788',**{'Content-Type':'application/json'})
        for path in ('/statistics/api/exclusion','/statistics/api/classification','/statistics/api/cleanup'):
            with self.subTest(path=path):
                self.assertEqual(self.portal.post(path,h,'127.0.0.1',b'{}')[0],403)
        self.assertEqual(self.post(self.rows[0],False)[0],403)
        for path in ('/statistics/review','/statistics/api/review','/statistics/api/exclusions'):
            self.assertEqual(self.get(path)[0],404)
        self.assertEqual(list(map(tuple,self.store._db.execute('SELECT * FROM stat_exclusions'))),before)
        self.assertEqual(list(map(tuple,self.store._db.execute('SELECT * FROM stat_games'))),games)
        self.assertEqual(list(map(tuple,self.store._db.execute('SELECT * FROM stat_builds'))),builds)
        self.assertEqual(self.get('/statistics/sample/'+self.rows[0]['digest']+'?player=%E7%94%B2')[0],404)

    def test_user_pages_and_common_source_popup_have_no_management_actions(self):
        for path in ('/statistics','/statistics/recordings','/statistics/cards','/statistics/matrix'):
            response=self.get(path)
            self.assertEqual(response[0],200)
            for removed in ('id="excluded"','/statistics/review','SS.excluded('):
                self.assertNotIn(removed,response[2].decode())
        for asset in ('statistics_samples.js','statistics_card_metrics.js'):
            text=self.get('/statistics/assets/'+asset)[2].decode()
            for removed in ('api/exclusion',"method:'POST'",'排除此玩家的这局统计','async function excluded'):
                self.assertNotIn(removed,text)
