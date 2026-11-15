# -*- coding: utf-8 -*-
"""代表卡：等价类 canonical 没有本地图时，改用组内有图的最小编号。"""
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import deck_pie, series_naming


class RepresentativeImageTests(unittest.TestCase):
    def _select(self, members, groups, available):
        registry = dict(members={'无垢大艺术': members},
                        groups={'无垢大艺术': groups})
        with patch.object(deck_pie, '_stamps', return_value=(1,)), \
                patch.object(series_naming, 'load_config', return_value={}), \
                patch.object(series_naming, 'reference_catalog', return_value={}), \
                patch.object(series_naming, 'build_registry', return_value=registry), \
                patch.object(deck_pie, '_has_image',
                             side_effect=lambda data_dir, code: code in available):
            deck_pie._FIRST_CACHE.clear()
            return deck_pie.first_cards(Path('data'))['无垢大艺术']

    def test_missing_canonical_falls_back_to_image_member(self):
        self.assertEqual(
            self._select([23133136, 36270527], {'representative': None}, {36270527}),
            36270527)

    def test_configured_representative_wins_when_it_has_image(self):
        self.assertEqual(
            self._select([23133136, 36270527], {'representative': 23133136}, {23133136}),
            23133136)

    def test_no_image_keeps_original_min(self):
        self.assertEqual(
            self._select([23133136, 36270527], {'representative': None}, set()),
            23133136)


if __name__ == '__main__':
    unittest.main()
