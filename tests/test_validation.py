from __future__ import annotations

from collections import deque

from app.models import SymbolRuntime
from app.validation import evaluate_validation

NOW_MS = 1_700_000_000_000

SETTINGS = {
    "entryTimeframe": "15m",
    "exitTimeframe": "1h",
    "emaPeriod": 20,
    "atrPeriod": 14,
    "xEntry": 1.75,
    "xExit": 1.75,
    "cvdLookback": 3,
    "maxSpreadBps": 10,
    "bookMaxAgeSec": 3,
    "tradeMaxAgeSec": 3,
    "cooldownSec": 600,
}

PRICE = 102.0


def long_runtime(**overrides) -> SymbolRuntime:
    """A runtime where every LONG validation condition passes."""
    rt = SymbolRuntime(symbol="BTCUSDT", settings=dict(SETTINGS))
    rt.candle_open = 100.0
    rt.vwap_complete = True
    rt.vwap_notional_sum = 101.0
    rt.vwap_qty_sum = 1.0
    rt.cvd_deltas = deque([0.1, 0.2, 0.3], maxlen=3)
    rt.best_bid = 101.99
    rt.best_ask = 102.01
    rt.last_book_event_ms = NOW_MS
    rt.last_trade_event_ms = NOW_MS
    for key, value in overrides.items():
        setattr(rt, key, value)
    return rt


def test_accepts_when_every_condition_holds() -> None:
    result = evaluate_validation(long_runtime(), "LONG", PRICE, NOW_MS)
    assert result.accepted
    assert result.reason is None
    assert result.snapshot["vwap"] == 101.0
    assert result.snapshot["cvdSlope"] > 0


def test_rejects_partially_observed_vwap_window() -> None:
    result = evaluate_validation(long_runtime(vwap_complete=False), "LONG", PRICE, NOW_MS)
    assert result.reason == "vwap_not_ready"
    assert result.snapshot is None


def test_rejects_when_price_is_not_above_vwap_and_open() -> None:
    result = evaluate_validation(long_runtime(candle_open=103.0), "LONG", PRICE, NOW_MS)
    assert result.reason == "vwap_condition"


def test_rejects_when_fewer_complete_buckets_than_lookback() -> None:
    rt = long_runtime(cvd_deltas=deque([0.1, 0.2], maxlen=3))
    assert evaluate_validation(rt, "LONG", PRICE, NOW_MS).reason == "cvd_not_enough_buckets"


def test_rejects_when_a_bucket_in_the_lookback_was_incomplete() -> None:
    rt = long_runtime(cvd_deltas=deque([0.1, None, 0.3], maxlen=3))
    assert evaluate_validation(rt, "LONG", PRICE, NOW_MS).reason == "cvd_not_enough_buckets"


def test_rejects_long_on_falling_cvd() -> None:
    rt = long_runtime(cvd_deltas=deque([-0.1, -0.2, -0.3], maxlen=3))
    result = evaluate_validation(rt, "LONG", PRICE, NOW_MS)
    assert result.reason == "cvd_direction"
    assert "slope" in result.detail


def test_rejects_when_book_has_not_arrived_yet() -> None:
    rt = long_runtime(best_bid=None, best_ask=None, last_book_event_ms=None)
    assert evaluate_validation(rt, "LONG", PRICE, NOW_MS).reason == "book_missing"


def test_rejects_spread_wider_than_the_configured_maximum() -> None:
    rt = long_runtime(best_bid=101.0, best_ask=103.0)
    result = evaluate_validation(rt, "LONG", PRICE, NOW_MS)
    assert result.reason == "spread_too_wide"
    assert "bps" in result.detail


def test_rejects_when_no_trade_has_been_seen() -> None:
    rt = long_runtime(last_trade_event_ms=None)
    assert evaluate_validation(rt, "LONG", PRICE, NOW_MS).reason == "trade_missing"


def test_rejects_stale_trade_data() -> None:
    rt = long_runtime(last_trade_event_ms=NOW_MS - 5_000)
    assert evaluate_validation(rt, "LONG", PRICE, NOW_MS).reason == "trade_stale"


def test_rejects_stale_book_data() -> None:
    rt = long_runtime(last_book_event_ms=NOW_MS - 5_000)
    assert evaluate_validation(rt, "LONG", PRICE, NOW_MS).reason == "book_stale"


def test_short_mirrors_the_long_conditions() -> None:
    rt = long_runtime(
        candle_open=104.0,
        vwap_notional_sum=103.0,
        cvd_deltas=deque([-0.1, -0.2, -0.3], maxlen=3),
    )
    result = evaluate_validation(rt, "SHORT", PRICE, NOW_MS)
    assert result.accepted, result.reason
    assert result.snapshot["cvdSlope"] < 0


def test_short_is_rejected_when_cvd_rises() -> None:
    rt = long_runtime(candle_open=104.0, vwap_notional_sum=103.0)
    assert evaluate_validation(rt, "SHORT", PRICE, NOW_MS).reason == "cvd_direction"
