import asyncio
import json
import sqlite3
import threading
import unittest
from unittest.mock import AsyncMock
from bot.statistics_match_identity import capture_metadata
from bot.statistics_population import latest_views
from bot.service import BotService


class UploadedMatchTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE stat_evidence(digest,payload_json); CREATE TABLE stat_files(digest,replay_id); CREATE TABLE replay_sessions(replay_id,session_id,game_no);')
        for i in (1, 2, 3):
            self.db.execute('INSERT INTO stat_files VALUES(?,?)', (str(i), i))
            self.db.execute('INSERT INTO replay_sessions VALUES(?,?,?)', (i, 'uploaded-match', i))
            self.db.execute('INSERT INTO stat_evidence VALUES(?,?)', (str(i), '{}'))

    def test_upload_first_game_supplies_both_main_and_extra_build(self):
        meta = capture_metadata(self.db, ['1', '2', '3'])
        rows = [dict(player='p', digest=str(i), build='build'+str(i), recorded_at=i,
                     observed_session_id=meta[str(i)][0], observed_game_no=meta[str(i)][1]) for i in (1, 2, 3)]
        self.assertEqual(latest_views({'views': rows})['p']['build'], 'build1')

    def test_explicit_invalid_capture_blocks_legacy_repair(self):
        self.db.execute('INSERT INTO stat_evidence VALUES(?,?)', ('1', json.dumps({'capture': {'game_no': None}})))
        self.assertNotIn('1', capture_metadata(self.db, ['1']))

    def test_conflicting_legacy_and_explicit_evidence_remains_unknown(self):
        self.db.execute('INSERT INTO replay_sessions VALUES(?,?,?)', (1, 'other', 2))
        self.assertNotIn('1', capture_metadata(self.db, ['1']))
        for no in (1, 2):
            self.db.execute('INSERT INTO stat_evidence VALUES(?,?)', ('2', json.dumps({'capture': {'game_no': no, 'session_id': 'x', 'captured_from_start': True}})))
        self.assertNotIn('2', capture_metadata(self.db, ['2']))

    def test_explicit_capture_has_priority_and_reads_fresh_changes(self):
        self.db.execute('INSERT INTO stat_evidence VALUES(?,?)', ('1', json.dumps({'capture': {'game_no': 2, 'session_id': 'real', 'captured_from_start': True}})))
        self.assertEqual(capture_metadata(self.db, ['1'])['1'], ('real', 2, True))
        self.db.execute('DELETE FROM stat_evidence WHERE digest=?', ('1',))
        self.assertEqual(capture_metadata(self.db, ['1'])['1'], ('uploaded-match', 1, True))


class StatisticsQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_command_waits_and_both_results_are_delivered(self):
        service = object.__new__(BotService)
        service._statistics_command_lock = asyncio.Lock()
        service.write_log = lambda text: None
        started = threading.Event()
        release = threading.Event()
        calls = []
        def handle(user, text, conversation):
            calls.append(text)
            if len(calls) == 1:
                started.set()
                release.wait(3)
            return text + ':done'
        service.handle_text = handle
        send = AsyncMock()
        first = asyncio.create_task(service._deck_build_job('1', '构筑 a', '', 'g', send))
        await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(service._slow_text_job('2', '前百构筑 雷盟', '', 'g', send))
        await asyncio.sleep(.03)
        self.assertEqual(calls, ['构筑 a'])
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual([call.args[1] for call in send.await_args_list], ['构筑 a:done', '前百构筑 雷盟:done'])

class LatestMatchIsolationTests(unittest.TestCase):
    def test_reused_session_different_opponent_cannot_borrow_first_game(self):
        rows = [dict(player='p', digest='a', build='old-first', recorded_at=1, observed_session_id='s', observed_game_no=1, observed_match_players=('p','old')),
                dict(player='p', digest='b', build='new-third', recorded_at=3, observed_session_id='s', observed_game_no=3, observed_match_players=('p','new'))]
        self.assertEqual(latest_views({'views': rows})['p']['build'], 'new-third')


class Top100CacheTests(unittest.TestCase):
    def test_repeated_keywords_share_work_and_revision_invalidates(self):
        from test_bot import temporary_service
        from unittest.mock import patch
        with temporary_service() as service:
            with patch('bot.deck_naming.latest_views', return_value=[]) as compute:
                service._top100_latest_views()
                service._top100_latest_views()
                self.assertEqual(compute.call_count, 1)
                service.store._db.execute('UPDATE stat_classification_state SET revision=revision+1 WHERE singleton=1')
                service.store._db.commit()
                service._top100_latest_views()
                self.assertEqual(compute.call_count, 2)


class PrivateDeckSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_number_selection_uses_the_same_background_deck_queue(self):
        service=object.__new__(BotService)
        service._statistics_command_lock=None
        service._top100_deck_candidates={'10001':dict(players={91:'p'},time=100)}
        service.clock=lambda:100
        service._deck_tasks=set()
        service.write_log=lambda text:None
        calls=[]
        service.handle_text=lambda user,text,conversation:calls.append((user,text,conversation)) or 'fixture-result'
        service.qq=type('QQ',(),{})()
        service.qq.send_private=AsyncMock(return_value=True)
        service.qq.send_group=AsyncMock(return_value=True)
        service.qq.send_private_forward=AsyncMock(return_value=True)
        service.qq.send_group_forward=AsyncMock(return_value=True)
        await service.on_qq_event(dict(message_type='private',user_id=10001,message='91',raw_message='91'))
        self.assertEqual(len(service._deck_tasks),1)
        await asyncio.gather(*tuple(service._deck_tasks))
        self.assertEqual(len(calls),1)
        self.assertEqual(service.qq.send_private.await_count,2)
        self.assertEqual(service.qq.send_private.await_args_list[-1].args[1],'fixture-result')
