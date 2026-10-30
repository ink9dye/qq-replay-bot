import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from urllib.parse import urlencode
import test_card_statistics as C
import test_top100 as T
from bot.statistics_card_names import index, search
from bot.statistics_local_cards import picture
from bot.statistics_investment import query as investment, summary
from bot.statistics_build_detail import query as detail, ydk
from bot.statistics_portal import StatisticsPortal


class LocalLibraryTests(unittest.TestCase):
    def test_match_labels_stable_per_player_without_changing_game_facts(self):
        from bot.statistics_build_detail import match_deck_names
        games = [dict(player='甲', opponent='乙', deck_type='奥艺耀圣', opponent_deck_type='闪刀'),
                 dict(player='甲', opponent='乙', deck_type=None, opponent_deck_type=None),
                 dict(player='甲', opponent='乙', deck_type='换边分类', opponent_deck_type=None)]
        match_deck_names(games)
        self.assertEqual([g['match_deck_type'] for g in games], ['奥艺耀圣'] * 3)
        self.assertEqual([g['opponent_match_deck_type'] for g in games], ['闪刀'] * 3)
        self.assertIsNone(games[1]['deck_type'])
        self.assertEqual(games[2]['deck_type'], '换边分类')
        unknown = [dict(player='甲', opponent='乙', deck_type=None, opponent_deck_type=None)]
        match_deck_names(unknown)
        self.assertIsNone(unknown[0]['match_deck_type'])

    def test_local_name_priority_and_password_not_cid_and_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);library=root/'ygopro';library.mkdir();(root/'cache').mkdir()
            (root/'card-library.json').write_text(json.dumps(dict(ygopro_root=str(library))))
            (root/'cache/cards.json').write_text(json.dumps({'35269905':{'id':89631139,'cn_name':'公开青眼'},'7':{'id':35269905,'cn_name':'旧名'}}))
            with closing(sqlite3.connect(library/'cards.cdb')) as db:
                db.execute('CREATE TABLE texts (id INTEGER PRIMARY KEY,name TEXT)');db.execute('INSERT INTO texts VALUES (?,?)',(35269905,'三战之号'))
                db.commit()
            self.assertEqual(search(root,'35269905')[0]['name'],'三战之号')
            self.assertEqual(index(root)['89631139']['cn_name'],'公开青眼')
            with closing(sqlite3.connect(library/'cards.cdb')) as db:
                db.execute('UPDATE texts SET name=?',('三战之号更新',));db.commit()
            self.assertEqual(search(root,'35269905')[0]['name'],'三战之号更新')
            pics=library/'pics';pics.mkdir();(pics/'35269905.jpg').write_bytes(b'image fixture')
            self.assertEqual(picture(root,35269905)[1],'image/jpeg')
            self.assertIsNone(picture(root,'../secret'));self.assertIsNone(picture(root,0))


class PopulationAndDetailTests(unittest.TestCase):
    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)
        self.portal=StatisticsPortal(self.store.path,self.replays,lambda:C.NOW,'x'*43)
        self.headers={'Host':'127.0.0.1:8788'}
        self.headers['Cookie']=self.portal.route('/statistics/access/'+'x'*43,self.headers,'127.0.0.1')[3]['Set-Cookie'].split(';',1)[0]

    def get(self,path):return self.portal.route(path,self.headers,'127.0.0.1')

    def evidence(self):
        for number,index_ in ((1,0),(2,3)):
            self.store._db.execute('INSERT INTO stat_evidence VALUES(?,?,?,?)',(self.rows[index_]['digest'],'side-fixture','live_server',json.dumps(dict(capture=dict(session_id='verified-session',game_no=number,captured_from_start=True)))))
        self.store._db.commit()

    def test_investment_counts_one_person_not_five_replays_and_unknown_population(self):
        r=investment(self.store._db,self.replays,1,C.NOW);p=r['partitions']['main']
        self.assertEqual((p['confirmed_players'],p['confirmed_absent_players'],p['unknown_players'],p['population']),(1,1,0,2))
        self.assertEqual(p['percentage'],50);self.assertFalse(p['lower_bound'])
        self.assertEqual(r['partitions']['side']['unknown_players'],2)
        self.assertEqual(r['partitions']['total']['confirmed_players'],1)
        self.assertEqual(r['partitions']['total']['known_copy_players'],0)
        self.assertEqual(sum(g['players'] for g in r['by_deck']),2)

    def test_initial_never_falls_back_and_latest_selected_before_filters(self):
        r=investment(self.store._db,self.replays,1,C.NOW,basis='initial')
        self.assertEqual(r['partitions']['main']['unknown_players'],2)
        latest=max((r for r in self.store.current_top100_views(self.replays,C.NOW)['views'] if r['player']=='甲'),key=lambda r:(r['recorded_at'],r['digest']))
        self.store.classify_build(latest['build'],'changed',[],'manual')
        r=investment(self.store._db,self.replays,1,C.NOW,deck_types=('甲类型',))
        self.assertEqual(r['population'],0)

    def test_partial_positive_is_known_presence_but_not_exact_copy_count(self):
        r=summary([(2,False),(0,False),(0,True)],4)
        self.assertEqual((r['confirmed_players'],r['confirmed_absent_players'],r['unknown_players']),(1,1,2))
        self.assertEqual((r['known_copy_players'],r['bins']['2']),(1,0))

    def test_side_inference_read_only_traced_and_not_a_complete_partition(self):
        self.evidence();before=self.store._db.execute('SELECT SUM(expires_at) FROM stat_games').fetchone()[0]
        r=detail(self.store._db,self.replays,C.NOW,'甲',self.rows[0]['digest'])
        self.assertEqual(r['inferred_side']['cards'][0]['code'],2)
        self.assertEqual(r['inferred_side']['cards'][0]['copies'],1)
        self.assertEqual(r['inferred_side']['sources'][0]['kind'],'same_match')
        self.assertFalse(r['completeness']['side']);self.assertFalse(r['inferred_side']['complete'])
        self.assertEqual(before,self.store._db.execute('SELECT SUM(expires_at) FROM stat_games').fetchone()[0])
        self.assertNotIn('inferred Side',ydk(r,False));self.assertIn('inferred Side',ydk(r,True))

    def test_side_evidence_excluded_or_expired_never_contributes(self):
        self.evidence();self.store.set_fact_exclusion(self.rows[3]['digest'],'甲',reason='test')
        r=detail(self.store._db,self.replays,C.NOW,'甲',self.rows[0]['digest'])
        self.assertEqual(r['inferred_side']['cards'],[])
        self.assertIsNone(detail(self.store._db,self.replays,C.NOW+14*86400,'甲'))

    def test_existing_archived_sessions_restore_inference_without_time_guessing(self):
        for number,index_ in ((1,0),(2,3)):
            self.store._db.execute('INSERT INTO replay_sessions VALUES(?,?,?)',(self.rows[index_]['id'],'legacy:verified',number))
        self.store._db.commit()
        result=detail(self.store._db,self.replays,C.NOW,'甲',self.rows[0]['digest'])
        self.assertEqual(result['inferred_side']['cards'][0]['code'],2)
        self.assertEqual(result['inferred_side']['sources'][0]['kind'],'same_match')

    def test_conflicting_explicit_capture_cannot_be_overridden_by_legacy_mapping(self):
        self.evidence()
        self.store._db.execute('INSERT INTO stat_evidence VALUES(?,?,?,?)',(self.rows[0]['digest'],'conflict','live_server',json.dumps(dict(capture=dict(session_id='different-match',game_no=1,captured_from_start=True)))))
        for number,index_ in ((1,0),(2,3)):
            self.store._db.execute('INSERT INTO replay_sessions VALUES(?,?,?)',(self.rows[index_]['id'],'legacy:verified',number))
        self.store._db.commit()
        result=detail(self.store._db,self.replays,C.NOW,'甲',self.rows[0]['digest'])
        self.assertEqual(result['inferred_side']['cards'],[])

    def test_inferred_export_rejects_more_than_fifteen_without_truncation(self):
        result=detail(self.store._db,self.replays,C.NOW,'甲')
        result['inferred_side'].update(conflict=True,cards=[dict(code=2,copies=16)])
        with self.assertRaises(ValueError):ydk(result,True)
        self.assertNotIn('inferred Side',ydk(result,False))

    def test_detail_history_uses_actual_decks_and_current_ranks_are_labeled(self):
        r=detail(self.store._db,self.replays,C.NOW,'甲',scope='player')
        self.assertEqual(len(r['matches']),5)
        self.assertIsNone(r['matches'][0]['historical_rank'])
        self.assertTrue(any(m['deck_type'] is None for m in r['matches']))
        self.assertTrue(any(m['opponent_deck_type']=='对手C' for m in r['matches']))
        self.store.replace_top100(T.roster(['甲']),C.NOW)
        r=detail(self.store._db,self.replays,C.NOW,'甲',scope='player')
        self.assertTrue(all(m['opponent_current_rank'] is None for m in r['matches']))

    def test_new_routes_require_private_session_and_validate_parameters(self):
        for path in ('/statistics/api/build-detail?'+urlencode(dict(player='甲')),'/statistics/card-image/1','/statistics/build-ydk?'+urlencode(dict(player='甲'))):
            self.assertEqual(self.portal.route(path,{'Host':'localhost'},'127.0.0.1')[0],403)
        self.assertEqual(self.get('/statistics/card-image/../config.json')[0],404)
        self.assertEqual(self.get('/statistics/api/player-investment?code=0')[0],400)
        self.assertEqual(self.get('/statistics/api/player-investment?code=1&deck=%5B%5D')[0],400)
        self.assertEqual(self.get('/statistics/api/build-detail?'+urlencode(dict(player='甲',digest='bad')))[0],400)
        self.assertEqual(self.get('/statistics/build-ydk?'+urlencode(dict(player='甲')))[0],200)
        self.assertEqual(self.get('/statistics/api/player-investment?code=1')[0],200)

    def test_single_card_response_has_one_snapshot_and_game_filters_do_not_reweight_people(self):
        all_result=json.loads(self.get('/statistics/api/card-metrics?code=1')[2])
        first=json.loads(self.get('/statistics/api/card-metrics?code=1&first=first')[2])
        self.assertEqual(all_result['investment']['partitions'],first['investment']['partitions'])
        self.assertNotEqual(all_result['metrics']['player_samples'],first['metrics']['player_samples'])
        self.assertEqual(all_result['roster']['version'],all_result['investment']['roster']['version'])
        self.assertEqual(all_result['generated_at'],all_result['investment']['generated_at'])
        initial=json.loads(self.get('/statistics/api/card-metrics?code=1&basis=initial')[2])
        self.assertEqual(initial['investment']['selected_sources'],0)

    def test_build_scope_expands_related_session_and_adjacent_swaps(self):
        for number,index_ in ((1,0),(2,3),(3,4)):
            self.store._db.execute('INSERT INTO stat_evidence VALUES(?,?,?,?)',(self.rows[index_]['digest'],'match-three','live_server',json.dumps(dict(capture=dict(session_id='match-three',game_no=number,captured_from_start=True)))))
        self.store._db.commit()
        result=detail(self.store._db,self.replays,C.NOW,'甲',self.rows[4]['digest'])
        group=next(g for g in result['match_groups'] if any(m['digest']==self.rows[4]['digest'] for m in g['games']))
        self.assertEqual([m['game_no'] for m in group['games']],[1,2,3])
        self.assertFalse(group['games'][1]['matches_selected_build'])
        second,third=group['games'][1:]
        self.assertEqual(second['changes']['added'],[dict(partition='main',code=2,copies=1)])
        self.assertEqual(second['changes']['removed'],[dict(partition='main',code=1,copies=2)])
        self.assertEqual(third['changes']['added'],[dict(partition='main',code=1,copies=2)])
        self.assertEqual(third['changes']['removed'],[dict(partition='main',code=2,copies=1)])
        self.assertEqual(group['missing_game_numbers'],[])

    def test_missing_previous_game_stays_unknown_instead_of_comparing_game_one(self):
        for number,index_ in ((1,0),(3,4)):
            self.store._db.execute('INSERT INTO stat_evidence VALUES(?,?,?,?)',(self.rows[index_]['digest'],'gap','live_server',json.dumps(dict(capture=dict(session_id='gap',game_no=number,captured_from_start=True)))))
        self.store._db.commit();result=detail(self.store._db,self.replays,C.NOW,'甲')
        group=next(g for g in result['match_groups'] if any(m['digest']==self.rows[4]['digest'] for m in g['games']))
        self.assertEqual(group['missing_game_numbers'],[2])
        self.assertEqual(group['games'][-1]['changes']['status'],'missing_previous_game')
        self.assertEqual(group['games'][-1]['changes']['added'],[])
