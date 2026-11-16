import unittest

from bot import statistics_card_summary as summary


class BuildBlobTests(unittest.TestCase):
    def test_roundtrip(self):
        rows = [('main', 14558127, 3), ('main', 23434538, 1), ('extra', 94145021, 2)]
        blob = summary.encode_build(rows)
        self.assertEqual(len(blob), 6 * len(rows))
        self.assertEqual(list(summary.decode_build(blob)), rows)

    def test_skips_invalid_copies_and_partition(self):
        blob = summary.encode_build([('main', 1, 0), ('main', 2, 256), ('side', 3, 1)])
        self.assertEqual(list(summary.decode_build(blob)), [('side', 3, 1)])


class ViewBlobTests(unittest.TestCase):
    def test_flags_preserve_opening_zero_null(self):
        rows = [
            ('main', 11, 1, 1, 1, 2),   # 起手 2、抽到、使用、抽后使用
            ('main', 12, 0, 0, 0, 0),   # 起手 0
            ('main', 13, None, None, None, None),  # 起手未知
            ('extra', 14, 1, 0, None, None),
        ]
        decoded = list(summary.decode_view(summary.encode_view(rows)))
        self.assertEqual(decoded[0], ('main', 11, 1, 1, 1, 1))
        self.assertEqual(decoded[1], ('main', 12, 0, 0, None, 0))
        self.assertEqual(decoded[2], ('main', 13, 0, 0, None, None))
        self.assertEqual(decoded[3][:2], ('extra', 14))
        self.assertIsNone(decoded[3][5])

    def test_decode_returns_six_bytes_per_entry(self):
        rows = [('main', code, None, None, None, 0) for code in range(1, 41)]
        self.assertEqual(len(summary.encode_view(rows)), 240)


if __name__ == '__main__':
    unittest.main()
