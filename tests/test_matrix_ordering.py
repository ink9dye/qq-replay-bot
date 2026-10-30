import unittest
import test_card_statistics as C
from bot.statistics_matrix import query as matrix


class MatrixOrderTests(unittest.TestCase):
    setUp=C.CardStatisticsTests.setUp

    def usage_key(self,result):
        usage={item['deck_type']:item['players'] for item in result['deck_usage']}
        return lambda label:(label is None,-usage.get(label,0),label or '')

    def test_rows_and_columns_order_by_usage_then_name(self):
        result=matrix(self.store._db,self.replays,C.NOW)
        key=self.usage_key(result)
        self.assertEqual(result['rows'],sorted(result['rows'],key=key))
        self.assertEqual(result['columns'],sorted(result['columns'],key=key))


    def test_usage_uses_normalized_people_and_skips_unknown(self):
        from unittest.mock import patch
        from bot.statistics_matrix import deck_usage
        seed={'people':[{'player':'甲','deck_type':'A'},{'player':'乙','deck_type':'A'},
                        {'player':'丙','deck_type':None},{'player':'丁','deck_type':'B'}]}
        with patch('bot.player_statistics.query', return_value=seed):
            usage=deck_usage(self.store._db,self.replays,C.NOW)
        self.assertEqual(usage,{'A':2,'B':1})

    def test_raw_build_type_cannot_override_normalized_identity(self):
        from unittest.mock import patch
        from bot.statistics_matrix import deck_usage
        seed={'people':[{'player':'甲','deck_type':'可靠整场A','build':'b1','digest':'d1'}]}
        with patch('bot.player_statistics.query', return_value=seed):
            usage=deck_usage(self.store._db,self.replays,C.NOW)
        self.assertEqual(usage,{'可靠整场A':1})
        raw={row[0] for row in self.store._db.execute('SELECT deck_type FROM stat_builds')}
        self.assertNotEqual(raw,{'可靠整场A'})

    def test_ties_sort_by_name_and_unknown_last(self):
        from bot.statistics_matrix import deck_order
        usage={'B':2,'A':2,'C':1}
        self.assertEqual(deck_order(['B','A','C',None],usage),['A','B','C',None])

    def test_deck_usage_counts_each_player_once_and_skips_missing_builds(self):
        result=matrix(self.store._db,self.replays,C.NOW)
        players=[item['players'] for item in result['deck_usage']]
        self.assertTrue(players)
        self.assertTrue(all(isinstance(value,int) and value>0 for value in players))
        self.assertEqual(result['usage_basis'],
                         'current_top100_latest_valid_build_distinct_player')
        self.assertEqual([item['players'] for item in result['deck_usage']],
                         sorted(players,reverse=True))
