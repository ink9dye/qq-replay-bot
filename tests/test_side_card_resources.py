"""Regression checks using temporary SQLite databases and event-controlled fetches."""
import gc
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from bot import side_cache
from bot.card_search import CardSearch


def rows(prefix='card', count=8):
    return {'result': [dict(id=i+1, cn_name=prefix+str(i)) for i in range(count)], 'next': 0}


class SideConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.connections = []
        original = sqlite3.connect

        def tracked(*args, **kwargs):
            db = original(*args, **kwargs)
            self.connections.append(db)
            return db
        self.patcher = patch.object(side_cache.sqlite3, 'connect', side_effect=tracked)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def assert_closed(self):
        for db in self.connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_repeated_reads_count_and_commit_close_without_gc(self):
        enabled = gc.isenabled()
        gc.disable()
        try:
            self.assertEqual(side_cache.put(self.root, 'a', {'cards': [1]})['inserted'], 1)
            for _ in range(20):
                self.assertEqual(side_cache.get(self.root, 'a'), {'cards': [1]})
                self.assertEqual(len(side_cache.load(self.root)), 1)
            self.assertEqual(side_cache.commit(self.root, {})['status'], 'nochange')
            self.assert_closed()
        finally:
            if enabled:
                gc.enable()

    def test_failed_transaction_rolls_back_and_closes(self):
        side_cache.put(self.root, 'a', [1])
        with side_cache.connect(self.root) as db:
            db.execute("CREATE TRIGGER reject_bad BEFORE INSERT ON entries WHEN NEW.build='bad' BEGIN SELECT RAISE(ABORT, 'rejected'); END")
        db.close()
        self.assertEqual(side_cache.commit(self.root, {'new': [2], 'bad': [3]})['status'], 'error')
        self.assertIsNone(side_cache.get(self.root, 'new'))
        self.assertEqual(side_cache.get(self.root, 'a'), [1])
        self.assert_closed()

    def test_initialization_failure_closes(self):
        with patch.object(side_cache, 'SCHEMA', 'INVALID SQL'):
            self.assertIsNone(side_cache.get(self.root, 'x'))
            self.assertEqual(len(side_cache.load(self.root)), 0)
            self.assertEqual(side_cache.put(self.root, 'x', [])['status'], 'error')
        self.assert_closed()

    def test_invalid_json_read_closes(self):
        db = side_cache.connect(self.root)
        db.execute("INSERT INTO entries VALUES('bad','{',0)")
        db.commit()
        db.close()
        self.assertIsNone(side_cache.get(self.root, 'bad'))
        self.assert_closed()

    def test_concurrent_commits_keep_existing_keys(self):
        side_cache.put(self.root, 'old', [0])
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda i: side_cache.commit(self.root, {'old': [9], str(i): [i]}), range(4)))
        self.assertTrue(all(r['status'] == 'ok' for r in results))
        self.assertEqual(side_cache.get(self.root, 'old'), [0])
        self.assertEqual(len(side_cache.load(self.root)), 5)
        self.assert_closed()


class SearchConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.search = CardSearch(Path(self.tmp.name), fetch=lambda _: rows(), clock=lambda: 100)
        self.image_patch = patch('bot.card_search.card_image_payload', return_value='fixture')
        self.image_patch.start()
        self.addCleanup(self.image_patch.stop)
        self.search.handle('b', 'search', 'initial', purpose='profile')

    def exercise_wait(self, operation, fetch_attribute):
        entered, release = threading.Event(), threading.Event()
        original = getattr(self.search, fetch_attribute)

        def slow(*args):
            entered.set()
            if not release.wait(4):
                raise AssertionError('fetch never released')
            return original(*args)

        setattr(self.search, fetch_attribute, slow)
        with ThreadPoolExecutor(3) as pool:
            first = pool.submit(operation)
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(pool.submit(self.search.has_candidates, 'b').result(timeout=.5))
                selected = pool.submit(self.search.handle, 'b', 'next').result(timeout=.5)
                self.assertIn('6. card5', selected)
            finally:
                release.set()
            first.result(timeout=2)
        self.assertEqual(self.search._operation_locks, {})

    def test_search_wait_does_not_block_candidates_or_paging(self):
        self.exercise_wait(lambda: self.search.handle('a', 'search', 'other'), 'fetch')

    def test_different_user_search_completes_while_first_fetch_waits(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(path):
            if 'slow' in path:
                entered.set()
                self.assertTrue(release.wait(4))
                return rows('slow')
            return rows('fast')
        self.search.fetch = fetch
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.search.handle, 'a', 'search', 'slow')
            try:
                self.assertTrue(entered.wait(1))
                reply = pool.submit(self.search.handle, 'b', 'search', 'fast').result(timeout=.5)
                self.assertIn('fast0', reply)
                self.assertNotIn('slow0', reply)
            finally:
                release.set()
            self.assertIn('slow0', first.result(timeout=2))
        self.assertEqual(self.search.sessions['b']['rows'][0]['cn_name'], 'fast0')
        self.assertEqual(self.search._operation_locks, {})

    def test_release_wait_does_not_block_candidates_or_paging(self):
        self.search.release_fetch = lambda _: '<i>OCG</i><span class="pack">AB-001 (2026-01-01)</span>'
        self.exercise_wait(lambda: self.search._reply_detail(dict(id=123)), 'release_fetch')

    def test_image_wait_does_not_block_candidates_or_paging(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            self.assertTrue(release.wait(4))
            return 'fixture'
        with patch('bot.card_search.card_image_payload', side_effect=slow), ThreadPoolExecutor(2) as pool:
            self.search.handle('a', 'search', 'initial')
            first = pool.submit(self.search.handle, 'a', 'select', '1')
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(pool.submit(self.search.has_candidates, 'b').result(timeout=.5))
            finally:
                release.set()
            first.result(timeout=2)

    def test_same_user_operations_serialize_and_keep_purpose(self):
        entered, release, selecting = threading.Event(), threading.Event(), threading.Event()
        def slow(_):
            entered.set()
            self.assertTrue(release.wait(4))
            return rows('new')
        self.search.fetch = slow
        def select():
            selecting.set()
            return self.search.handle('a', 'select', '1')
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.search.handle, 'a', 'search', 'new', 'profile')
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(select)
                self.assertTrue(selecting.wait(1))
                self.assertFalse(second.done())
            finally:
                release.set()
            first.result(timeout=2)
            reply = second.result(timeout=2)
        self.assertEqual(reply.selected_card['cn_name'], 'new0')
        self.assertEqual(reply.purpose, 'profile')
        self.assertEqual(self.search._operation_locks, {})

    def test_same_search_and_release_requests_merge(self):
        for kind in ('request', 'release'):
            entered, release, started = threading.Event(), threading.Event(), threading.Event()
            calls = []
            def slow(_):
                calls.append(1)
                entered.set()
                self.assertTrue(release.wait(4))
                return rows() if kind == 'request' else '<i>OCG</i><span class="pack">AB-001 (2026-01-01)</span>'
            if kind == 'request':
                self.search.fetch = slow
                operation = lambda: self.search._request('?shared')
            else:
                self.search.release_fetch = slow
                operation = lambda: self.search._reply_detail(dict(id=999))
            def follower():
                started.set()
                return operation()
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(operation)
                try:
                    self.assertTrue(entered.wait(1))
                    second = pool.submit(follower)
                    self.assertTrue(started.wait(1))
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=2), second.result(timeout=2))
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.search._operation_locks, {})

    def test_error_releases_locks_and_fallback_isolated(self):
        def fail(_):
            raise OSError('offline')
        self.search.fetch = fail
        with patch('bot.card_search.time.sleep'), patch('bot.statistics_local_cards.local_index', return_value={}):
            self.assertIn('没有找到', self.search.handle('a', 'search', 'missing'))
        self.assertTrue(self.search.has_candidates('b'))
        self.assertEqual(self.search._operation_locks, {})


if __name__ == '__main__':
    unittest.main()
