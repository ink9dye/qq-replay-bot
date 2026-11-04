import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from bot import card_search as search
from bot import statistics_local_cards as local
from bot.statistics_recording_filters import snapshot


class ImageConcurrencyTests(unittest.TestCase):
    def setUp(self):
        search._IMAGE_BYTES.clear()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)

    def test_cached_card_is_not_blocked_by_an_unrelated_download(self):
        started=threading.Event();release=threading.Event()
        def slow(code,data):started.set();release.wait(3);return b'jpeg fixture'
        search._IMAGE_BYTES[11]='cached'
        with patch.object(search,'_card_image_blob',side_effect=slow),ThreadPoolExecutor(2) as pool:
            first=pool.submit(search.card_image_payload,22,self.tmp.name)
            try:
                self.assertTrue(started.wait(1))
                fast=pool.submit(search.card_image_payload,11,self.tmp.name)
                self.assertEqual(fast.result(timeout=.5),'base64://cached')
            finally:release.set()
            self.assertTrue(first.result(timeout=1).startswith('base64://'))

    def test_same_card_requests_share_one_download(self):
        started=threading.Event();release=threading.Event();calls=[]
        def slow(code,data):calls.append(code);started.set();release.wait(3);return b'jpeg fixture'
        with patch.object(search,'_card_image_blob',side_effect=slow),ThreadPoolExecutor(2) as pool:
            first=pool.submit(search.card_image_payload,33,self.tmp.name)
            try:
                self.assertTrue(started.wait(1));second=pool.submit(search.card_image_payload,33,self.tmp.name)
            finally:release.set()
            self.assertEqual(first.result(timeout=1),second.result(timeout=1))
            self.assertEqual(calls,[33])

    def test_local_payload_does_not_probe_a_remote_url_first(self):
        with patch.object(search, 'card_image_url', side_effect=AssertionError('no HEAD probe')), \
                patch.object(search, '_card_image_blob', return_value=b'local picture'):
            self.assertTrue(search.card_image_payload(991122, self.tmp.name).startswith('base64://'))

    def test_image_cache_has_a_byte_budget(self):
        search._IMAGE_BYTES.update({1:'a'*4,2:'b'*4})
        with patch.object(search,'IMAGE_CACHE_MAX_BYTES',10),patch.object(search,'_card_image_blob',return_value=b'abcd'):
            search.card_image_payload(3,self.tmp.name)
            self.assertLessEqual(sum(map(len,search._IMAGE_BYTES.values())),10)
            self.assertIn(3,search._IMAGE_BYTES)

    def test_raw_downloader_returns_the_existing_future(self):
        started=threading.Event();release=threading.Event();calls=[]
        def get(*a,**k):calls.append(a);started.set();release.wait(3);return io.BytesIO(b'\xff\xd8'+b'x'*64)
        pics=Path(self.tmp.name)/'pics'
        with patch.object(local.urllib.request,'urlopen',side_effect=get):
            first=local._schedule(Path(self.tmp.name),55443322,pics)
            try:
                self.assertTrue(started.wait(1));second=local._schedule(Path(self.tmp.name),55443322,pics)
                self.assertIs(first,second)
            finally:release.set()
            self.assertTrue(first.result(timeout=1).is_file())
            self.assertEqual(len(calls),1)

    def test_already_downloaded_card_is_not_fetched_again(self):
        pics=Path(self.tmp.name)/'pics';pics.mkdir()
        path=pics/'55443323.jpg';path.write_bytes(b'jpeg fixture')
        with patch.object(local.urllib.request,'urlopen',side_effect=AssertionError('no download expected')):
            future=local._schedule(Path(self.tmp.name),55443323,pics)
            self.assertEqual(future.result(timeout=1),path)


class EarlyFactFilterTests(unittest.TestCase):
    def test_only_surviving_digests_and_builds_read_fact_rows(self):
        calls=[]
        class DB:
            def execute(self,sql,params=()):calls.append((sql,tuple(params)));return []
        rows=[dict(digest='old',player='p',build='old-build',recorded_at=1),dict(digest='new',player='p',build='new-build',recorded_at=100)]
        basis=dict(views=rows,roster={})
        with patch('bot.statistics_recording_filters.eligible_views',return_value=basis),patch('bot.statistics_recording_filters.reference',return_value=({},None,None)):
            result=snapshot(DB(),Path('.'),200,annotate_series=False,row_filter=lambda row:row['recorded_at']>=50)
        fact=[params for sql,params in calls if 'FROM stat_card_facts' in sql]
        builds=[params for sql,params in calls if 'FROM stat_build_cards' in sql]
        self.assertEqual(fact,[('new',)])
        self.assertEqual(builds,[('new-build',)])
        self.assertEqual(len(basis['views']),2)
        self.assertEqual([r['digest'] for r in result[1]],['new'])


class QueryCacheBudgetTests(unittest.TestCase):
    def test_web_cache_evicts_by_bytes_and_does_not_cache_oversized_responses(self):
        from bot.statistics_portal import StatisticsPortal
        from bot import statistics_portal as module
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db';path.touch()
            portal=object.__new__(StatisticsPortal)
            portal.database=path;portal._query_cache={};portal._query_cache_lock=threading.Lock()
            portal._query_reply=lambda callback:(200,'application/json',callback(None),{})
            with patch.object(module,'QUERY_CACHE_MAX_BYTES',10):
                for key in (1,2,3):portal._cached_query_reply_locked((key,),lambda db:b'12345')
                self.assertLessEqual(sum(len(v[2][2]) for v in portal._query_cache.values()),10)
                self.assertEqual(len(portal._query_cache),2)
                portal._cached_query_reply_locked(('large',),lambda db:b'x'*11)
                self.assertNotIn(('large',),portal._query_cache)
