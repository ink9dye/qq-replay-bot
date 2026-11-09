# -*- coding: utf-8 -*-
"""饼图代表卡缺本地图时：先同步取一次，失败再退回后台补图。"""
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import card_image_cache, statistics_local_cards
from bot import top100_pie


class PieCardArtFallbackTests(unittest.TestCase):
    def test_missing_local_art_is_fetched_before_drawing(self):
        # 随便取一个本地库里不会有的卡号，确保走到 ensure_full 分支。
        with patch.object(card_image_cache, 'ensure_full', return_value=Path('fetched.jpg')) as fetch:
            self.assertEqual(top100_pie._card_file(999000111), Path('fetched.jpg'))
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[1], 999000111)

    def test_fetch_failure_falls_back_to_background_schedule(self):
        with patch.object(card_image_cache, 'ensure_full', side_effect=OSError), \
                patch.object(statistics_local_cards, 'picture', return_value=None) as queued:
            self.assertIsNone(top100_pie._card_file(999000112))
        queued.assert_called_once()


if __name__ == '__main__':
    unittest.main()
