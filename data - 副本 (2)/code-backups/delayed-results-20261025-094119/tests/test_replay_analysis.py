import json
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import replay_native as N
from bot.replay_analysis import VERSION, Events, analyze, apply_live_evidence, digest_file, fill_usage, mt19937_first, sidecar
from bot.replay_result import infer_bo3, matching_history, supplement_history
from bot.watch_core import Spectator, ReplayWriter, STOC_GAME_MSG, STOC_REPLAY, STOC_DUEL_START, cleanup_old_files
from test_bot import room, temporary_service
from test_deck_links import pack_replay, save_game


class EventTests(unittest.TestCase):
    def test_extra_appearance_requires_success_and_never_mixes_with_main_side(self):
        events = Events()
        events.observe(N.Message(40, b'\0'))
        def summon(code, sequence):
            return N.Message(62, struct.pack('<I',code)+bytes([0,4,sequence,1]))
        events.observe(summon(100,0))
        # 第一张召唤被无效后离场；随后另一张成功，不能把第一张也计为出场。
        events.observe(N.Message(50, struct.pack('<I',100)+bytes([0,4,0,1,0,16,0,1])+bytes(4)))
        events.observe(summon(200,1))
        events.observe(N.Message(63,b''))
        events.observe(N.Message(70,struct.pack('<I',300)+bytes([0,2,0,1,0,2,0])+bytes(5)))
        report=dict(players=[{'index':0,'name':'甲','main':[300],'extra':[100,200]}])
        fill_usage(report,events)
        player=report['players'][0]
        appearances={r['code']:r['appeared'] for r in player['extra_appearances']}
        self.assertEqual(appearances,{100:False,200:True})
        self.assertEqual([c['code'] for c in player['used_cards'] if c['category']=='main_side'],[300])

    def test_initiative_and_usage_are_per_actor_not_card_controller(self):
        e = Events()
        e.observe(N.Message(40, b'\x01'))
        e.observe(N.Message(40, b'\x00'))
        body = struct.pack('<I', 14558127) + bytes([0, 4, 0, 1, 1, 4, 0]) + bytes(5)
        e.observe(N.Message(70, body))
        e.observe(N.Message(70, body))
        e.observe(N.Message(5, b'\x01\x00'))
        self.assertEqual(e.first, 1)
        self.assertEqual([v['player'] for v in e.items], [1, 1])
        self.assertEqual(e.winner, 1)

    def test_mt19937_matches_published_reference_seed(self):
        self.assertEqual(mt19937_first(5489), 3499211612)

    def test_invalid_compression_and_truncated_input_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.yrp'
            path.write_bytes(b'x')
            report = analyze(path, Path(temp))
            self.assertEqual(report['playback'], 'failed')
            self.assertIsNone(report['winner'])
        with self.assertRaises(N.ReplayError): N.parse_responses(b'\x41')


class ResultTests(unittest.TestCase):
    def test_three_game_inference_is_not_shared_with_an_incomplete_link_snapshot(self):
        from bot.player_history import moment
        from bot.replay_analysis_ui import request_report
        stamp=moment('2026-10-24T10:20:00Z').timestamp()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'third.yrp';path.write_bytes(b'fixture')
            digest=digest_file(path)
            report=dict(version=VERSION,digest=digest,players=[{'name':'甲'},{'name':'乙'}],
                recorded_at=stamp,winner=None,result='unknown',warnings=[],match_evidence=self.record())
            sidecar(path).write_text(json.dumps(report),encoding='utf-8')
            games=[dict(game_no=i,digest=(digest if i==3 else str(i)),recorded_at=stamp-60*(3-i),players=['甲','乙']) for i in (1,2,3)]
            full=request_report(path,Path(temp),{'confirmed':True,'games':games})
            self.assertEqual(full['winner'],0)
            incomplete=request_report(path,Path(temp))
            self.assertIsNone(incomplete['winner'])
            self.assertIsNone(json.loads(sidecar(path).read_text())['winner'])

    def test_third_game_equals_match_winner_and_first_two_are_ambiguous(self):
        for winner, scores in ((0, [2, 1]), (1, [1, 2])):
            self.assertEqual(infer_bo3(winner, scores, 2, 3), winner)
            self.assertIsNone(infer_bo3(winner, scores, 0, 3))
            self.assertIsNone(infer_bo3(winner, scores, 1, 3))
            self.assertEqual(infer_bo3(winner, scores, 1, 3, {0: winner}), 1-winner)
        self.assertIsNone(infer_bo3(0, [2, 1], 2, 4))
        self.assertIsNone(infer_bo3(1, [2, 0], 1, 2))
        self.assertEqual(infer_bo3(0, [2, 0], 0, 2), 0)

    def record(self):
        return dict(type='athletic', usernamea='甲', usernameb='乙', winner='甲', userscorea=2,
                    userscoreb=1, start_time='2026-10-24T10:00:00Z', end_time='2026-10-24T10:30:00Z',
                    pta_ex=1000, pta=1010)

    def test_history_requires_same_players_unique_interval_and_complete_three_games(self):
        from bot.player_history import moment
        row = self.record()
        stamp = moment('2026-10-24T10:20:00Z').timestamp()
        self.assertEqual(matching_history(['乙', '甲'], stamp, [row]), [row])
        self.assertEqual(matching_history(['甲', '丙'], stamp, [row]), [])
        report = dict(players=[{'name': '乙'}, {'name': '甲'}], recorded_at=stamp,
                      digest='g3', winner=None, result='unknown', warnings=[])
        games = [dict(game_no=i, digest='g'+str(i), recorded_at=stamp-100*(3-i), players=['甲','乙']) for i in (1, 2, 3)]
        supplement_history(report, {'confirmed': True, 'games': games}, lambda *a: {'data': [row]})
        self.assertEqual(report['winner'], 1)
        self.assertEqual(report['result_source'], 'history_inferred')
        report.update(winner=None, result='unknown')
        report.pop('result_source', None)
        supplement_history(report, {'confirmed': True, 'games': games[1:]}, lambda *a: {'data': [row]})
        self.assertIsNone(report['winner'])

    def test_points_alone_never_assign_a_small_game_winner(self):
        row = self.record()
        row.pop('userscorea'); row.pop('userscoreb')
        from bot.player_history import moment
        report = dict(players=[{'name':'甲'},{'name':'乙'}], recorded_at=moment('2026-10-24T10:20:00Z').timestamp(), winner=None)
        supplement_history(report, fetch=lambda *a: {'data':[row]})
        self.assertIsNone(report['winner'])


class CaptureTests(unittest.TestCase):
    def test_complete_live_journal_can_restore_usage_without_matching_local_scripts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'game.yrp'
            body = struct.pack('<I',14558127)+bytes([0,2,0,0,1,2,0])+bytes(5)
            evidence = dict(digest='known',players=['甲','乙'],winner=1,captured_from_start=True,
                events=['2800', (bytes([70])+body).hex(), '050100'],
                previous_game={'digest':'previous','loser':'乙'})
            sidecar(path,'evidence').write_text(json.dumps(evidence),encoding='utf-8')
            report=dict(digest='known',winner=None,warnings=[],card_names={'14558127':'灰流丽'},
                players=[{'index':0,'name':'甲','main':[],'extra':[]},
                         {'index':1,'name':'乙','main':[14558127],'extra':[]}])
            apply_live_evidence(report,path)
            self.assertEqual(report['playback'],'live_complete')
            self.assertEqual(report['first_player'],0)
            self.assertEqual(report['players'][1]['used_cards'][0]['code'],14558127)
            self.assertEqual(report['initiative_chooser'],'乙')

    def test_midgame_join_never_claims_full_live_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            spectator=Spectator({},room('进行中',['甲','乙'])[0],ReplayWriter(Path(temp)))
            spectator.handle(STOC_GAME_MSG,bytes([4])+bytes(18))
            self.assertFalse(spectator._live_from_start)

    def test_live_surrender_result_binds_to_replay_order_then_resets(self):
        with tempfile.TemporaryDirectory() as temp, patch('watch.arena_rank_number', return_value=None), patch('watch.deck_title_for', return_value='测试'):
            writer = ReplayWriter(Path(temp), ['甲'])
            spectator = Spectator({}, room('测试房间', ['甲', '乙'])[0], writer)
            spectator.handle(STOC_GAME_MSG, bytes([5, 1, 0]))
            blob = pack_replay(['乙', '甲'], [([1], []), ([2], [])])
            spectator.handle(STOC_REPLAY, blob)
            path = next(Path(temp).rglob('*.yrp'))
            evidence = json.loads(sidecar(path, 'evidence').read_text(encoding='utf-8'))
            self.assertEqual(evidence['players'], ['乙', '甲'])
            self.assertEqual(evidence['winner'], 1)
            self.assertIsNone(spectator._game_result)
            report = dict(digest=digest_file(path), players=[{'name':'乙'},{'name':'甲'}], winner=None, warnings=[])
            apply_live_evidence(report, path)
            self.assertEqual(report['winner'], 1)
            self.assertEqual(report['result_source'], 'live_server')
            report['digest'] = 'wrong'
            report['winner'] = None
            apply_live_evidence(report, path)
            self.assertIsNone(report['winner'])
            spectator.handle(STOC_GAME_MSG, b'\x05\x00\x00')
            spectator.handle(STOC_DUEL_START, b'')
            self.assertIsNone(spectator._game_result)

    def test_cleanup_removes_analysis_and_evidence_with_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'game.yrp'; path.write_bytes(b'replay')
            for kind in ('analysis', 'evidence'): sidecar(path, kind).write_text('{}')
            self.assertEqual(cleanup_old_files(root, root/'decks', time.time()+14*86400), 1)
            self.assertEqual(list(root.iterdir()), [])

    def test_analysis_routes_enforce_token_membership_focus_and_window(self):
        with temporary_service() as s, patch('bot.replay_analysis_ui.request_report', return_value=None) as request, patch('watch.deck_title_for', return_value='测试'), patch('watch.arena_rank_number', return_value=None), patch('bot.service.arena_rank_number', return_value=None):
            s.handle_text('10001', '订阅 甲 全部 静默')
            replay = save_game(s, '甲', [1], [])
            link = s.share.create_link('10001','甲',[replay['id']],s.clock()-100)
            token = link['token']
            self.assertEqual(s.share.page(token,'analysis',str(replay['id']))[0], 200)
            self.assertEqual(s.share.page(token,'analysis-json',str(replay['id']))[0], 202)
            self.assertEqual(s.share.page(token,'analysis','999999')[0], 404)
            self.assertEqual(s.share.page('bad','analysis',str(replay['id']))[0], 404)
            s.clock.now += 14*86400
            self.assertEqual(s.share.page(token,'analysis',str(replay['id']))[0], 410)


if __name__ == '__main__': unittest.main()
