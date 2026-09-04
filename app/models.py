from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Candle:
    symbol: str
    timeframe: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_document(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "openTime": self.open_time,
            "closeTime": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "closed": True,
            "source": "binance",
        }


@dataclass
class FlowBucket:
    symbol: str
    start_ms: int
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    trade_count: int = 0
    complete_capture: bool = True

    def add(self, notional: float, buyer_is_maker: bool) -> None:
        if buyer_is_maker:
            self.sell_notional += notional
        else:
            self.buy_notional += notional
        self.trade_count += 1

    def normalized_delta(self) -> float | None:
        total = self.buy_notional + self.sell_notional
        if total <= 0:
            return None
        return (self.buy_notional - self.sell_notional) / total

    def to_document(self) -> dict[str, Any]:
        total = self.buy_notional + self.sell_notional
        delta = self.buy_notional - self.sell_notional
        return {
            "symbol": self.symbol,
            "bucketStart": self.start_ms,
            "bucketEnd": self.start_ms + 60_000 - 1,
            "buyNotional": self.buy_notional,
            "sellNotional": self.sell_notional,
            "delta": delta,
            "total": total,
            "normalizedDelta": self.normalized_delta(),
            "tradeCount": self.trade_count,
            "completeCapture": self.complete_capture,
            "closed": True,
        }


@dataclass
class OutcomeTracker:
    signal_id: Any
    symbol: str
    side: str
    signal_price: float
    signal_time_ms: int
    returns: dict[int, float | None] = field(
        default_factory=lambda: {60: None, 180: None, 300: None, 600: None, 900: None, 1200: None}
    )
    mfe: float = 0.0
    mae: float = 0.0
    time_to_mfe: float = 0.0
    time_to_mae: float = 0.0

    def fields(self) -> dict[str, Any]:
        return {
            "return1m": self.returns[60],
            "return3m": self.returns[180],
            "return5m": self.returns[300],
            "return10m": self.returns[600],
            "return15m": self.returns[900],
            "return20m": self.returns[1200],
            "MFE": self.mfe,
            "MAE": self.mae,
            "timeToMFE": self.time_to_mfe,
            "timeToMAE": self.time_to_mae,
        }


@dataclass
class SymbolRuntime:
    symbol: str
    settings: dict[str, Any]
    state: str = "IDLE"
    cooldown_until: datetime | None = None

    entry_ema: float | None = None
    entry_atr: float | None = None
    entry_last_open_time: int | None = None
    entry_last_close_time: int | None = None
    entry_last_close_price: float | None = None

    exit_ema: float | None = None
    exit_atr: float | None = None
    exit_last_open_time: int | None = None
    exit_last_close_time: int | None = None
    exit_last_close_price: float | None = None

    previous_price: float | None = None
    last_price: float | None = None
    last_trade_event_ms: int | None = None
    last_trade_received_at: float | None = None

    best_bid: float | None = None
    best_ask: float | None = None
    last_book_event_ms: int | None = None
    last_book_received_at: float | None = None

    vwap_bucket_start: int | None = None
    vwap_notional_sum: float = 0.0
    vwap_qty_sum: float = 0.0
    candle_open: float | None = None
    vwap_complete: bool = False

    flow_bucket: FlowBucket | None = None
    cvd_deltas: deque = field(default_factory=deque)
    market_stream_continuous: bool = False

    @property
    def lower_entry(self) -> float | None:
        if self.entry_ema is None or self.entry_atr is None:
            return None
        return self.entry_ema - float(self.settings["xEntry"]) * self.entry_atr

    @property
    def upper_entry(self) -> float | None:
        if self.entry_ema is None or self.entry_atr is None:
            return None
        return self.entry_ema + float(self.settings["xEntry"]) * self.entry_atr

    @property
    def current_vwap(self) -> float | None:
        if self.vwap_qty_sum <= 0:
            return None
        return self.vwap_notional_sum / self.vwap_qty_sum

    def exit_guideline(self, side: str) -> float | None:
        if self.exit_ema is None or self.exit_atr is None:
            return None
        x_exit = float(self.settings["xExit"])
        if side == "LONG":
            return self.exit_ema + x_exit * self.exit_atr
        if side == "SHORT":
            return self.exit_ema - x_exit * self.exit_atr
        raise ValueError(f"Unknown side: {side}")
