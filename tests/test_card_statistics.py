import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from bot.store import Store
from bot.card_statistics import Filters
from test_deck_links import pack_replay
import test_top100 as T

NOW=1800000000.0


class CardStatisticsTests(unittest.TestCase):
    def test_quality_counts_distinguish_exclusion_missing_source_and_filtering(self):
        self.store.set_fact_exclusion(self.rows[0]['digest'],'甲',reason='test')
        (self.replays/self.rows[1]['rel_path']).unlink()
        result=self.query(filters=Filters(first_player=True))
        quality=result['data_quality']
        self.assertEqual(quality['current_unexpired_views'],5)
        self.assertEqual(quality['excluded_current_views'],1)
        self.assertEqual(quality['missing_source_views'],1)
        self.assertEqual(quality['unexpired_outside_roster_views'],5)
        self.assertEqual(result['candidate_samples'],3)
        self.assertEqual(result['candidate_samples'],result['metrics']['player_samples']+sum(quality['filter_exclusions'].values()))

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.replays=self.root/'replays';self.replays.mkdir()
        self.store=Store(self.root/'db.sqlite3');self.addCleanup(self.store.close)
        self.store.replace_top100(T.roster(['甲']),NOW)
        self.rows=[]
        # 赢家0/1/平局/未知，最后一局只有部分过程但有正向抽到与使用证据。
        for i,winner in enumerate((0,1,2,None,0),1):
            main=[2,2] if i==4 else [1,1,2]
            opponent=[5,6] if i==3 else [3,4]
            blob=bytearray(pack_replay(['甲','乙'],[(main,[9]),(opponent,[8])]))
            struct.pack_into('<I',blob,12,i)
            digest=hashlib.sha256(blob).hexdigest();path=self.replays/f'{i}.yrp';path.write_bytes(blob)
            row,_=self.store.add_replay(digest,'甲',path.name,['甲','乙'],{},NOW-100+i,saved_at=NOW-100)
            complete=i in (1,2,3)
            used=[dict(code=1,events=5)] if i in (1,3,5) else []
            drawn=[dict(code=1,draw_count=2,initial_count=1,used_after_draw=i!=2)] if i in (1,2,5) else []
            report=dict(version=4,digest=digest,winner=winner,result_source='live_server' if winner is not None else None,
                playback='completed' if complete else 'failed',first_player=0 if i in (1,3,5) else 1 if i==2 else None,
                players=[dict(name='甲',main=main,extra=[9],used_cards=used,drawn_cards=drawn,
                              draw_record_complete=complete,initial_draws_accounted=complete,
                              extra_appearances=[dict(code=9,appearance_count=1 if i in (1,3) else 0)] if complete else []),
                         dict(name='乙',main=opponent,extra=[8],used_cards=[],drawn_cards=[],extra_appearances=[],
                              draw_record_complete=complete,initial_draws_accounted=complete)])
            self.store.import_replay_facts(row,path,report,NOW)
            detail=self.store.fact_details(digest,self.replays,NOW)
            self.store.classify_build(detail['views'][0]['build'],'甲类型' if i!=4 else None,['X','Y'] if i!=4 else [],'manual')
            self.store.classify_build(detail['views'][1]['build'],'对手C' if i==3 else '对手B',['Z'],'manual')
            self.rows.append(row)

    def query(self,code=1,partition='main',filters=None,now=NOW,**options):
        return self.store.card_statistics(self.replays,code,partition,now,filters,**options)

    def test_independent_denominators_and_partial_positive_wins(self):
        result=self.query();m=result['metrics']
        self.assertEqual((m['carrying']['numerator'],m['carrying']['denominator']),(4,5))
        self.assertEqual((m['draw_rate']['numerator'],m['draw_rate']['denominator']),(2,3))
        self.assertEqual(m['draw_rate']['unknown_samples'],1)
        self.assertEqual((m['use_rate']['numerator'],m['use_rate']['denominator']),(2,3))
        self.assertEqual((m['drawn_win_rate']['wins'],m['drawn_win_rate']['denominator']),(2,3))
        self.assertEqual((m['used_win_rate']['wins'],m['used_win_rate']['draws'],m['used_win_rate']['denominator']),(2,1,3))
        self.assertEqual((m['use_after_draw_rate']['numerator'],m['use_after_draw_rate']['denominator']),(1,2))
        self.assertEqual(m['use_after_draw_rate']['unknown_samples'],1)
        self.assertEqual(result['unknown_deck_samples'],1)
        self.assertEqual(m['player_samples'],5)

    def test_overall_is_weighted_not_average_of_first_and_second_percentages(self):
        result=self.query()
        self.assertAlmostEqual(result['first']['scope_win_rate']['percentage'],200/3)
        self.assertEqual(result['second']['scope_win_rate']['percentage'],0)
        self.assertEqual(result['metrics']['scope_win_rate']['percentage'],50)
        self.assertEqual(result['unknown_first_samples'],1)

    def test_extra_is_separate_and_uses_successful_appearances_only(self):
        result=self.query(9,'extra');m=result['metrics']
        self.assertNotIn('draw_rate',m)
        self.assertNotIn('use_rate',m)
        self.assertEqual((m['appearance_rate']['numerator'],m['appearance_rate']['denominator']),(2,3))
        self.assertEqual((m['appeared_win_rate']['wins'],m['appeared_win_rate']['draws'],m['appeared_win_rate']['denominator']),(1,1,2))

    def test_owner_opponent_multi_series_and_order_combine_without_duplicates(self):
        filtered=self.query(filters=Filters(deck_types=('甲类型',),opponent_types=('对手B',),series=('X','Y'),opponent_series=('Z',)))
        self.assertEqual(filtered['metrics']['player_samples'],3)
        self.assertEqual(filtered['metrics']['used_win_rate']['percentage'],100)
        first=self.query(filters=Filters(first_player=True))
        self.assertEqual(first['metrics']['player_samples'],3)
        self.assertEqual(sum(g['metrics']['player_samples'] for g in self.query()['by_deck']),5)
        self.assertEqual(self.query(filters=Filters(start_at=NOW-98,end_at=NOW-96))['metrics']['player_samples'],2)

    def test_current_top100_internal_match_requires_both_and_reentry_restores(self):
        self.assertEqual(self.query(filters=Filters(internal_only=True))['metrics']['player_samples'],0)
        self.store.replace_top100(T.roster(['甲','乙']),NOW+1)
        result=self.query(filters=Filters(internal_only=True))
        self.assertEqual((result['metrics']['player_samples'],result['metrics']['small_games']),(10,5))
        self.store.replace_top100(T.roster(['甲']),NOW+2)
        self.assertEqual(self.query()['metrics']['player_samples'],5)

    def test_exclusion_restore_expiry_and_classification_recompute(self):
        self.store.set_fact_exclusion(self.rows[1]['digest'],reason='排除验收')
        self.assertEqual(self.query()['metrics']['player_samples'],4)
        self.assertAlmostEqual(self.query()['metrics']['scope_win_rate']['percentage'],200/3)
        self.store.set_fact_exclusion(self.rows[1]['digest'])
        self.assertEqual(self.query()['metrics']['player_samples'],5)
        before=self.query(filters=Filters(deck_types=('甲类型',)))
        self.assertEqual(before['metrics']['player_samples'],4)
        build=self.store.fact_details(self.rows[0]['digest'],self.replays,NOW)['views'][0]['build']
        self.store.classify_build(build,'新名称',['X'],'manual2')
        self.assertEqual(self.query(filters=Filters(deck_types=('甲类型',)))['metrics']['player_samples'],0)
        self.assertEqual(self.query(filters=Filters(deck_types=('新名称',)))['metrics']['player_samples'],4)
        from retention_policy import RETENTION_SECONDS
        self.assertEqual(self.query(now=NOW+RETENTION_SECONDS)['metrics']['player_samples'],0)

    def test_unknown_side_and_no_samples_are_none_not_zero_percent(self):
        result=self.query(1,'side')
        self.assertIsNone(result['metrics']['carrying']['percentage'])
        self.assertEqual(result['metrics']['carrying']['unknown_samples'],5)
        self.assertIsNone(self.query(filters=Filters(deck_types=('不存在',)))['metrics']['used_win_rate']['percentage'])
        self.assertEqual(self.query(999)['metrics']['carrying']['percentage'],0)

    def test_used_after_matchups_require_both_top100_and_known_deck(self):
        options=dict(matchups_enabled=True,matchup_min_games=1,matchup_fallback_games=1)
        before=self.query(**options)['matchups']
        # 对手乙还不在榜：不计入“双方前100”的总体与分卡组。
        self.assertEqual(before['base']['games'],0)
        self.assertEqual(before['rows'],[])
        self.store.replace_top100(T.roster(['甲','乙']),NOW+1)
        result=self.query(**options)['matchups']
        rows=result['rows']
        self.assertEqual([row['deck_type'] for row in rows],['对手B','对手C'])
        self.assertEqual((rows[0]['games'],rows[0]['wins'],rows[0]['losses'],rows[0]['draws']),(2,2,0,0))
        self.assertEqual(rows[0]['rate'],100)
        self.assertFalse(rows[0]['low_sample'])
        self.assertEqual((rows[1]['games'],rows[1]['wins'],rows[1]['draws']),(1,0,1))
        self.assertEqual(rows[1]['rate'],0)
        self.assertEqual(result['base']['games'],3)
        self.assertEqual(result['base']['rate'],200/3)

    def test_matchups_fall_back_to_low_sample_rows(self):
        self.store.replace_top100(T.roster(['甲','乙']),NOW+1)
        result=self.query(matchups_enabled=True,matchup_min_games=10,
                          matchup_fallback_games=1)['matchups']
        self.assertEqual(result['qualified_decks'],0)
        self.assertEqual([row['deck_type'] for row in result['rows']],['对手B','对手C'])
        self.assertTrue(all(row['low_sample'] for row in result['rows']))
        self.assertIn('样本少',result['notes'][0])

    def test_missing_event_fact_is_unknown_and_does_not_erase_known_carrying(self):
        self.store._db.execute('DELETE FROM stat_card_facts WHERE digest=? AND player=? AND code=?',(self.rows[0]['digest'],'甲',1))
        self.store._db.commit()
        result=self.query()['metrics']
        self.assertEqual((result['carrying']['numerator'],result['carrying']['denominator']),(4,5))
        self.assertEqual(result['use_rate']['unknown_samples'],2)
        self.assertEqual(result['draw_rate']['unknown_samples'],2)
