"""概率运算：无放回抽牌的命中概率（超几何分布），给 QQ 文本指令用。

概率运算 40 5 5 1 = 牌堆 40 张、其中目标卡 5 张、抽 5 张、至少命中 1 张。
"""
from __future__ import annotations

from math import comb

# 卡组场景远小于这个数；设上限是为了不让一条指令把进程算很久。
MAX_TOTAL = 2000


def probability_message(values):
    """返回一段可直接发到 QQ 的文本；参数不合法时抛 ValueError（消息即用户提示）。"""
    if len(values) != 4:
        raise ValueError('格式：概率运算 总数 目标数 抽取数 至少命中数，例如：概率运算 40 5 5 1。')
    total, target, draws, need = values
    if not 1 <= total <= MAX_TOTAL:
        raise ValueError(f'总数要在 1–{MAX_TOTAL} 之间。')
    if not 1 <= target <= total:
        raise ValueError('目标数要在 1 到总数之间。')
    if not 1 <= draws <= total:
        raise ValueError('抽取数要在 1 到总数之间。')
    limit = min(target, draws)
    if not 1 <= need <= limit:
        raise ValueError(f'至少命中数要在 1 到 {limit} 之间（目标数 {target}、抽取数 {draws}）。')

    # 无放回抽样：抽 n 张里命中 i 张的概率 = C(K,i)·C(N-K,n-i)/C(N,n)。
    total_ways = comb(total, draws)
    miss = sum(comb(target, hit) * comb(total - target, draws - hit) for hit in range(need))
    at_least = 1 - miss / total_ways
    expected = target * draws / total

    below = '命中 0 张' if need == 1 else f'命中 0–{need - 1} 张'
    lines = [
        f'概率运算：牌堆 {total} 张，其中目标卡 {target} 张；抽 {draws} 张。',
        f'至少命中 {need} 张：{_percent(at_least)}',
        f'未达到（{below}）：{_percent(1 - at_least)}',
        f'期望命中：{expected:.3g} 张',
        '口径：无放回抽取（超几何分布），不考虑起手换牌等额外规则。',
    ]
    return '\n'.join(lines)


def _percent(value):
    pct = value * 100
    if pct <= 0:
        return '0%'
    # 极小概率不要显示成 0.00%，避免和“不可能”混淆。
    if pct < 0.01:
        return f'{pct:.4f}%'
    return f'{pct:.2f}%'
