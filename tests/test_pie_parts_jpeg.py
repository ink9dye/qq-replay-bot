import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from bot import statistics_pie_image as spi


class PiePartsTests(unittest.TestCase):
    """机器人发的两张拆分图走 JPEG：体积约为 PNG 的 1/5，旧 PNG 仍可读。"""

    def _source(self):
        image = Image.new('RGB', (1600, 2000), 'white')
        for step in range(0, 600, 20):
            image.paste((40 + step % 200, 90, 120), (120 + step, 120 + step, 420 + step, 320 + step))
        for y in range(1100, 1900, 18):
            image.paste((40, 50, 60), (80, y, 1400, y + 9))
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return buffer.getvalue()

    def test_parts_are_jpeg(self):
        parts = spi._parts(self._source())
        self.assertEqual(set(parts), {'pie', 'legend'})
        for name, blob in parts.items():
            self.assertTrue(blob.startswith(b'\xff\xd8\xff'), name)
            self.assertLess(len(blob), 1_000_000, name)

    def test_load_reads_new_jpeg_and_legacy_png(self):
        parts = spi._parts(self._source())
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / 'bot.sqlite3')
            root = Path(tmp) / 'cache' / 'deck-pie'
            root.mkdir(parents=True)
            (root / 'latest-pie.jpg').write_bytes(parts['pie'])
            (root / 'latest-legend.jpg').write_bytes(parts['legend'])
            loaded = spi._load_parts(database)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded['pie'], parts['pie'])

            for name in ('latest-pie.jpg', 'latest-legend.jpg'):
                (root / name).unlink()
            (root / 'latest-pie.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'legacy pie')
            (root / 'latest-legend.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'legacy legend')
            loaded = spi._load_parts(database)
            self.assertIsNotNone(loaded)
            self.assertTrue(loaded['legend'].endswith(b'legacy legend'))

    def test_stale_parts_are_ignored(self):
        parts = spi._parts(self._source())
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / 'bot.sqlite3')
            root = Path(tmp) / 'cache' / 'deck-pie'
            root.mkdir(parents=True)
            old = time.time() - 700
            for name, key in (('latest-pie.jpg', 'pie'), ('latest-legend.jpg', 'legend')):
                path = root / name
                path.write_bytes(parts[key])
                os.utime(path, (old, old))
            self.assertIsNone(spi._load_parts(database))
            self.assertIsNotNone(spi._load_parts(database, max_age=1200))


if __name__ == '__main__':
    unittest.main()
