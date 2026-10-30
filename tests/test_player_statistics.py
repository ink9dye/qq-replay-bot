import test_card_statistics as C
import unittest,json
NOW=C.NOW


class PlayerStatisticsTests(unittest.TestCase):
    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        import test_top100 as T
        self.store.replace_top100(T.roster(['甲','乙']),NOW)
    def test_latest_is_selected_before_deck_filter_and_one_player_counts_once(self):
        result=self.store.player_card_statistics(self.replays,1,NOW)
        self.assertEqual(result['covered_players'],2)
        self.assertEqual(result['current_players'],100)
        self.assertEqual(result['missing_players'],98)
        self.assertEqual(len(result['people']),2)
        latest=max(self.store.current_top100_views(self.replays,NOW)['views'],key=lambda r:(r['recorded_at'],r['digest']))
        self.store.classify_build(latest['build'],'最新类型',[],'manual')
        older=self.store.player_card_statistics(self.replays,1,NOW,deck_types=['甲类型'])
        self.assertNotIn(latest['player'],[p['player'] for p in older['people']])

    def test_unknown_side_makes_total_unknown_without_fabricated_zero(self):
        result=self.store.player_card_statistics(self.replays,1,NOW)
        self.assertEqual(result['distributions']['side']['known_players'],0)
        self.assertEqual(result['distributions']['total']['unknown_players'],2)
        self.assertIsNone(result['distributions']['total']['bins'][0]['percentage'])
        for partition in ('main','extra'):
            d=result['observed_distributions'][partition]
            self.assertEqual(sum(b['players'] for b in d['bins']),d['known_players'])

    def test_initial_distribution_requires_confirmed_first_game_in_same_match(self):
        result=self.store.player_card_statistics(self.replays,1,NOW)
        self.assertEqual(result['distributions']['main']['known_players'],0)
        for i in (3,4,5):
            payload=dict(capture=dict(session_id='match-two',game_no=i-2,captured_from_start=True))
            self.store._db.execute('INSERT INTO stat_evidence VALUES(?,?,?,?)',
                (self.rows[i-1]['digest'],'fixture','live_server',json.dumps(payload)))
        self.store._db.commit()
        result=self.store.player_card_statistics(self.replays,1,NOW)
        self.assertEqual(result['distributions']['main']['known_players'],2)
        self.assertTrue(all(p['initial_source_digest']==self.rows[2]['digest'] for p in result['people']))
        self.assertTrue(all(p['observation_kind']=='post_side' for p in result['people']))

    def test_total_sums_cards_per_person_before_grouping(self):
        build=self.store.current_top100_views(self.replays,NOW)['views'][-1]['build']
        self.store._db.execute('UPDATE stat_builds SET side_complete=1 WHERE fingerprint=?',(build,))
        self.store._db.execute('INSERT INTO stat_build_cards VALUES(?,?,?,?)',(build,'side',1,2))
        self.store._db.commit()
        result=self.store.player_card_statistics(self.replays,1,NOW)
        person=next(p for p in result['people'] if p['build']==build)
        self.assertEqual(person['copies']['total'],person['copies']['main']+2)
        dist=result['observed_distributions']['total']
        self.assertEqual(sum(b['players'] for b in dist['bins']),dist['known_players'])

    def test_ttl_and_dropout_remove_people_without_renewal(self):
        import test_top100 as T
        self.store.replace_top100(T.roster(['甲']),NOW+1)
        result=self.store.player_card_statistics(self.replays,1,NOW+1)
        self.assertEqual(result['covered_players'],1)
        from retention_policy import RETENTION_SECONDS
        self.assertEqual(self.store.player_card_statistics(self.replays,1,NOW+RETENTION_SECONDS)['covered_players'],0)
