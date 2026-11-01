"""Tests for skipping preexisting rooms and clearing them on fresh starts."""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(r'D:\pythons\qq-replay-bot')
sys.path.insert(0, str(ROOT))
import watch  # noqa: E402


class Room:
    def __init__(self, key):
        self.key = key


class JoinableTests(unittest.TestCase):
    def test_preexisting_skipped_by_default(self):
        rooms = [Room('old'), Room('new')]
        result = watch.joinable_rooms(rooms, {'old'}, set())
        self.assertEqual([room.key for room in result], ['new'])

    def test_direct_room_and_allow_flag_keep_preexisting(self):
        rooms = [Room('old'), Room('new')]
        self.assertEqual([r.key for r in watch.joinable_rooms(rooms, {'old'}, {'old'})], ['old', 'new'])
        self.assertEqual([r.key for r in watch.joinable_rooms(rooms, {'old'}, set(), True)], ['old', 'new'])


class BoardTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_marks_preexisting_and_create_clears_it(self):
        board = watch.RoomBoard()
        room = {'roomname': 'r1', 'title': 'r1', 'istart': 'start',
                'users': [{'name': 'a', 'pos': 0}, {'name': 'b', 'pos': 1}]}
        await board.apply_ws('wss://example/?filter=started', 'init', [room])
        starts, preexisting = await board.timing()
        self.assertIn('r1', preexisting)
        await board.apply_ws('wss://example/?filter=started', 'create', room)
        starts, preexisting = await board.timing()
        self.assertNotIn('r1', preexisting)
        self.assertIn('r1', starts)


if __name__ == '__main__':
    unittest.main()
