import unittest
from fractions import Fraction

from bot.sample_threshold import (DEFAULT_CARD_RATIO, DEFAULT_MATCHUP_RATIO, effective_minimum,
                                  qualifies, ratio, summary)


class ThresholdTests(unittest.TestCase):
    def test_parse_percent_and_fraction(self):
        self.assertEqual(ratio('1%'), Fraction(1, 100))
        self.assertEqual(ratio('0.5%'), Fraction(1, 200))
        self.assertEqual(ratio(0.01), Fraction(1, 100))
        self.assertEqual(DEFAULT_CARD_RATIO, Fraction(1, 100))
        self.assertEqual(DEFAULT_MATCHUP_RATIO, Fraction(1, 10))

    def test_boundary_is_inclusive(self):
        self.assertTrue(qualifies(1, 100, '1%'))
        self.assertFalse(qualifies(1, 101, '1%'))
        self.assertTrue(qualifies(10, 1000, '1%'))

    def test_effective_minimum_uses_ceiling(self):
        self.assertEqual(effective_minimum(988, '1%'), 10)
        self.assertEqual(effective_minimum(1000, '1%'), 10)
        self.assertEqual(effective_minimum(999, '1%'), 10)
        self.assertEqual(effective_minimum(988, '0.5%'), 5)
        self.assertEqual(effective_minimum(988, '10%'), 99)

    def test_zero_denominator_never_passes_or_reports_ratio(self):
        self.assertFalse(qualifies(0, 0, '1%'))
        self.assertFalse(qualifies(5, 0, '1%'))
        item = summary(0, 0, '1%')
        self.assertIsNone(item['ratio'])
        self.assertFalse(item['passes'])
        self.assertEqual(item['minimum'], 0)

    def test_zero_threshold_still_needs_one_sample(self):
        self.assertFalse(qualifies(0, 500, '0%'))
        self.assertTrue(qualifies(1, 500, '0%'))
        self.assertEqual(effective_minimum(500, '0%'), 0)


    def test_decimal_percent_is_not_rounded(self):
        self.assertFalse(qualifies(1, 8000, '0.014%'))
        self.assertTrue(qualifies(2, 8000, '0.014%'))
        self.assertFalse(qualifies(1, 8000, '0.013%'))
        self.assertTrue(qualifies(1, 8000, '0.012%'))
        self.assertEqual(effective_minimum(8000, '0.014%'), 2)
        self.assertEqual(ratio('0.014%'), Fraction(14, 100000))

    def test_invalid_or_extreme_inputs(self):
        for bad in ('', '-1', 'abc', '1e2', 'Infinity', 'NaN'):
            self.assertFalse(qualifies(5, 100, bad))
        self.assertFalse(qualifies(0, 100, '0%'))
        self.assertTrue(qualifies(1, 100, '0%'))

    def test_summary_fields(self):
        item = summary(12, 988, '1%')
        self.assertEqual((item['numerator'], item['denominator'], item['minimum']), (12, 988, 10))
        self.assertAlmostEqual(item['ratio'], 12 / 988)
        self.assertTrue(item['passes'])


class ReviewerBoundaryTests(unittest.TestCase):
    def test_negative_fraction_uses_same_validation_as_text(self):
        from fractions import Fraction
        from bot.sample_threshold import qualifies, ratio
        self.assertFalse(qualifies(1,100,Fraction(-1,100)))
        with self.assertRaises(ValueError):
            ratio(Fraction(-1,100))
