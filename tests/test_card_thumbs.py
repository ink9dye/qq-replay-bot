import io
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


if __name__ == '__main__':
    unittest.main()
