import asyncio
import io
import json
import time
import unittest
from unittest.mock import patch, AsyncMock
from urllib.error import HTTPError, URLError

from bot import top100
from bot import watch_core as watch
from test_top100 import roster


class RankingResilienceTests(unittest.TestCase):
    def test_selection_reason_identifies_roster_version_or_explicit_attention(self):
        self.addCleanup(watch.set_watch_target_reasons,{})
        watch.set_watch_target_reasons({'甲':'前100第12名，榜单v9／22:00:00','乙':'独立关注名单'})
        room=watch.RoomInfo(key='test',title='test',join_id='test',players=['甲','乙'],started=True)
        reason=watch.selection_reason(room)
        self.assertIn('榜单v9',reason);self.assertIn('乙：独立关注名单',reason)

    def test_transient_timeout_retries_once_then_returns_valid_full_roster(self):
        body=io.BytesIO(json.dumps(roster()).encode())
        with patch.object(top100,'urlopen',side_effect=[TimeoutError(),body]) as fetch, patch.object(top100.time,'sleep'):
            self.assertEqual(len(top100.fetch()),100)
        self.assertEqual(fetch.call_count,2)

    def test_permanent_http_and_invalid_roster_are_not_retried(self):
        for value in (HTTPError('https://example.test',403,'denied',{},None),io.BytesIO(b'[]')):
            with self.subTest(value=value), patch.object(top100,'urlopen',side_effect=value if isinstance(value,Exception) else None,return_value=value) as fetch:
                with self.assertRaises((OSError,ValueError)):top100.fetch()
                self.assertEqual(fetch.call_count,1)

    def test_retries_are_bounded_and_error_labels_do_not_expose_exception_text(self):
        with patch.object(top100,'urlopen',side_effect=TimeoutError('private proxy credential')) as fetch,patch.object(top100.time,'sleep'):
            with self.assertRaises(TimeoutError):top100.fetch()
            self.assertEqual(fetch.call_count,2)
        self.assertEqual(top100.error_category(URLError(TimeoutError('secret'))),'timeout')

    def test_official_rank_wins_over_profile_cache_and_stale_is_explicit(self):
        self.addCleanup(watch.set_official_rank_snapshot,[],None,True)
        with patch.dict(watch._rank_cache,{'甲':999},clear=True),patch.object(watch.urllib.request,'urlopen',side_effect=AssertionError('must not request known player')):
            watch.set_official_rank_snapshot([{'player':'甲','rank':12}],time.time(),False)
            self.assertEqual(watch.arena_rank_number('甲'),12)
            room=watch.RoomInfo(key='test',title='test',join_id='test',players=['甲'],started=True)
            self.assertIn('第12名',watch.room_label_with_ranks(room))
            self.assertNotIn('过期',watch.room_label_with_ranks(room))
            watch.set_official_rank_snapshot([{'player':'甲','rank':12}],time.time(),True)
            self.assertIn('榜单过期',watch.room_label_with_ranks(room))
            watch.set_official_rank_snapshot([],time.time(),False)
            self.assertEqual(watch.arena_rank_number('甲'),999)


class PushRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_then_reconnection_is_announced(self):
        stop=asyncio.Event()
        client=AsyncMock()
        client.connect.side_effect=[TimeoutError(),None]
        async def received():
            stop.set()
            return '{}'
        client.recv_text.side_effect=received
        async def wait(awaitable,timeout):
            if timeout==20:
                awaitable.close()
                raise asyncio.TimeoutError
            return await awaitable
        with patch.object(watch,'WebSocketClient',return_value=client),patch.object(watch.asyncio,'wait_for',side_effect=wait),patch.object(watch,'log') as log:
            await watch.websocket_loop('wss://example.test/',AsyncMock(),stop)
        messages=[call.args[0] for call in log.call_args_list]
        self.assertTrue(any('暂时不可用' in m for m in messages))
        self.assertTrue(any('已连接房间推送' in m for m in messages))
