import unittest
from unittest.mock import patch
from bot import deck_pie


class IntersectionTests(unittest.TestCase):
    def test_multifield_pairs_follow_final_order_and_keep_real_counts(self):
        rows = [dict(name='mix1', primary='A', extras=['B', 'hidden'], players=8),
                dict(name='mix2', primary='B', extras=['hidden', 'A'], players=3),
                dict(name='mix3', primary='hidden', extras=['A', 'B'], players=6),
                dict(name='soloA', primary='A', extras=[], players=1),
                dict(name='soloB', primary='B', extras=[], players=1),
                dict(name='soloC', primary='C', extras=[], players=7)]
        base = dict(total=26, outer=rows, middle=rows,
                    inner=[dict(name=n, players=p, color='#123456', image=None)
                           for n, p in [('A',9), ('C',7), ('B',4)]])
        with patch.object(deck_pie, 'layers', return_value=base):
            result = deck_pie.single_ring([], [])
        slices = result['slices']
        self.assertEqual([s['name'] for s in slices], ['A', 'B', 'C'])
        self.assertEqual([s['players'] for s in slices], [9.5, 9.5, 7])
        bands = [b for s in slices for b in s['bands'] if b['kind']=='overlap']
        self.assertEqual(len(bands), 1)
        self.assertEqual((bands[0]['partner'], bands[0]['players']), ('B', 17))
        self.assertEqual(bands[0]['display_players'], 9.5)
        self.assertEqual(sum(s['players'] for s in slices)+result['other']['players'], 26)

    def test_nonadjacent_pairs_are_retained_once_for_legend(self):
        rows = [dict(name='mix',primary='A',extras=['C','hidden'],players=2)]
        base = dict(total=30,outer=rows,middle=rows,
                    inner=[dict(name=n,players=10,color='#123456') for n in ['A','B','C']])
        with patch.object(deck_pie,'layers',return_value=base):
            result=deck_pie.single_ring([],[])
        bands=[b for s in result['slices'] for b in s['bands'] if b['kind']=='overlap']
        self.assertEqual(len(bands),1)
        self.assertEqual(bands[0]['players'],2)
