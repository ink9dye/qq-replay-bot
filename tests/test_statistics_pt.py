import unittest
from datetime import datetime
from bot.statistics_pt import InteractiveHistory
from bot.player_history import CST
from test_player_history import game

NOW=datetime(2026,10,26,20,tzinfo=CST)

class InteractivePTTests(unittest.TestCase):
    def test_different_players_do_not_block_each_other(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier=threading.Barrier(2)
        def fetch(endpoint,params,timeout):
            barrier.wait(timeout=3)
            return dict(data=[],total=0)
        service=InteractiveHistory(fetch,lambda:NOW)
        with ThreadPoolExecutor(2)as pool:
            results=list(pool.map(service.query,['甲','乙']))
        self.assertEqual([r['status']for r in results],['ok','ok'])
    def test_both_sides_delta_unknown_winner_dedup_and_month(self):
        rows=[game(player='甲',when='2026-10-02T00:00:00Z',before=1000,after=1012.34),
              game(player='甲',side='b',when='2026-10-03T00:00:00Z',before=1012.34,after=1009.21,winner='对手'),
              game(player='甲',when='2026-10-04T00:00:00Z',before=1009.21,after=1009.21,winner='missing'),
              game(player='甲',when='2026-09-01T00:00:00Z')]
        calls=[]
        def fetch(endpoint,params,timeout):
            calls.append(endpoint);return dict(total=5,data=rows+[rows[0]])
        service=InteractiveHistory(fetch,lambda:NOW);r=service.query('甲')
        self.assertEqual((r['status'],r['count'],r['month']),('ok',3,'2026-10'))
        self.assertEqual([g['delta'] for g in r['games']],[12.34,-3.13,0])
        self.assertEqual([g['outcome'] for g in r['games']],['win','loss','unknown'])
        self.assertEqual([g['opponent'] for g in r['games']],['对手']*3)
        self.assertIs(service.query('甲'),r);self.assertEqual(calls,['history'])

    def test_partial_month_and_failure_never_fake_chart(self):
        r=InteractiveHistory(lambda *a:dict(data=[game(player='甲')],total=100000),lambda:NOW).query('甲')
        self.assertEqual(r['status'],'incomplete');self.assertEqual(r['games'],[])
        calls=[]
        def fail(*a):calls.append(1);raise TimeoutError()
        service=InteractiveHistory(fail,lambda:NOW)
        self.assertEqual(service.query('甲')['status'],'unavailable')
        service.query('甲');self.assertEqual(len(calls),1)

    def test_empty_month_and_player_validation(self):
        service=InteractiveHistory(lambda *a:dict(data=[],total=0),lambda:NOW)
        self.assertEqual(service.query('甲')['count'],0)
        self.assertRaises(ValueError,service.query,'')

if __name__=='__main__':unittest.main()
