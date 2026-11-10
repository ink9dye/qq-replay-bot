"""卡密 -> 图名 映射的校验：错映射不读、不写、不自愈成别人的卡图。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot import card_image_cache as cache


class PageUrlTests(unittest.TestCase):
    def test_same_card_page(self):
        self.assertTrue(cache._same_card_page('https://ygocdb.com/card/12345', 12345))
        self.assertTrue(cache._same_card_page('https://ygocdb.com/card/12345/', 12345))
        self.assertTrue(cache._same_card_page('https://ygocdb.com/card/12345?from=search', 12345))
        self.assertFalse(cache._same_card_page('https://ygocdb.com/card/67890', 12345))
        self.assertFalse(cache._same_card_page('', 12345))


class MappingGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        cache._maps.clear()
        cache._stamps.clear()
        self.tmp.cleanup()

    def _write_map(self, entries):
        payload = {"version": 1,
                   "cards": {str(k): {"image": v, "checked_at": 1.0} for k, v in entries.items()}}
        (self.data / cache.MAP_FILE).write_text(json.dumps(payload), encoding='utf-8')

    def test_poisoned_entry_is_ignored_on_load(self):
        # 100268010（沉默斗者）曾被写成 25750986（永恒混沌）的图名。
        self._write_map({100268010: 25750986, 58406693: 101307051})
        mapping = cache.load_map(self.data)
        self.assertNotIn(100268010, mapping)
        self.assertEqual(mapping[58406693][0], 101307051)

    def test_super_pre_card_never_reads_mapping(self):
        self._write_map({100268010: 101268010})
        self.assertIsNone(cache.image_id_cached(self.data, 100268010))

    def test_remember_rejects_another_cards_picture(self):
        self.assertFalse(cache.remember(self.data, 100268010, 25750986))
        self.assertFalse(cache.remember(self.data, 12345678, 87654321))
        self.assertFalse(cache.remember(self.data, 58406693, 58406693))
        self.assertFalse((self.data / cache.MAP_FILE).exists())

    def test_remember_keeps_super_pre_alias(self):
        self.assertTrue(cache.remember(self.data, 58406693, 101307051))
        self.assertEqual(cache.load_map(self.data)[58406693][0], 101307051)

    def test_remember_page_needs_the_cards_own_page(self):
        other = '<img src="https://cdn.233.momobako.com/ygopro/pics/25750986.jpg">'
        self.assertIsNone(cache.remember_page(self.data, 100268010, other))
        # 页面确实是本卡，但超先行卡号不需要映射
        own = '<a href="/card/100268010">本卡</a><img src="/pics/101268010.jpg">'
        self.assertIsNone(cache.remember_page(self.data, 100268010, own))
        self.assertEqual(cache.load_map(self.data), {})

    def test_remember_page_accepts_verified_alias(self):
        page = '<link rel="canonical" href="/card/58406693"><img src="/pics/101307051.jpg">'
        self.assertEqual(cache.remember_page(self.data, 58406693, page), 101307051)
        self.assertEqual(cache.load_map(self.data)[58406693][0], 101307051)

    def test_fetch_redirect_is_not_recorded(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def read(self, *_args):
                return self.body

            def geturl(self):
                return 'https://ygocdb.com/card/999'

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        page = '<a href="/card/12345">x</a><img src="/pics/101234567.jpg">'
        with mock.patch.object(cache._opener, 'open', return_value=Response(page.encode())):
            self.assertIsNone(cache.image_id(self.data, 12345))
        self.assertEqual(cache.load_map(self.data), {})


    def test_page_urls_use_super_pre_for_super_pre_alias(self):
        # 99522650（沉默斗者）的图在先行图号 100268010；momobako 同号是另一张卡。
        self._write_map({99522650: 100268010})
        thumb, full = cache.page_urls(self.data, 99522650)
        self.assertIn('super-pre', thumb)
        self.assertIn('super-pre', full)


if __name__ == '__main__':
    unittest.main()
