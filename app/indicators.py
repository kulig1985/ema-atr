from __future__ import annotations

from typing import Iterable

import numpy as np


def ema(values: Iterable[float], period: int) -> float:
    data = [float(v) for v in values]
    if len(data) < period:
        raise ValueError("Not enough values for EMA")
    alpha = 2.0 / (period + 1.0)
    current = sum(data[:period]) / period
    for value in data[period:]:
        current = alpha * value + (1.0 - alpha) * current
    return float(current)


def ema_next(previous_ema: float, close: float, period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    return float(alpha * close + (1.0 - alpha) * previous_ema)


def true_range(high: float, low: float, previous_close: float) -> float:
    return float(max(high - low, abs(high - previous_close), abs(low - previous_close)))


def wilder_atr(highs: Iterable[float], lows: Iterable[float], closes: Iterable[float], period: int) -> float:
    h = [float(v) for v in highs]
    l = [float(v) for v in lows]
    c = [float(v) for v in closes]
    if not (len(h) == len(l) == len(c)):
        raise ValueError("High, low and close lengths differ")
    if len(c) < period + 1:
        raise ValueError("Not enough candles for ATR")
    trs = [true_range(h[i], l[i], c[i - 1]) for i in range(1, len(c))]
    current = sum(trs[:period]) / period
    for tr in trs[period:]:
        current = ((period - 1.0) * current + tr) / period
    return float(current)


def wilder_atr_next(previous_atr: float, tr: float, period: int) -> float:
    return float(((period - 1.0) * previous_atr + tr) / period)


def normalized_cvd_metrics(normalized_deltas: Iterable[float]) -> tuple[list[float], float, float]:
    deltas = np.asarray(list(normalized_deltas), dtype=float)
    if deltas.size < 3:
        raise ValueError("At least 3 normalized deltas are required")
    series = np.cumsum(deltas)
    x = np.linspace(-1.0, 1.0, series.size)
    slope = float(np.polyfit(x, series, 1)[0])
    a, _b, _c = np.polyfit(x, series, 2)
    curvature = float(2.0 * a)
    return [float(v) for v in series], slope, curvature


def spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0:
        raise ValueError("Mid price must be > 0")
    return float((ask - bid) / mid * 10_000.0)


def bullish_vwap(current_price: float, candle_open: float, vwap: float) -> bool:
    return current_price > candle_open and candle_open < vwap < current_price


def bearish_vwap(current_price: float, candle_open: float, vwap: float) -> bool:
    return current_price < candle_open and current_price < vwap < candle_open


def cvd_valid(side: str, slope: float, curvature: float) -> bool:
    if side == "LONG":
        return slope > 0 and curvature >= 0
    if side == "SHORT":
        return slope < 0 and curvature <= 0
    raise ValueError(f"Unknown side: {side}")


def directional_return_pct(side: str, signal_price: float, current_price: float) -> float:
    if side == "LONG":
        return float((current_price - signal_price) / signal_price * 100.0)
    if side == "SHORT":
        return float((signal_price - current_price) / signal_price * 100.0)
    raise ValueError(f"Unknown side: {side}")
