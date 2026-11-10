import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from bot import deck_image
from bot import statistics_local_cards as cards


class ThumbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / 'ygopro'
        (self.root / 'pics').mkdir(parents=True)
        self.data = base / 'data'
        self.data.mkdir()
        (self.data / 'card-library.json').write_text(
            '{"ygopro_root": "%s"}' % self.root.as_posix(), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def _full_picture(self, code):
        Image.new('RGB', (480, 700), (200, 30, 30)).save(self.root / 'pics' / (str(code) + '.jpg'), 'JPEG')

    def _wait_idle(self, code, seconds=3):
        deadline = time.time() + seconds
        while time.time() < deadline and code in cards._thumb_pending:
            time.sleep(0.05)

    def _write_map(self, entries):
        payload = {"version": 1,
                   "cards": {str(k): {"image": v, "checked_at": 1.0} for k, v in entries.items()}}
        (self.data / 'card-image-ids.json').write_text(json.dumps(payload), encoding='utf-8')

    def test_thumb_derived_from_local_picture_without_network(self):
        self._full_picture(12345)
        with mock.patch.object(cards.urllib.request, 'urlopen', side_effect=AssertionError('offline')):
            path = cards.thumb_picture(str(self.data), 12345)
        self.assertIsNotNone(path)
        self.assertEqual(path.suffix, '.webp')
        with Image.open(path) as image:
            self.assertLessEqual(image.width, 120)
            self.assertLessEqual(image.height, 165)

    def test_missing_card_does_not_block_rendering(self):
        with mock.patch.object(cards.urllib.request, 'urlopen', side_effect=OSError('offline')):
            self.assertIsNone(cards.thumb_picture(str(self.data), 999999))
            start = time.time()
            cards.wait_for_thumbs(str(self.data), [999999], timeout=1)
            self.assertLess(time.time() - start, 1.5)
            self._wait_idle(999999)

    def test_downloaded_thumb_is_cached(self):
        buffer = io.BytesIO()
        Image.new('RGB', (82, 120), (10, 90, 160)).save(buffer, 'WEBP')
        payload = buffer.getvalue()

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(cards.urllib.request, 'urlopen', return_value=Response(payload)):
            cards.wait_for_thumbs(str(self.data), [777], timeout=2)
            self._wait_idle(777)
        path = cards.thumb_picture(str(self.data), 777)
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        self.assertLessEqual(path.stat().st_size, 4 * 1024 * 1024)

    def test_render_reports_missing_tiles(self):
        result = dict(cards=[dict(partition='main', code=1, copies=1, name='x')],
                      inferred_side=dict(cards=[]), person=dict(player='p', deck_type='d'))
        stats = {}
        blob = deck_image.render(result, lambda code: None, stats=stats)
        self.assertTrue(blob)
        self.assertEqual(stats.get('missing'), 1)

    def test_wrong_mapping_cannot_supply_another_cards_thumb(self):
        thumbs = self.data / 'card-thumbs'
        thumbs.mkdir()
        Image.new('RGB', (120, 165), (0, 120, 0)).save(thumbs / '25750986.webp', 'WEBP')
        self._write_map({100268010: 25750986})
        self.assertIsNone(cards._thumb_file(thumbs, 100268010))

    def test_super_pre_alias_mapping_still_used_last(self):
        thumbs = self.data / 'card-thumbs'
        thumbs.mkdir()
        alias = thumbs / '101307051.webp'
        Image.new('RGB', (120, 165), (200, 200, 0)).save(alias, 'WEBP')
        self._write_map({58406693: 101307051})
        self.assertEqual(cards._thumb_file(thumbs, 58406693), alias)

    def test_picture_falls_back_to_cached_full_image(self):
        folder = self.data / 'card-images'
        folder.mkdir()
        cached = folder / '777.jpg'
        Image.new('RGB', (480, 700), (10, 10, 200)).save(cached, 'JPEG')
        found = cards.picture(str(self.data), 777)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], cached)
        self.assertEqual(found[1], 'image/jpeg')

    def test_super_pre_id_downloads_from_super_pre_first(self):
        seen = []
        buffer = io.BytesIO()
        Image.new('RGB', (82, 120), (5, 5, 5)).save(buffer, 'JPEG')
        payload = buffer.getvalue()

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(url, timeout=None):
            seen.append(url)
            return Response(payload)

        with mock.patch.object(cards.urllib.request, 'urlopen', side_effect=fake_urlopen):
            cards._schedule(str(self.data), 100268010, self.root / 'pics')
            deadline = time.time() + 3
            while time.time() < deadline and 100268010 in cards._pending:
                time.sleep(0.05)
        self.assertTrue(seen)
        self.assertIn('super-pre', seen[0])

    def test_normal_card_never_asks_momobako_for_a_super_pre_alias(self):
        from bot import card_image_cache as cache
        seen = []

        def fake_urlopen(url, timeout=None):
            seen.append(url)
            raise OSError('offline')

        with mock.patch.object(cards.urllib.request, 'urlopen', side_effect=fake_urlopen), \
                mock.patch.object(cache, 'image_id', return_value=100268010), \
                mock.patch.object(cache, 'ensure_full', return_value=None):
            cards._schedule_thumb(str(self.data), 12345678, self.data / 'card-thumbs')
            deadline = time.time() + 3
            while time.time() < deadline and 12345678 in cards._thumb_pending:
                time.sleep(0.05)
        self.assertTrue(seen)
        self.assertFalse([url for url in seen if '100268010' in url or '10012345678' in url or '10112345678' in url])
        self.assertTrue(all('momobako' in url for url in seen))


if __name__ == '__main__':
    unittest.main()
