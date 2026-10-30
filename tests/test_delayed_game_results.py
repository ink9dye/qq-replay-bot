import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import watch
from test_deck_links import pack_replay


class DelayedResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('arena_rank_number', None), ('deck_title_for', '测试'), ('log', None)]:
            patcher = patch.object(watch, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.s = watch.Spectator({}, watch.RoomInfo('test', 'test', 'test', ['甲', '乙'], True),
                                 watch.ReplayWriter(self.root, ['甲']))

    def start(self):
        self.s.handle(watch.STOC_GAME_MSG, bytes([4, 0x10]) + bytes(17))
        self.s.handle(watch.STOC_GAME_MSG, bytes([40, 0]))

    def win(self, winner):
        self.s.handle(watch.STOC_GAME_MSG, bytes([5, winner, 0]))

    def replay(self, number, names, code):
        text = ('正在发送第%d局决斗的录像。' % number).encode('utf-16le') + b'\0\0'
        self.s.handle(watch.STOC_CHAT, b'\x08\0' + text)
        blob = pack_replay(names, [([code], []), ([code + 1], [])])
        self.s.handle(watch.STOC_REPLAY, blob)
        import hashlib
        digest = hashlib.sha256(blob).hexdigest()
        return next(path for path in self.root.rglob('*.yrp')
                    if hashlib.sha256(path.read_bytes()).hexdigest() == digest)

    def evidence(self, path):
        metadata = Path(str(path) + '.evidence.json')
        return json.loads(metadata.read_text(encoding='utf-8')) if metadata.exists() else None

    def test_three_games_delivered_after_match_keep_each_result_and_previous_loser(self):
        self.s.handle(watch.STOC_DUEL_START, b'')
        for winner in (0, 0, 1):
            self.start()
            self.win(winner)
        self.s.handle(watch.STOC_DUEL_END, b'')
        paths = [self.replay(1, ['甲', '乙'], 1), self.replay(2, ['乙', '甲'], 3),
                 self.replay(3, ['甲', '乙'], 5)]
        results = [self.evidence(path) for path in paths]
        self.assertTrue(all(r is not None for r in results), '每份延迟录像都应有对应的小局证据')
        self.assertEqual([r['winner'] for r in results], [0, 0, 1])
        self.assertEqual([r['game_no'] for r in results], [1, 2, 3])
        self.assertTrue(all(r['captured_from_start'] for r in results))
        self.assertIsNone(results[0]['previous_game'])
        self.assertEqual(results[1]['previous_game']['loser'], '乙')
        self.assertEqual(results[2]['previous_game']['loser'], '甲')
        self.assertNotEqual(results[0]['raw'], results[2]['raw'])

    def test_missing_middle_result_is_unknown_without_shifting_other_games(self):
        self.s.handle(watch.STOC_DUEL_START, b'')
        self.start(); self.win(0)
        self.start()  # 第二小局缺胜负消息
        self.start(); self.win(1)
        paths = [self.replay(1, ['甲', '乙'], 11), self.replay(2, ['乙', '甲'], 13),
                 self.replay(3, ['甲', '乙'], 15)]
        self.assertEqual(self.evidence(paths[0])['winner'], 0)
        self.assertIsNone(self.evidence(paths[1]))
        self.assertEqual(self.evidence(paths[2])['winner'], 1)
        self.assertIsNone(self.evidence(paths[2])['previous_game'])

    def test_unanchored_midgame_result_is_not_attached_to_first_delayed_replay(self):
        self.start(); self.win(1)
        self.assertIsNone(self.evidence(self.replay(1, ['甲', '乙'], 21)))

    def test_reconnect_discards_old_timeline_before_receiving_new_stream(self):
        import asyncio
        self.s.handle(watch.STOC_DUEL_START, b'')
        self.start(); self.win(0)
        self.assertEqual(len(self.s._completed_games), 1)
        with patch.object(watch.asyncio, 'open_connection', new=AsyncMock(side_effect=OSError('offline'))):
            self.assertFalse(asyncio.run(self.s._connect('test.invalid', 8911, 'test', 'test')))
        self.assertFalse(self.s._capture_anchor)
        self.assertEqual(self.s._completed_games, {})

    def test_conflicting_results_for_same_game_stay_unknown(self):
        self.s.handle(watch.STOC_DUEL_START, b'')
        self.start(); self.win(0); self.win(1); self.win(1)
        self.assertIsNone(self.evidence(self.replay(1, ['甲', '乙'], 31)))

    def test_nonobserver_start_invalidates_timeline_anchor(self):
        self.s.handle(watch.STOC_DUEL_START, b'')
        self.s.handle(watch.STOC_GAME_MSG, bytes([4, 0]) + bytes(17))
        self.win(0)
        self.assertIsNone(self.evidence(self.replay(1, ['甲', '乙'], 41)))

    def test_final_win_after_all_replays_updates_result_without_losing_journal(self):
        self.s.handle(watch.STOC_DUEL_START, b'')
        self.start(); self.win(0)
        self.start(); self.win(0)
        self.start()
        paths = [self.replay(1, ['甲', '乙'], 51), self.replay(2, ['乙', '甲'], 53),
                 self.replay(3, ['甲', '乙'], 55)]
        self.assertIsNone(self.evidence(paths[2]))
        stamp = paths[2].stat().st_mtime_ns
        cache = Path(str(paths[2]) + '.analysis.json')
        cache.write_text('{}')
        self.win(1)
        result = self.evidence(paths[2])
        self.assertEqual(result['winner'], 1)
        self.assertEqual(result['game_no'], 3)
        self.assertTrue(result['captured_from_start'])
        self.assertEqual(result['events'], ['2800', '050100'])
        self.assertEqual(result['previous_game']['loser'], '甲')
        self.assertEqual(paths[2].stat().st_mtime_ns, stamp, '补证据不得续期')
        self.assertFalse(cache.exists(), '晚到结果必须使旧解析失效')

    def test_late_conflict_revokes_saved_evidence_and_next_chooser(self):
        self.s.handle(watch.STOC_DUEL_START, b'')
        self.start(); self.win(0)
        path = self.replay(1, ['甲', '乙'], 61)
        self.assertIsNotNone(self.evidence(path))
        self.win(1)
        self.assertIsNone(self.evidence(path))
        self.start(); self.win(0)
        self.assertIsNone(self.evidence(self.replay(2, ['乙', '甲'], 63))['previous_game'])

    def test_duplicate_delivery_before_late_win_keeps_original_path(self):
        self.s.handle(watch.STOC_DUEL_START, b'')
        self.start()
        path = self.replay(1, ['甲', '乙'], 71)
        self.replay(1, ['甲', '乙'], 71)
        self.win(0)
        self.assertEqual(self.evidence(path)['winner'], 0)

    def test_late_result_updates_every_player_copy_and_does_not_recreate_deleted_replay(self):
        self.s.replay_writer.targets = ['甲', '乙']
        self.s.handle(watch.STOC_DUEL_START, b'')
        self.start()
        self.replay(1, ['甲', '乙'], 81)
        paths = list(self.root.rglob('*.yrp'))
        self.assertEqual(len(paths), 2)
        self.win(0)
        self.assertTrue(all(self.evidence(path)['winner'] == 0 for path in paths))
        paths[0].unlink()
        Path(str(paths[0]) + '.evidence.json').unlink()
        self.win(0)
        self.assertFalse(paths[0].exists())
        self.assertFalse(Path(str(paths[0]) + '.evidence.json').exists())
