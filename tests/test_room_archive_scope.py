import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import watch
from test_deck_links import pack_replay


class RoomArchiveScopeTests(unittest.TestCase):
    def test_removing_target_does_not_move_active_room_to_root(self):
        with tempfile.TemporaryDirectory() as tmp, patch('watch.log'), \
                patch('watch.arena_rank_number', return_value=None), patch('watch.deck_title_for', return_value='测试'):
            root = Path(tmp)
            parent = watch.ReplayWriter(root, ['甲', '乙', '路人'])
            saved = []
            parent.on_saved = lambda paths, *args: saved.extend(paths)
            active = parent.for_room(['甲', '乙'])
            active.start_match()
            watch.apply_watch_rules(parent, {}, ['丙'], [])
            active.save(pack_replay(['甲', '乙'], [([1], []), ([2], [])]), ['甲', '乙'])
            self.assertEqual({p.parent.name for p in saved}, {'甲', '乙'})
            self.assertFalse(list(root.glob('*.yrp')))
            self.assertEqual(parent.targets, ['丙'])
            self.assertEqual(parent.for_room(['丙', '丁']).targets, ['丙'])

    def test_concurrent_rooms_keep_prefixes_but_share_counts_and_digest_dedup(self):
        with tempfile.TemporaryDirectory() as tmp, patch('watch.log'), \
                patch('watch.arena_rank_number', return_value=None), patch('watch.deck_title_for', return_value='测试'):
            parent = watch.ReplayWriter(Path(tmp), ['甲'])
            first, second = parent.for_room(['甲', '乙']), parent.for_room(['甲', '丙'])
            first.start_match(); second.start_match()
            blob = pack_replay(['甲', '乙'], [([11], []), ([12], [])])
            first_paths = first.save(blob, ['甲', '乙'])
            second_paths = second.save(pack_replay(['甲', '丙'], [([13], []), ([14], [])]), ['甲', '丙'])
            self.assertEqual(watch.match_number_from_filename(first_paths[0].name), 1)
            self.assertEqual(watch.match_number_from_filename(second_paths[0].name), 2)
            self.assertEqual(second.save(blob, ['甲', '乙']), [])
            self.assertEqual(len(list(Path(tmp).rglob('*.yrp'))), 2)


class WatchLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_room_for_two_targets_survives_rule_removal_and_stops_with_serve(self):
        entered, observer_closed, feed_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        created = []
        calls = 0
        class Observer:
            def __init__(self, config, room, writer, known_start):
                created.append(writer)
            async def run(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    observer_closed.set()
        async def feed(*args):
            try:
                await asyncio.Event().wait()
            finally:
                feed_closed.set()
        def rules():
            nonlocal calls
            calls += 1
            return (['甲', '乙'] if calls == 1 else []), []
        room = watch.RoomInfo('one-room', 'one-room', 'one-room', ['甲', '乙'], True)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(watch, 'Spectator', Observer), patch.object(watch, 'log'), \
                patch.object(watch, 'candidate_sources', return_value=([], [])), \
                patch.object(watch, 'http_poll_loop', feed), \
                patch.object(watch.RoomBoard, 'snapshot', new=AsyncMock(return_value=[room])), \
                patch.object(watch.RoomBoard, 'timing', new=AsyncMock(return_value=({}, {'one-room'}))), \
                patch.object(watch, 'room_label_with_ranks', return_value='test room'):
            task = asyncio.create_task(watch.serve(dict(replay_dir=tmp, cleanup_files=False,
                host='offline.invalid', port=8911, join_mode='plain', poll_seconds=0.01),
                ['甲', '乙'], [], rules_provider=rules))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for _ in range(20):
                    if calls >= 3:
                        break
                    await asyncio.sleep(0.01)
                self.assertGreaterEqual(calls, 3)
                self.assertEqual(len(created), 1, '双目标只应共用一个房间连接')
                self.assertEqual(created[0].targets, ['甲', '乙'])
                self.assertFalse(observer_closed.is_set(), '移出名单不应截断现有录像')
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(observer_closed.is_set(), '停止serve必须关闭观战')
            self.assertTrue(feed_closed.is_set(), '停止serve必须关闭房间推送')
