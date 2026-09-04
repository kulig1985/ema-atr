"""End-to-end walk through the state machine without network or MongoDB."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.config import DEFAULT_CONFIG, StrategyConfig
from app.main import ShadowSignalApp

SYMBOL = "BTCUSDT"
ENTRY_MS = 900_000
HOUR_MS = 3_600_000
BASE_HOUR = 1_700_000_000_000 // HOUR_MS * HOUR_MS


class FakeStorage:
    def __init__(self) -> None:
        self.signals: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.flow_buckets: list[dict[str, Any]] = []

    async def save_flow_bucket(self, bucket: dict[str, Any]) -> None:
        self.flow_buckets.append(bucket)

    async def save_candles(self, candles: list[dict[str, Any]]) -> None:
        pass

    async def insert_signal(self, document: dict[str, Any]) -> str:
        self.signals.append(document)
        return f"signal-{len(self.signals)}"

    async def update_signal(self, signal_id: Any, fields: dict[str, Any]) -> None:
        self.updates.append({"_id": signal_id, **fields})


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def build_app() -> tuple[ShadowSignalApp, FakeStorage, FakeTelegram]:
    config = StrategyConfig(document=dict(DEFAULT_CONFIG))
    storage = FakeStorage()
    telegram = FakeTelegram()
    app = ShadowSignalApp(config, storage, None, telegram, [SYMBOL])

    rt = app.runtimes[SYMBOL]
    rt.entry_ema, rt.entry_atr = 100.0, 10.0          # lowerEntry = 82.5, upperEntry = 117.5
    rt.exit_ema, rt.exit_atr = 100.0, 12.0
    rt.entry_last_close_time = BASE_HOUR - 1
    rt.exit_last_close_time = BASE_HOUR - 1
    rt.entry_last_open_time = BASE_HOUR - ENTRY_MS
    rt.exit_last_open_time = BASE_HOUR - HOUR_MS
    rt.market_stream_continuous = True
    rt.best_bid, rt.best_ask = 82.99, 83.01
    return app, storage, telegram


def agg_trade(price: float, qty: float, trade_ms: int, buyer_is_maker: bool) -> dict[str, Any]:
    return {
        "e": "aggTrade",
        "s": SYMBOL,
        "p": str(price),
        "q": str(qty),
        "T": trade_ms,
        "E": trade_ms,
        "m": buyer_is_maker,
    }


async def drive(app: ShadowSignalApp) -> None:
    rt = app.runtimes[SYMBOL]

    # A trade in the previous 15m window: the current window is only fully
    # observed from the next boundary on.
    await app.handle_market_event(agg_trade(80.0, 1.0, BASE_HOUR - 60_000, False))

    # Eight minutes of increasingly buy-heavy flow inside the new 15m window,
    # so the CVD lookback fills with complete buckets and curves upward.
    for minute in range(8):
        trade_ms = BASE_HOUR + minute * 60_000
        rt.best_book_ms = trade_ms
        rt.last_book_event_ms = trade_ms
        await app.handle_market_event(agg_trade(80.0, 1.0 + 0.3 * minute, trade_ms, False))
        await app.handle_market_event(agg_trade(80.0, 1.0, trade_ms + 1_000, True))

    # Price walks up to the lower entry band and crosses it from below.
    for price, offset in ((82.0, 480_000), (83.0, 481_000)):
        trade_ms = BASE_HOUR + offset
        rt.last_book_event_ms = trade_ms
        await app.handle_market_event(agg_trade(price, 1.0, trade_ms, False))


def run_scenario(monkeypatch) -> tuple[ShadowSignalApp, FakeStorage, FakeTelegram]:
    app, storage, telegram = build_app()
    # Freshness checks compare against the wall clock; pin it to the scenario.
    monkeypatch.setattr(time, "time", lambda: (BASE_HOUR + 481_000) / 1000.0)
    asyncio.run(drive(app))
    return app, storage, telegram


def test_re_entry_across_the_lower_band_produces_a_long_signal(monkeypatch) -> None:
    app, storage, telegram = run_scenario(monkeypatch)

    assert len(storage.signals) == 1, storage.signals
    signal = storage.signals[0]
    assert signal["side"] == "LONG"
    assert signal["signalPrice"] == 83.0
    assert signal["entry"]["lowerEntry"] == 82.5
    assert signal["exitGuideline"]["price"] == 100.0 + 1.75 * 12.0
    assert signal["validation"]["cvdSlope"] > 0
    assert signal["validation"]["candleOpen"] == 80.0
    assert app.runtimes[SYMBOL].state == "COOLDOWN"


def test_signal_is_announced_on_telegram(monkeypatch) -> None:
    app, _storage, telegram = run_scenario(monkeypatch)
    asyncio.run(_drain(app))
    assert len(telegram.sent) == 1
    message = telegram.sent[0]
    assert f"<b>LONG · {SYMBOL}</b>" in message
    assert "Why now" in message
    assert "crossed back above" in message
    assert "No order is sent" in message


async def _drain(app: ShadowSignalApp) -> None:
    if app.notification_tasks:
        await asyncio.gather(*app.notification_tasks, return_exceptions=True)


def test_status_line_reports_state_bands_and_data_readiness(monkeypatch) -> None:
    app, _storage, _telegram = run_scenario(monkeypatch)
    line = app._status_line(app.runtimes[SYMBOL])
    assert line.startswith(f"{SYMBOL:<10} COOLDOWN")
    assert "band=[82.5..117.5]" in line
    assert "vwap=ready" in line
    assert "cvd=5/5" in line
    assert "spread=" in line
    assert "waiting=" in line
    assert "_cooldown" in line


def test_rejected_re_entry_keeps_the_symbol_armed(monkeypatch) -> None:
    app, storage, _telegram = build_app()
    rt = app.runtimes[SYMBOL]
    monkeypatch.setattr(time, "time", lambda: (BASE_HOUR + 481_000) / 1000.0)

    async def scenario() -> None:
        # No VWAP warmup and no CVD history: the crossing must be refused.
        await app.handle_market_event(agg_trade(80.0, 1.0, BASE_HOUR + 480_000, False))
        await app.handle_market_event(agg_trade(83.0, 1.0, BASE_HOUR + 481_000, False))

    asyncio.run(scenario())
    assert storage.signals == []
    assert rt.state == "LONG_ARMED"
