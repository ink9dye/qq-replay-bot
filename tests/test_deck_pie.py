# -*- coding: utf-8 -*-
"""三层饼图数据：内环主大类、中环交集细分、外环名字。"""
import unittest

from bot import deck_pie


def dist(rows):
    return [dict(name=name, players=players) for name, players in rows]


GROUPS = ['混沌', '三幻魔', '烙印', '影依', '星辰']


class DeckPieTests(unittest.TestCase):
    def test_primary_category_prefers_the_most_repeated_field(self):
        # 混沌出现在 3 个名字里；烙印也 3 个。混沌烙印两者重叠，按重复程度/覆盖/先后归混沌。
        rows = dist([('混沌烙印', 1), ('混沌三幻魔', 2), ('混沌影依', 1),
                     ('烙印甲', 1), ('烙印乙', 1), ('其他一', 1), ('未识别', 1)])
        pie = deck_pie.layers(rows, GROUPS)
        inner = {item['name']: item for item in pie['inner']}
        self.assertEqual(inner['混沌']['players'], 4)
        self.assertEqual(inner['烙印']['players'], 2)
        self.assertEqual(sum(item['players'] for item in pie['inner']), 8)
        self.assertAlmostEqual(pie['middle_percent'], 100.0, places=1)
        self.assertAlmostEqual(pie['outer_percent'], 100.0, places=1)
        middle = {(item['primary'], item['label']): item for item in pie['middle']}
        self.assertEqual(middle[('混沌', '∩烙印')]['players'], 1)
        self.assertEqual(middle[('混沌', '独占')]['players'], 3)

    def test_threshold_is_three_distinct_names(self):
        rows = dist([('混沌烙印', 3), ('混沌影依', 2), ('烙印甲', 1), ('其他一', 1)])
        pie = deck_pie.layers(rows, GROUPS)
        names = {item['name'] for item in pie['inner']}
        self.assertNotIn('混沌', names)   # 只有 2 个不同名字，不算大类
        self.assertEqual(names, {'其他'})

    def test_match_fields_keeps_longest_and_order(self):
        self.assertEqual(deck_pie.match_fields('混沌三幻魔', ['混沌', '幻魔', '三幻魔']),
                         ['混沌', '三幻魔'])
        self.assertEqual(deck_pie.match_fields('未识别', GROUPS), [])

    def test_three_names_create_inner_ring(self):
        rows = dist([('混沌烙印', 2), ('混沌三幻魔', 1), ('混沌影依', 1), ('其他一', 2)])
        pie = deck_pie.layers(rows, GROUPS)
        inner = {item['name']: item for item in pie['inner']}
        self.assertEqual(inner['混沌']['players'], 4)
        self.assertEqual(inner['混沌']['percent'], 66.7)
        self.assertAlmostEqual(sum(item['percent'] for item in pie['inner']), 100.0, places=0)

    def test_colors_are_stable_and_distinct_for_categories(self):
        rows = dist([('混沌烙印', 1), ('混沌三幻魔', 1), ('混沌影依', 1),
                     ('烙印甲', 1), ('烙印乙', 1), ('其他一', 1)])
        first = deck_pie.layers(rows, GROUPS)
        second = deck_pie.layers(rows, GROUPS)
        self.assertEqual([item['color'] for item in first['inner']],
                         [item['color'] for item in second['inner']])
        colors = [item['color'] for item in first['inner'] if item['name'] not in ('其他', '未识别')]
        self.assertEqual(len(colors), len(set(colors)))


if __name__ == '__main__':
    unittest.main()
