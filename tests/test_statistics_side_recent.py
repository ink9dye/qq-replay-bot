import unittest

from bot.statistics_side_recent import build_plan, format_text


def card(code, copies=1, partition='main'):
    return dict(code=code, copies=copies, partition=partition)


def game(number, label='B', first_player=1, added=(), removed=(), status='known'):
    return dict(game_no=number, first_player=first_player, digest=f'{label}-{number}',
                opponent_match_deck_type=label,
                changes=dict(status=status, added=list(added), removed=list(removed)))


def group(label, recorded_at, first_player=1, side=(), seats=None):
    seats = seats or {}
    games = [game(1, label, first_player)]
    for number, added, removed in side:
        games.append(game(number, label, seats.get(number, first_player), added, removed))
    return dict(id=f'{label}-{recorded_at}', recorded_at=recorded_at,
                associated=True, sequence_ambiguous=False, games=games)


def detail(groups):
    return dict(person=dict(player='甲', deck_type='A'), selected=dict(
        digest='d', recorded_at=1, observation_kind='confirmed_first_game'),
        generated_at=1, match_groups=groups)


class BuildPlanTests(unittest.TestCase):
    def test_side_game_seat_decides_bucket_not_first_game(self):
        """第2局座位与首局相反时，换入换出必须算到第2局的先/后攻。"""
        groups = [group('B', 100, 1, ((2, [card(4, 3)], [card(1, 3)]),), seats={2: 0})]
        plan = build_plan(detail(groups))
        b = plan['opponents'][0]
        self.assertEqual((b['first']['side_games'], b['second']['side_games']), (0, 1))
        self.assertEqual([item['code'] for item in b['second']['added']], [4])
        self.assertEqual(b['second']['added'][0]['max_copies'], 3)
        self.assertEqual([item['code'] for item in b['second']['removed']], [1])
        self.assertEqual(b['first']['added'], [])
        self.assertEqual(b['sessions'][0]['first_player'], 1)
        self.assertEqual(b['sessions'][0]['side_seats'], ['second'])
        self.assertEqual(b['seat_unknown_games'], 0)

    def test_one_match_can_feed_both_seats(self):
        """同一场第2局先攻、第3局后攻时，两局按各自座位分别计入。"""
        groups = [group('B', 100, 0, ((2, [card(4)], []), (3, [card(5)], [])),
                        seats={2: 1, 3: 0})]
        plan = build_plan(detail(groups))
        b = plan['opponents'][0]
        self.assertEqual((b['first']['side_games'], b['second']['side_games']), (1, 1))
        self.assertEqual([item['code'] for item in b['first']['added']], [4])
        self.assertEqual([item['code'] for item in b['second']['added']], [5])
        self.assertEqual(b['sessions'][0]['side_seats'], ['first', 'second'])

    def test_unknown_seat_is_not_guessed(self):
        """座位缺失的换边局不计入任何方向，只单独计数。"""
        groups = [group('B', 100, 1, ((2, [card(4)], []),), seats={2: None})]
        plan = build_plan(detail(groups))
        b = plan['opponents'][0]
        self.assertEqual((b['first']['side_games'], b['second']['side_games']), (0, 0))
        self.assertEqual(b['seat_unknown_games'], 1)
        self.assertEqual(b['first']['added'], [])
        self.assertEqual(b['sessions'][0]['side_seats'], [])
        self.assertTrue(b['sample_low'])

    def test_top_decks_recent_five_and_seat_split(self):
        groups = []
        for index, moment in enumerate((100, 200, 300, 400, 500, 600, 700)):
            added = [card(999)] if index == 0 else [card(4)]
            removed = [] if index == 0 else [card(1, 2)]
            first = 1 if index % 2 == 0 else 0
            groups.append(group('B', moment, first, ((2, added, removed),)))
        groups.append(group('C', 800, 1))
        groups.append(group('C', 850, 0))
        groups.append(group('D', 900, 1, ((2, [card(7)], []),)))

        plan = build_plan(detail(groups), top=6, recent=5)
        self.assertEqual([item['deck'] for item in plan['opponents']], ['B', 'C', 'D'])
        b = plan['opponents'][0]
        self.assertEqual((b['matches'], b['recent_matches'], b['side_games']), (7, 5, 5))
        self.assertFalse(b['sample_low'])
        self.assertEqual((b['first']['side_games'], b['second']['side_games']), (3, 2))
        added = {item['code']: item for item in b['first']['added']}
        self.assertNotIn(999, added)
        self.assertEqual(added[4]['sessions'], 3)
        self.assertEqual(added[4]['max_copies'], 1)
        removed = {item['code']: item for item in b['second']['removed']}
        self.assertEqual(removed[1]['sessions'], 2)
        self.assertEqual(removed[1]['max_copies'], 2)
        self.assertEqual([item['recorded_at'] for item in b['sessions']],
                         [700, 600, 500, 400, 300])
        self.assertTrue(plan['opponents'][1]['sample_low'])
        self.assertEqual(plan['opponents'][1]['side_games'], 0)

    def test_same_match_max_copies_not_sum(self):
        groups = [group('B', 100, 1, ((2, [card(4, 1)], []),
                                      (3, [card(4, 2)], [])))]
        plan = build_plan(detail(groups))
        b = plan['opponents'][0]
        self.assertEqual(b['side_games'], 2)
        self.assertEqual(b['first']['added'][0]['sessions'], 1)
        self.assertEqual(b['first']['added'][0]['max_copies'], 2)
        self.assertEqual(b['sessions'][0]['added'][0]['copies'], 2)

    def test_top_cap_six(self):
        groups = []
        for label_index, label in enumerate(('A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7')):
            for offset in range(8 - label_index):
                groups.append(group(label, 100 + label_index * 100 + offset,
                                    1, ((2, [card(label_index + 1)], []),)))
        plan = build_plan(detail(groups))
        self.assertEqual(len(plan['opponents']), 6)
        self.assertEqual([item['deck'] for item in plan['opponents']],
                         ['A1', 'A2', 'A3', 'A4', 'A5', 'A6'])

    def test_ambiguous_sequence_is_ignored(self):
        item = group('B', 100, 1, ((2, [card(4)], []),))
        item['sequence_ambiguous'] = True
        plan = build_plan(detail([item]))
        self.assertEqual(plan['opponents'][0]['side_games'], 0)
        self.assertTrue(plan['opponents'][0]['sample_low'])

    def test_unknown_deck_is_not_listed(self):
        plan = build_plan(detail([group('未识别', 100, 1, ((2, [card(4)], []),))]))
        self.assertEqual(plan['opponents'], [])
        self.assertEqual(plan['total_matches'], 1)

    def test_no_groups(self):
        plan = build_plan(detail([]))
        self.assertEqual(plan['opponents'], [])
        self.assertIn('没有可用于 Side', plan['notes'][-1])


class FormatTests(unittest.TestCase):
    def names(self, code):
        return f'卡{code}'

    def test_text_contains_sides_and_copy_ranges(self):
        groups = [group('B', 100, 1, ((2, [card(4)], [card(1, 2)]),)),
                  group('C', 200, 0, ((2, [card(5, 2)], []),))]
        plan = build_plan(detail(groups))
        pages = format_text(plan, self.names)
        self.assertEqual(len(pages), 1)
        text = pages[0]
        self.assertIn('换备 · 甲', text)
        self.assertIn('B（1场', text)
        self.assertIn('先攻（1局）：+卡4×1 ／ -卡1×2', text)
        self.assertIn('后攻：没有有效换边局', text)
        self.assertIn('C（1场', text)
        self.assertIn('后攻（1局）：+卡5×2 ／ -无', text)

    def test_pages_split_at_deck_boundaries(self):
        groups = []
        for index in range(4):
            groups.append(group(f'B{index}', 100 * (index + 1), 1,
                                ((2, [card(index + 1)], []),)))
        plan = build_plan(detail(groups), top=6, recent=5)
        pages = format_text(plan, self.names)
        self.assertEqual(len(pages), 2)
        self.assertIn('（1/2）', pages[0])
        self.assertIn('（2/2）', pages[1])
        self.assertIn('B3（1场', pages[0])
        self.assertIn('B0（1场', pages[1])

    def test_no_opponents_page(self):
        pages = format_text(build_plan(detail([])), self.names)
        self.assertEqual(len(pages), 1)
        self.assertIn('没有足够的对手卡组数据', pages[0])


if __name__ == '__main__':
    unittest.main()
