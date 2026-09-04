from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .indicators import (
    bearish_vwap,
    bullish_vwap,
    cvd_valid,
    normalized_cvd_metrics,
    spread_bps,
)
from .models import SymbolRuntime


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a signal validation attempt.

    Either `snapshot` is set (accepted), or `reason` carries a stable code and
    `detail` the numbers worth logging.
    """

    snapshot: dict[str, Any] | None = None
    reason: str | None = None
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.snapshot is not None


def _rejected(reason: str, detail: str = "") -> ValidationResult:
    return ValidationResult(snapshot=None, reason=reason, detail=detail)


def evaluate_validation(
    rt: SymbolRuntime,
    side: str,
    current_price: float,
    now_ms: int,
) -> ValidationResult:
    """Check every signal condition in one place and say which one failed."""
    vwap = rt.current_vwap
    if not rt.vwap_complete or rt.candle_open is None or vwap is None:
        return _rejected(
            "vwap_not_ready",
            f"complete={rt.vwap_complete} open={rt.candle_open} vwap={vwap}",
        )

    vwap_ok = (
        bullish_vwap(current_price, rt.candle_open, vwap)
        if side == "LONG"
        else bearish_vwap(current_price, rt.candle_open, vwap)
    )
    if not vwap_ok:
        return _rejected(
            "vwap_condition",
            f"price={current_price:.8f} open={rt.candle_open:.8f} vwap={vwap:.8f}",
        )

    lookback = int(rt.settings["cvdLookback"])
    values = list(rt.cvd_deltas)
    window = values[-lookback:]
    if len(window) < lookback or any(value is None for value in window):
        ready = sum(1 for value in values if value is not None)
        return _rejected("cvd_not_enough_buckets", f"complete={ready}/{lookback}")

    cvd_series, slope, curvature = normalized_cvd_metrics([float(value) for value in window])
    if not cvd_valid(side, slope, curvature):
        return _rejected("cvd_direction", f"slope={slope:+.6f} curv={curvature:+.6f}")

    if rt.best_bid is None or rt.best_ask is None or rt.last_book_event_ms is None:
        return _rejected("book_missing")

    current_spread = spread_bps(rt.best_bid, rt.best_ask)
    max_spread = float(rt.settings["maxSpreadBps"])
    if current_spread > max_spread:
        return _rejected("spread_too_wide", f"{current_spread:.3f} bps > {max_spread:.3f} bps")

    if rt.last_trade_event_ms is None:
        return _rejected("trade_missing")

    trade_age_sec = max(0.0, (now_ms - rt.last_trade_event_ms) / 1000.0)
    book_age_sec = max(0.0, (now_ms - rt.last_book_event_ms) / 1000.0)
    max_trade_age = float(rt.settings["tradeMaxAgeSec"])
    max_book_age = float(rt.settings["bookMaxAgeSec"])
    if trade_age_sec > max_trade_age:
        return _rejected("trade_stale", f"{trade_age_sec:.2f}s > {max_trade_age:.2f}s")
    if book_age_sec > max_book_age:
        return _rejected("book_stale", f"{book_age_sec:.2f}s > {max_book_age:.2f}s")

    return ValidationResult(
        snapshot={
            "candleOpen": rt.candle_open,
            "vwap": vwap,
            "normalizedCvdSeries": cvd_series,
            "cvdSlope": slope,
            "cvdCurvature": curvature,
            "bid": rt.best_bid,
            "ask": rt.best_ask,
            "spreadBps": current_spread,
            "tradeAgeSec": trade_age_sec,
            "bookAgeSec": book_age_sec,
        }
    )
