import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import replay_native as N
from bot.replay_analysis import Events, fill_draws, apply_live_evidence, sidecar
import watch
from test_deck_links import pack_replay


def draw(events, actor, cards):
    events.observe(N.Message(N.MSG_DRAW, bytes([actor, len(cards)]) +
                            b''.join(struct.pack('<I', code) for code in cards)))


def use(events, code):
    events.observe(N.Message(N.MSG_CHAINING, struct.pack('<I', code) +
        bytes([0, 4, 0, 1, 0, 4, 0]) + bytes(5)))


def report():
    return dict(digest='test', winner=None, warnings=[], players=[
        dict(index=0, name='甲', main=[101, 102, 103, 104], extra=[201]),
        dict(index=1, name='乙', main=[301], extra=[])])


class DrawFactTests(unittest.TestCase):
    def test_initial_and_later_draws_are_counted_once_per_card_with_actual_quantities(self):
        events = Events(start_hand=5)
        draw(events, 0, [101, 101, 102, 103, 104])
        events.observe(N.Message(N.MSG_NEW_TURN, b'\x01'))
        draw(events, 1, [301])  # 后攻正常抽卡和效果抽卡都使用MSG_DRAW
        draw(events, 0, [0x80000065, 102])
        r = report(); fill_draws(r, events, complete=True)
        cards = {c['code']: c for c in r['players'][0]['drawn_cards']}
        self.assertEqual((cards[101]['draw_count'], cards[101]['initial_count']), (3, 2))
        self.assertEqual((cards[102]['draw_count'], cards[102]['initial_count']), (2, 1))
        self.assertTrue(r['players'][0]['draw_record_complete'])
        self.assertEqual(r['players'][1]['drawn_cards'][0]['initial_count'], 0)

    def test_search_and_recovery_moves_to_hand_are_not_draws(self):
        events = Events(start_hand=0)
        for origin in (N.LOCATION_DECK, 0x10):
            body = struct.pack('<I', 101) + bytes([0, origin, 0, 0, 0, N.LOCATION_HAND, 0, 0]) + bytes(4)
            events.observe(N.Message(N.MSG_MOVE, body))
        r = report(); fill_draws(r, events, complete=True)
        self.assertEqual(r['players'][0]['drawn_cards'], [])
        self.assertTrue(r['players'][0]['draw_record_complete'])

    def test_use_before_draw_is_separate_from_use_after_draw(self):
        events = Events(); use(events, 101); draw(events, 0, [101])
        r = report(); fill_draws(r, events, complete=True)
        self.assertFalse(r['players'][0]['drawn_cards'][0]['used_after_draw'])
        use(events, 101)
        r = report(); fill_draws(r, events, complete=True)
        self.assertTrue(r['players'][0]['drawn_cards'][0]['used_after_draw'])

    def test_hidden_card_codes_make_negative_draw_claims_unknown(self):
        events = Events(start_hand=5); draw(events, 0, [0, 101, 0])
        r = report(); fill_draws(r, events, complete=True, source='live_server')
        player = r['players'][0]
        self.assertFalse(player['draw_record_complete'])
        self.assertEqual(player['unknown_draw_count'], 2)
        self.assertEqual([c['code'] for c in player['drawn_cards']], [101])

    def test_partial_draws_and_other_sources_do_not_become_complete_main_samples(self):
        events = Events(); draw(events, 0, [999, 201])
        r = report(); fill_draws(r, events, complete=False)
        self.assertFalse(r['players'][0]['draw_record_complete'])
        self.assertEqual({c['code']: c['category'] for c in r['players'][0]['drawn_cards']},
                         {201:'extra', 999:'other'})

    def test_malformed_draw_messages_fail_closed(self):
        for body in (b'', b'\x02\x00', b'\x00\x02'+bytes(4)):
            with self.subTest(body=body), self.assertRaises(N.ReplayError):
                Events().observe(N.Message(N.MSG_DRAW, body))

    def test_old_live_journal_without_draw_capture_version_cannot_claim_full_draws(self):
        self.check_live_journal(False)

    def test_hidden_live_draws_do_not_overwrite_complete_native_draws(self):
        self.check_live_journal(True)

    def check_live_journal(self, native):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'test.yrp'; path.write_bytes(b'test')
            r = report()
            if native:
                e = Events(start_hand=1); draw(e, 0, [101]); fill_draws(r, e, complete=True)
            evidence = dict(digest='test', winner=0, players=['甲','乙'], captured_from_start=True,
                            events=['5a000100000000','2800','050000'])
            if native: evidence['draw_capture_version'] = 1
            sidecar(path,'evidence').write_text(json.dumps(evidence), encoding='utf-8')
            apply_live_evidence(r,path)
            self.assertEqual(r['winner'], 0)
            if native:
                self.assertTrue(r['players'][0]['draw_record_complete'])
                self.assertEqual(r['players'][0]['draw_source'], 'native_replay')
            else:
                self.assertNotIn('draw_record_complete',r['players'][0])

    def test_watch_keeps_draw_packet_in_numbered_result_journal(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(watch,'arena_rank_number',return_value=None), \
             patch.object(watch,'deck_title_for',return_value='测试'), patch.object(watch,'log'):
            writer = watch.ReplayWriter(Path(temp), ['甲'])
            spectator = watch.Spectator({},watch.RoomInfo('t','t','t',['甲','乙']),writer)
            spectator.handle(watch.STOC_DUEL_START,b'')
            spectator.handle(watch.STOC_GAME_MSG,bytes([4,0x10])+bytes(17))
            packet=bytes([90,0,1])+struct.pack('<I',101)
            spectator.handle(watch.STOC_GAME_MSG,packet)
            spectator.handle(watch.STOC_GAME_MSG,bytes([40,0]))
            spectator.handle(watch.STOC_GAME_MSG,bytes([5,0,0]))
            spectator.handle(watch.STOC_REPLAY,pack_replay(['甲','乙'],[([101],[]),([301],[])]))
            data=json.loads(next(Path(temp).rglob('*.evidence.json')).read_text(encoding='utf-8'))
            self.assertEqual(data['draw_capture_version'],1)
            self.assertIn(packet.hex(),data['events'])
