"""Exact minimum-sample-ratio gate shared by card/deck pages.

Thresholds are ratios (0.01 = 1%), compared as exact fractions so the
boundary case n/N == threshold qualifies and display rounding never decides.
"""
from fractions import Fraction

DEFAULT_CARD_RATIO = Fraction(1, 100)   # 卡片统计、卡组内部单卡、常见对策牌
DEFAULT_MATCHUP_RATIO = Fraction(10, 100)  # 矩阵点击格子的对局内排行（P09 预留）


def ratio(value):
    """Parse a ratio from a number, '1%', '0.5%' or '0.01'."""
    if isinstance(value, Fraction):
        pass
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith('%'):
            value = Fraction(text[:-1].strip()) / 100
        else:
            value = Fraction(text)
    else:
        value = Fraction(str(value))
    if value < 0:
        raise ValueError('negative threshold')
    return value


def effective_minimum(denominator, threshold=DEFAULT_CARD_RATIO):
    """ceil(N x threshold); 0 when the threshold is zero."""
    t = ratio(threshold)
    if denominator <= 0 or t <= 0:
        return 0
    n = denominator * t
    return -((-n.numerator) // n.denominator)


def summary(numerator, denominator, threshold=DEFAULT_CARD_RATIO):
    """n/N gate payload. N=0 never yields a ratio or a pass."""
    t = ratio(threshold)
    minimum = effective_minimum(denominator, t)
    passes = denominator > 0 and numerator > 0 and Fraction(numerator, denominator) >= t
    return dict(numerator=numerator, denominator=denominator,
                ratio=float(Fraction(numerator, denominator)) if denominator > 0 else None,
                threshold=float(t), minimum=minimum, passes=passes)


def qualifies(numerator, denominator, threshold=DEFAULT_CARD_RATIO):
    try:
        return summary(numerator, denominator, threshold)['passes']
    except (ValueError, ZeroDivisionError, TypeError):
        return False
