import json,unittest
from urllib.parse import quote
import test_card_statistics as C
import test_top100 as T
from bot.statistics_groups import query,source_file
from bot.statistics_dashboard import query as dashboard
from bot.statistics_portal import StatisticsPortal


class StatisticsGroupsTests(unittest.TestCase):
    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)

    def query(self):return query(self.store._db,self.replays,C.NOW)

    def test_people_and_small_game_counts_have_distinct_units(self):
        result=self.query()
        self.assertEqual(sum(g['players'] for g in result['deck_groups']),2)
        self.assertEqual(sum(g['small_game_perspectives'] for g in result['deck_groups']),10)
        x=next(g for g in result['series_groups'] if g['name']=='X')
        y=next(g for g in result['series_groups'] if g['name']=='Y')
        self.assertEqual(x['players'],y['players'])
        self.assertEqual((x['players'],x['highest_rank']),(1,1))
        self.assertEqual(sum(x['outcomes'].values()),x['small_game_perspectives'])
        self.assertEqual(x['unique_small_games'],x['small_game_perspectives'])
        self.assertIn(None,[g['name'] for g in result['deck_groups']])

    def test_same_group_internal_game_deduplicated_as_game_but_not_perspective(self):
        for build in self.store._db.execute('SELECT fingerprint FROM stat_builds').fetchall():
            self.store.classify_build(build[0],'共同类型',['共同系列'],'manual')
        result=self.query();group=result['deck_groups'][0]
        self.assertEqual((group['players'],group['small_game_perspectives'],group['unique_small_games']),(2,10,5))
        self.assertEqual({p['player'] for p in group['members']},{'甲','乙'})

    def test_latest_selected_before_grouping_history_not_current(self):
        historical=self.store._db.execute('SELECT build FROM stat_views WHERE digest=? AND player=?',(self.rows[3]['digest'],'甲')).fetchone()[0]
        self.store.classify_build(historical,'历史类型',['旧系列'],'manual')
        latest=dashboard(self.store._db,self.replays,C.NOW,'甲')
        fingerprint=self.store._db.execute('SELECT build FROM stat_views WHERE digest=? AND player=?',(latest['source']['digest'],'甲')).fetchone()[0]
        self.store.classify_build(fingerprint,'最新专用',['新系列'],'manual')
        result=self.query();old=next(g for g in result['deck_groups'] if g['name']=='历史类型')
        self.assertEqual(old['players'],0);self.assertIsNone(old['highest_rank']);self.assertEqual(old['members'],[])
        new=next(g for g in result['deck_groups'] if g['name']=='最新专用')
        self.assertEqual(new['players'],1)

    def test_source_only_current_latest_and_original_ttl_exclusion_membership(self):
        detail=dashboard(self.store._db,self.replays,C.NOW,'甲');digest=detail['source']['digest']
        self.assertIsNotNone(source_file(self.store._db,self.replays,C.NOW,'甲',digest))
        self.assertIsNone(source_file(self.store._db,self.replays,C.NOW,'甲',self.rows[0]['digest']))
        self.assertIsNone(source_file(self.store._db,self.replays,C.NOW,'甲','../secret'))
        self.assertIsNone(source_file(self.store._db,self.replays,C.NOW+14*86400,'甲',digest))
        self.store.set_fact_exclusion(digest,'甲',reason='test')
        self.assertIsNone(source_file(self.store._db,self.replays,C.NOW,'甲',digest))
        self.store.set_fact_exclusion(digest,'甲',reason=None)
        self.store.replace_top100(T.roster(['乙']),C.NOW)
        self.assertIsNone(source_file(self.store._db,self.replays,C.NOW,'甲',digest))

    def test_stale_roster_and_missing_sources_do_not_fabricate_population(self):
        self.store.top100_failed(C.NOW,'timeout');before=self.query()
        self.assertTrue(before['roster']['stale']);self.assertEqual(before['covered_players'],2)
        for file in self.replays.iterdir():file.unlink()
        result=self.query();self.assertEqual(result['covered_players'],0)
        self.assertEqual(result['deck_groups'],[]);self.assertEqual(result['missing_players'],100)

    def test_private_api_download_rechecks_and_share_cookie_cannot_access(self):
        portal=StatisticsPortal(self.store.path,self.replays,lambda:C.NOW,'x'*43)
        headers={'Host':'127.0.0.1'}
        cookie=portal.route('/statistics/access/'+'x'*43,headers,'127.0.0.1')[3]['Set-Cookie'].split(';',1)[0]
        headers['Cookie']=cookie
        result=portal.route('/statistics/api/groups',headers,'127.0.0.1')
        self.assertEqual(result[0],200);self.assertEqual(json.loads(result[2])['covered_players'],2)
        detail=dashboard(self.store._db,self.replays,C.NOW,'甲');digest=detail['source']['digest']
        path='/statistics/source/'+digest+'?player='+quote('甲')
        response=portal.route(path,headers,'127.0.0.1')
        self.assertEqual(response[0],200);self.assertEqual(response[1],'application/octet-stream')
        self.assertIn('attachment;',response[3]['Content-Disposition'])
        self.assertEqual(response[2],source_file(self.store._db,self.replays,C.NOW,'甲',digest).read_bytes())
        self.assertEqual(portal.route(path,{'Host':'127.0.0.1','Cookie':'token='+'x'*43},'127.0.0.1')[0],403)
        self.store.set_fact_exclusion(digest,'甲',reason='test')
        self.assertEqual(portal.route(path,headers,'127.0.0.1')[0],404)
