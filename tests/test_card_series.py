import json,unittest
import test_card_statistics as C
from bot.card_series import setcodes,matches_setcode
from bot.card_statistics import Filters


class CardSeriesTests(unittest.TestCase):
    def test_invalid_catalog_keeps_unknown_instead_of_empty_known(self):
        (self.root/'card-series-reference.json').write_text('[]',encoding='utf-8')
        result=self.store.card_statistics(self.replays,1,'main',C.NOW)
        self.assertEqual(result['series_reference']['status'],'invalid_reference')
        self.assertEqual(result['data_quality']['selected_unknown_series'],5)

    def setUp(self):
        C.CardStatisticsTests.setUp(self)
        self.store._db.execute("UPDATE stat_builds SET classification_source='reference',series_status='unknown'")
        self.store._db.execute('DELETE FROM stat_build_series');self.store._db.commit()
        self.catalog={str(code):dict(tags=tags,complete=True) for code,tags in {
            1:['系列甲'],2:[],3:['系列乙'],4:[],5:['系列丙'],6:[],8:[],9:['额外系列']}.items()}
        self.save()

    def save(self):
        from bot.card_series import SEMANTICS
        (self.root/'card-series-reference.json').write_text(json.dumps(dict(version=1,semantics=SEMANTICS,cards=self.catalog)),encoding='utf-8')

    def test_parent_subfamily_and_packed_codes_follow_core_matching(self):
        self.assertEqual(setcodes((0x3008<<16)|0x2),[2,0x3008])
        self.assertTrue(matches_setcode(0x3008,0x8))
        self.assertTrue(matches_setcode(0x3008,0x3008))
        self.assertFalse(matches_setcode(0x6008,0x3008))
        self.assertFalse(matches_setcode(0,0))

    def test_actual_owner_and_opponent_cards_filter_without_name_splitting_or_duplicates(self):
        all_rows=self.store.current_top100_views(self.replays,C.NOW)['views']
        self.assertEqual(len(all_rows),5)
        self.assertTrue(all(r['series_status']=='known' for r in all_rows))
        result=self.store.card_statistics(self.replays,1,'main',C.NOW,Filters(series=('系列甲',),opponent_series=('系列丙',)))
        self.assertEqual(result['metrics']['player_samples'],1)
        self.assertEqual(result['metrics']['small_games'],1)
        multi=self.store.card_statistics(self.replays,1,'main',C.NOW,Filters(series=('系列甲','额外系列')))
        self.assertEqual(multi['metrics']['player_samples'],5)
        self.assertEqual(multi['series_reference']['status'],'ready')

    def test_missing_card_keeps_partial_coverage_and_never_fabricates_absence(self):
        del self.catalog['2'];self.save()
        result=self.store.card_statistics(self.replays,1,'main',C.NOW)
        self.assertEqual(result['data_quality']['selected_unknown_series'],5)
        rows=self.store.current_top100_views(self.replays,C.NOW)['views']
        self.assertTrue(all(r['series_status']=='partial' for r in rows))
        self.assertTrue(all('额外系列' in r['series'] for r in rows))

    def test_known_empty_requires_complete_card_metadata_and_manual_labels_win(self):
        for card in self.catalog.values():card['tags']=[]
        self.save()
        rows=self.store.current_top100_views(self.replays,C.NOW)['views']
        self.assertTrue(all(r['series']==[] and r['series_status']=='known' for r in rows))
        build=rows[0]['build'];self.store.classify_build(build,'人工类型',['手工系列'],'manual')
        self.catalog['1']['tags']=['自动系列'];self.save()
        rows=self.store.current_top100_views(self.replays,C.NOW)['views']
        self.assertTrue(all(r['series']==['手工系列'] for r in rows if r['build']==build))

    def test_reference_change_updates_queries_and_latest_people_without_changing_expiry(self):
        before=tuple(self.store._db.execute('SELECT COUNT(*),SUM(expires_at) FROM stat_games').fetchone())
        one=self.store.player_card_statistics(self.replays,1,C.NOW)
        self.assertEqual(len(one['people']),1)
        self.assertEqual({r['series'] for r in one['series_distribution']},{'系列甲','额外系列'})
        self.assertEqual(sum(r['players'] for r in one['series_distribution']),2)
        self.catalog['1']['tags']=['新系列'];self.save()
        two=self.store.player_card_statistics(self.replays,1,C.NOW)
        self.assertNotEqual(one['series_reference']['revision'],two['series_reference']['revision'])
        self.assertIn('新系列',two['people'][0]['series'])
        self.assertEqual(tuple(self.store._db.execute('SELECT COUNT(*),SUM(expires_at) FROM stat_games').fetchone()),before)
