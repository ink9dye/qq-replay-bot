import unittest
import test_card_statistics as C
import test_top100 as T
from recording_filter_fixture import set_up
from bot.statistics_recording_filters import query, draw_rankings, carried_game_counts


class RecordingFilterTests(unittest.TestCase):
    setUp=set_up
    def test_missing_game_number_remains_excluded(self):
        self.store._db.execute("UPDATE stat_evidence SET payload_json=json_remove(payload_json,'$.capture')")
        self.assertEqual(query(self.store._db,self.replays,C.NOW,{})['total'],0)

    def test_both_sides_cards_intersect_without_expanding_top100_population(self):
        db=self.store._db
        args={'card':['1'],'opponent_card':['3'],'opponent_mode':['not_drawn']}
        result=query(db,self.replays,C.NOW,args)
        self.assertEqual(result['total'],2)
        self.assertTrue(all(r['player']=='甲' for r in result['samples']))
        self.assertEqual(query(db,self.replays,C.NOW,dict(args,opponent_mode=['drawn']))['total'],0)
        self.assertEqual(query(db,self.replays,C.NOW,dict(args,opponent_card=['3','5'],opponent_match=['all']))['total'],0)
        self.assertEqual(query(db,self.replays,C.NOW,{'opponent_card_none':['1']})['total'],0)
        self.store.set_fact_exclusion(self.rows[0]['digest'],'乙',reason='test')
        self.assertEqual(query(db,self.replays,C.NOW,args)['total'],1)
        self.assertRaises(ValueError,query,db,self.replays,C.NOW,{'opponent_card':['bad']})
        self.assertRaises(ValueError,query,db,self.replays,C.NOW,{'opponent_mode':['bad']})

    def test_carried_sample_counts_games_not_copies_or_latest_players(self):
        counts=carried_game_counts(self.store._db,self.replays,C.NOW)
        self.assertEqual(counts[1],4)  # one player, two copies, four known-result games
        self.assertEqual(counts[9],4)  # extra included in carrying, not draw ranking
        self.store.set_fact_exclusion(self.rows[0]['digest'],'甲',reason='test')
        self.assertEqual(carried_game_counts(self.store._db,self.replays,C.NOW)[1],3)
        self.assertEqual(carried_game_counts(self.store._db,self.replays,C.NOW+14*86400),{})

    def test_draw_and_not_draw_require_different_completeness(self):
        db=self.store._db
        yes=query(db,self.replays,C.NOW,{'card':['1'],'mode':['drawn']})
        self.assertEqual(len(yes['samples']),3)
        no=query(db,self.replays,C.NOW,{'card':['1'],'mode':['not_drawn']})
        self.assertEqual(len(no['samples']),1)
        self.assertTrue(all(r['digest']!=self.rows[4]['digest']for r in no['samples']))
        self.assertEqual(query(db,self.replays,C.NOW,{'deck_none':['1']})['total'],0)
        self.assertEqual(query(db,self.replays,C.NOW,{'first':['1'],'outcome':['loss']})['total'],0)
        rank=draw_rankings(db,self.replays,C.NOW)[1]
        self.assertEqual((rank['overall']['wins'],rank['overall']['denominator']),(2,3))
        self.assertEqual(rank['first']['denominator'],2)
        self.assertEqual(rank['second']['denominator'],1)

    def test_explicit_not_drawn_list_combines_with_the_drawn_list(self):
        db=self.store._db
        # 甲每局都带卡1和卡2；卡2从未抽到，卡1在i=1、2、5抽到。
        self.assertEqual(query(db,self.replays,C.NOW,{'not_card':['2']})['total'],3)
        self.assertEqual(query(db,self.replays,C.NOW,{'card':['1'],'mode':['drawn'],'not_card':['2']})['total'],2)
        self.assertEqual(query(db,self.replays,C.NOW,{'not_card':['1']})['total'],1)
        self.assertEqual(query(db,self.replays,C.NOW,{'not_card':['1','2'],'not_match':['all']})['total'],1)
        self.assertEqual(query(db,self.replays,C.NOW,{'not_card':['1','2'],'not_match':['any']})['total'],3)
        self.assertEqual(query(db,self.replays,C.NOW,{'not_card':['999']})['total'],0)
        # 对手侧同理：乙只在i=1、2带卡3，且两局都有完整抽卡过程。
        self.assertEqual(query(db,self.replays,C.NOW,{'opponent_not_card':['3']})['total'],2)
        self.assertEqual(query(db,self.replays,C.NOW,{'opponent_not_card_none':['1']})['total'],0)

    def test_both_sides_must_be_in_current_top100(self):
        db=self.store._db
        # 默认名单只有甲；乙未上榜，所以“双方都在前100”一条都不该留下。
        self.assertEqual(query(db,self.replays,C.NOW,{'internal':['1']})['total'],0)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)
        # 乙上榜后双方视角都进入候选：甲5条+乙5条。
        everything=query(db,self.replays,C.NOW,{})
        self.assertEqual(everything['total'],10)
        both=query(db,self.replays,C.NOW,{'internal':['1']})
        self.assertEqual(both['total'],10)
        self.assertTrue(all(r['current_rank']<=100 and r['opponent_current_rank']<=100 for r in both['samples']))
        # 与卡片条件叠加时仍按交集过滤（默认条件为“整局实际抽到”）。
        self.assertEqual(query(db,self.replays,C.NOW,{'internal':['1'],'card':['1'],'mode':['drawn']})['total'],3)
        # 掉榜后立即不再算“双方前100”。
        self.store.replace_top100(T.roster(['甲']),C.NOW)
        self.assertEqual(query(db,self.replays,C.NOW,{'internal':['1']})['total'],0)

    def test_expired_excluded_and_invalid_card(self):
        self.store.set_fact_exclusion(self.rows[0]['digest'],'甲',reason='test')
        self.assertEqual(query(self.store._db,self.replays,C.NOW,{'card':['1'],'mode':['drawn']})['total'],2)
        self.assertEqual(query(self.store._db,self.replays,C.NOW+14*86400,{})['total'],0)
        self.assertRaises(ValueError,query,self.store._db,self.replays,C.NOW,{'card':['bad']})
