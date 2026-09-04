from __future__ import annotations

import pytest

from app.models import FlowBucket


def test_agg_trade_m_true_is_aggressive_sell() -> None:
    bucket = FlowBucket(symbol="BTCUSDT", start_ms=0)
    bucket.add(notional=100.0, buyer_is_maker=True)
    assert bucket.buy_notional == 0.0
    assert bucket.sell_notional == 100.0
    assert bucket.normalized_delta() == pytest.approx(-1.0)


def test_agg_trade_m_false_is_aggressive_buy() -> None:
    bucket = FlowBucket(symbol="BTCUSDT", start_ms=0)
    bucket.add(notional=100.0, buyer_is_maker=False)
    assert bucket.buy_notional == 100.0
    assert bucket.sell_notional == 0.0
    assert bucket.normalized_delta() == pytest.approx(1.0)
