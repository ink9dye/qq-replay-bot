import json
import unittest
from urllib.parse import quote

import test_card_statistics as C
import test_top100 as T
from recording_filter_fixture import set_up
from bot.statistics_portal import StatisticsPortal
from bot.statistics_recording_filters import query


class RecordingParamTests(unittest.TestCase):
    def setUp(self):
        set_up(self)
        self.store.replace_top100(T.roster(['甲','乙']),C.NOW)

    def q(self,args):
        return query(self.store._db,self.replays,C.NOW,args)

    def test_series_own_and_opponent(self):
        base=self.q({})['total']
        self.assertGreater(base,0)
        self.assertEqual(self.q({'series':['X']})['total'],4)
        self.assertEqual(self.q({'series':['X','Z']})['total'],9)
        self.assertEqual(self.q({'series':['不存在的系列']})['total'],0)
        self.assertEqual(self.q({'opponent_series':['Z']})['total'],5)
        self.assertEqual(self.q({'opponent_series':['X']})['total'],4)

    def test_time_range_inclusive_start_exclusive_end(self):
        rows=sorted(r['recorded_at'] for r in self.q({})['samples'])
        start=rows[2]
        self.assertEqual(self.q({'start':[__import__('datetime').datetime.fromtimestamp(start,__import__('datetime').timezone.utc).isoformat()]})['total'],8)
        end=rows[2]
        self.assertEqual(self.q({'end':[__import__('datetime').datetime.fromtimestamp(end,__import__('datetime').timezone.utc).isoformat()]})['total'],2)
        both={'start':[__import__('datetime').datetime.fromtimestamp(rows[2],__import__('datetime').timezone.utc).isoformat()],
              'end':[__import__('datetime').datetime.fromtimestamp(rows[4],__import__('datetime').timezone.utc).isoformat()]}
        self.assertEqual(self.q(both)['total'],2)

    def test_series_combines_with_cards_and_top100(self):
        result=self.q({'series':['X'],'card':['1'],'mode':['drawn'],'internal':['1']})
        self.assertEqual(result['total'],3)
        self.assertTrue(all(r['current_rank']<=100 and r['opponent_current_rank']<=100 for r in result['samples']))

    def test_invalid_parameters_raise(self):
        for args in ({'outcome':['bogus']},{'first':['2']},{'mode':['bogus']},{'match':['bogus']},
                     {'series':['x'*200]},{'start':['not-a-time']},
                     {'start':['2026-10-28T00:00:00']},{'start':['2026-10-29T00:00:00+08:00'],'end':['2026-10-28T00:00:00+08:00']}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.q(args)


    def test_player_name_filter(self):
        base=self.q({})['total']
        self.assertGreater(base,0)
        result=self.q({'player':['甲']})
        self.assertLessEqual(result['total'],base)
        self.assertTrue(all('甲' in r['player'] for r in result['samples']))
        self.assertEqual(self.q({'player':['不存在的玩家']})['total'],0)

    def test_scalar_and_list_validation(self):
        cases=({'internal':['bogus']},{'internal':['0','1']},{'mode':['drawn','used']},
               {'deck':['x'*129]},{'deck':['x']*101},{'deck':[' ']},{'opponent':['y'*129]},
               {'match':['any','all']},{'opponent_match':['bad']})
        for args in cases:
            with self.subTest(args=sorted(args)):
                with self.assertRaises(ValueError):
                    self.q(args)

    def test_unknown_series_is_legal_zero_not_error(self):
        self.assertEqual(self.q({'series':['不存在的系列']})['total'],0)
        self.assertEqual(self.q({'opponent_series':['不存在的系列']})['total'],0)

    def test_valid_equivalents_still_work(self):
        self.assertEqual(self.q({'internal':['1']})['total'],10)
        self.assertEqual(self.q({'internal':['0']})['total'],10)
        self.assertEqual(self.q({'deck':['甲类型']})['total'],4)

    def test_portal_returns_400_and_keeps_zero_as_200(self):
        portal=StatisticsPortal(self.store.path,self.replays,lambda:C.NOW,'x'*43)
        headers={'Host':'127.0.0.1:8788'}
        headers['Cookie']=portal.route('/statistics/access/'+'x'*43,headers,'127.0.0.1')[3]['Set-Cookie'].split(';',1)[0]
        bad=portal.route('/statistics/api/recordings?outcome=bogus',headers,'127.0.0.1')
        self.assertEqual(bad[0],400)
        self.assertIn('error',json.loads(bad[2]))
        zero=portal.route('/statistics/api/recordings?series='+quote('不存在的系列'),headers,'127.0.0.1')
        self.assertEqual(zero[0],200)
        self.assertEqual(json.loads(zero[2])['total'],0)

    def test_new_filter_version_preserves_card_filter_contract(self):
        response=self.q({})
        self.assertEqual(response['card_filters_version'],3)
        self.assertEqual(response['recording_filters_version'],4)
