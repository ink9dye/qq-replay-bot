import unittest
from bot.replay_analysis_ui import render_report


class DrawUiTests(unittest.TestCase):
    def page(self, draw_complete=True, playback='responses_exhausted'):
        player=dict(name='测试玩家', index=0, main=[101,102,103], extra=[],
            used_cards=[dict(code=101,name='只使用过',category='main_side')],
            drawn_cards=[dict(code=102,name='抽到未使用',category='main_side',draw_count=1,used_after_draw=False)],
            draw_record_complete=draw_complete)
        return render_report(dict(players=[player],playback=playback,winner=None,first_player=0,
                                  warnings=[],digest='test',responses_consumed=0),'preview',1)

    def test_drawn_but_unused_is_visible_and_use_does_not_imply_draw(self):
        page=self.page()
        self.assertIn('<td>抽到未使用</td><td>102</td><td>已抽到</td><td>1</td><td>未用过</td><td>否</td>',page)
        self.assertIn('<td>只使用过</td><td>101</td><td>未抽到</td><td>0</td><td>已用过</td><td>不适用</td>',page)
        self.assertIn('不含检索或回收',page)

    def test_incomplete_draws_are_unknown_and_positive_counts_are_lower_bounds(self):
        page=self.page(False,'failed')
        self.assertIn('<td>只使用过</td><td>101</td><td>未知</td><td>—</td><td>已用过</td><td>未知</td>',page)
        self.assertIn('<td>抽到未使用</td><td>102</td><td>已抽到</td><td>≥1</td><td>未知</td><td>未知</td>',page)
