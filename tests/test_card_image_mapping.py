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
        cache._hits.clear()
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


class NewCdnFallbackTests(unittest.TestCase):
    """2026-10 起 momobako 渐进上新路径 ygoimg：新卡老路径全 404。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        cache._maps.clear()
        cache._stamps.clear()
        cache._hits.clear()
        self.tmp.cleanup()

    def test_parse_image_id_reads_ygoimg_webp(self):
        page = ('<img src="https://cdn.233.momobako.com/ygoimg/ygopro/31321471.webp'
                '!/format/webp/fw/400/quality/85">')
        self.assertEqual(cache.parse_image_id(page), 31321471)

    def test_ygoimg_image_id_equal_to_card_is_not_mapped(self):
        # 图名==卡密的普通卡不需要映射，也不许写映射文件。
        page = '<a href="/card/31321471">x</a><img src="/ygoimg/ygopro/31321471.webp">'
        self.assertIsNone(cache.remember_page(self.data, 31321471, page))
        self.assertEqual(cache.load_map(self.data), {})

    def test_page_urls_fall_back_to_ygoimg_for_normal_card(self):
        thumb, full = cache.page_urls(self.data, 12345678)
        self.assertIn('pics', thumb)
        self.assertIn('ygoimg', full)
        self.assertIn('12345678', full)

    def test_ensure_full_falls_back_to_ygoimg(self):
        jpeg = b'\xff\xd8' + b'x' * 64
        seen = []

        def fake_download(url, timeout=8):
            seen.append(url)
            if 'ygoimg' in url:
                return jpeg
            raise OSError('404')

        with mock.patch.object(cache, '_download', side_effect=fake_download), \
                mock.patch.object(cache, 'image_id', return_value=None):
            path = cache.ensure_full(self.data, 31321471)
        self.assertIsNotNone(path)
        self.assertEqual(path.name, '31321471.jpg')
        self.assertTrue(any('ygoimg' in url for url in seen))
        # 老路径 404 的新卡：命中后聊天兜底 URL 直接给已知可用的 ygoimg。
        self.assertIn('ygoimg', cache.full_url(self.data, 31321471))

    def test_full_url_keeps_super_pre_for_alias(self):
        self._write_map({58406693: 101307051})
        self.assertIn('super-pre', cache.full_url(self.data, 58406693))

    def _write_map(self, entries):
        payload = {"version": 1,
                   "cards": {str(k): {"image": v, "checked_at": 1.0} for k, v in entries.items()}}
        (self.data / cache.MAP_FILE).write_text(json.dumps(payload), encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
