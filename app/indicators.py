from __future__ import annotations

from typing import List, Optional, Sequence

FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    """指数移动平均，前 period-1 个值为 None，以 SMA 作为种子。"""
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def funding_boundaries(ts_from: int, ts_to: int) -> int:
    """统计 (ts_from, ts_to] 区间内的 8 小时资金费结算点数量。"""
    if ts_to <= ts_from:
        return 0
    first = (ts_from // FUNDING_INTERVAL_MS + 1) * FUNDING_INTERVAL_MS
    if first > ts_to:
        return 0
    return int((ts_to - first) // FUNDING_INTERVAL_MS + 1)
