from __future__ import annotations

import pytest

from app.indicators import (
    bearish_vwap,
    bullish_vwap,
    cvd_valid,
    directional_return_pct,
    ema,
    ema_next,
    normalized_cvd_metrics,
    spread_bps,
    true_range,
    wilder_atr,
    wilder_atr_next,
)


def test_ema_uses_sma_seed_then_recursive_formula() -> None:
    assert ema([1, 2, 3], 3) == pytest.approx(2.0)
    assert ema([1, 2, 3, 4], 3) == pytest.approx(3.0)
    assert ema_next(3.0, 5.0, 3) == pytest.approx(4.0)


def test_true_range_and_wilder_atr() -> None:
    highs = [11, 12, 13, 12, 14]
    lows = [9, 10, 11, 10, 12]
    closes = [10, 11, 12, 11, 13]

    assert true_range(14, 12, 11) == pytest.approx(3.0)
    assert wilder_atr(highs, lows, closes, 3) == pytest.approx(7.0 / 3.0)
    assert wilder_atr_next(2.0, 3.0, 3) == pytest.approx(7.0 / 3.0)


def test_normalized_cvd_slope_and_curvature_signs() -> None:
    _series, slope, curvature = normalized_cvd_metrics([0.1, 0.2, 0.3, 0.4, 0.5])
    assert slope > 0
    assert curvature > 0
    assert cvd_valid("LONG", slope, curvature)

    _series, slope, curvature = normalized_cvd_metrics([-0.1, -0.2, -0.3, -0.4, -0.5])
    assert slope < 0
    assert curvature < 0
    assert cvd_valid("SHORT", slope, curvature)


def test_vwap_validation_is_strict() -> None:
    assert bullish_vwap(102.0, 100.0, 101.0)
    assert not bullish_vwap(102.0, 100.0, 100.0)
    assert bearish_vwap(98.0, 100.0, 99.0)
    assert not bearish_vwap(98.0, 100.0, 100.0)


def test_spread_bps() -> None:
    assert spread_bps(99.0, 101.0) == pytest.approx(200.0)


def test_directional_return_pct() -> None:
    assert directional_return_pct("LONG", 100.0, 101.0) == pytest.approx(1.0)
    assert directional_return_pct("SHORT", 100.0, 99.0) == pytest.approx(1.0)


def test_positive_return_always_means_the_signal_direction_worked() -> None:
    """The sign is direction-adjusted, so both sides are comparable in one average."""
    assert directional_return_pct("LONG", 100.0, 101.0) > 0   # price up, long wins
    assert directional_return_pct("LONG", 100.0, 99.0) < 0    # price down, long loses
    assert directional_return_pct("SHORT", 100.0, 99.0) > 0   # price down, short wins
    assert directional_return_pct("SHORT", 100.0, 101.0) < 0  # price up, short loses
