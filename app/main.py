from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from .binance_feed import (
    StreamStats,
    fetch_closed_klines,
    fetch_symbol_universe,
    run_market_stream,
    run_public_stream,
)
from .config import StrategyConfig, interval_to_ms
from .indicators import (
    directional_return_pct,
    ema,
    ema_next,
    spread_bps,
    true_range,
    wilder_atr,
    wilder_atr_next,
)
from .messages import (
    STATS_WINDOW,
    format_price,
    format_signal_message,
    format_status_message,
    summarize_signals,
    waiting_for,
)
from .models import Candle, FlowBucket, OutcomeTracker, SymbolRuntime
from .storage import Storage
from .symbols import select_symbols
from .telegram import TelegramClient
from .validation import evaluate_validation

logger = logging.getLogger(__name__)
UTC = timezone.utc


class ShadowSignalApp:
    def __init__(
        self,
        config: StrategyConfig,
        storage: Storage,
        http: httpx.AsyncClient,
        telegram: TelegramClient,
        symbols: list[str],
    ) -> None:
        self.config = config
        self.storage = storage
        self.http = http
        self.telegram = telegram
        self.symbols = list(symbols)
        self.runtimes = {
            symbol: SymbolRuntime(
                symbol=symbol,
                settings=config.for_symbol(symbol),
                cvd_deltas=deque(maxlen=int(config.for_symbol(symbol)["cvdLookback"])),
            )
            for symbol in self.symbols
        }
        self.outcomes: dict[str, list[OutcomeTracker]] = {symbol: [] for symbol in self.symbols}
        self.notification_tasks: set[asyncio.Task] = set()
        self.stop_event = asyncio.Event()
        self.market_stats = StreamStats("Binance market stream")
        self.public_stats = StreamStats("Binance public stream")
        self.max_loop_lag_sec = 0.0
        self.started_at = datetime.now(UTC)

    def _log_startup_summary(self) -> None:
        """Spell out what the process will do, so the log is readable without the docs."""
        document = self.config.document
        entry_tf, exit_tf = document["entryTimeframe"], document["exitTimeframe"]
        digest = self.config.telegram_status_sec
        logger.info("=" * 72)
        logger.info("Shadow Signal starting with %d symbol(s): %s", len(self.symbols), ", ".join(self.symbols))
        logger.info(
            "Entry band: %s EMA%s +/- %s x ATR%s   |   Exit guideline: %s EMA%s +/- %s x ATR%s",
            entry_tf, document["emaPeriod"], document["xEntry"], document["atrPeriod"],
            exit_tf, document["emaPeriod"], document["xExit"], document["atrPeriod"],
        )
        logger.info(
            "A signal needs ALL of: re-entry cross back through the band edge, VWAP alignment, "
            "CVD direction over %s complete 1m buckets, spread <= %s bps, "
            "trade/book younger than %ss/%ss",
            document["cvdLookback"], document["maxSpreadBps"],
            document["tradeMaxAgeSec"], document["bookMaxAgeSec"],
        )
        logger.info(
            "After a signal: %ss cooldown, 20 min outcome measurement. NO ORDER IS EVER SENT.",
            document["cooldownSec"],
        )
        logger.info(
            "Log status every %ss | Telegram: signals on, digest %s",
            self.config.log_status_sec,
            f"every {digest}s" if digest > 0 else "disabled",
        )
        logger.info("=" * 72)

    async def bootstrap(self) -> None:
        self._log_startup_summary()
        interrupted = await self.storage.mark_stale_active_measurements_interrupted()
        if interrupted:
            logger.warning("Marked %d unfinished previous measurements as INTERRUPTED", interrupted)
        await asyncio.gather(*(self._bootstrap_symbol(rt) for rt in self.runtimes.values()))

    async def _bootstrap_symbol(self, rt: SymbolRuntime) -> None:
        entry_tf = str(rt.settings["entryTimeframe"])
        exit_tf = str(rt.settings["exitTimeframe"])
        if entry_tf == exit_tf:
            candles = await fetch_closed_klines(self.http, rt.symbol, entry_tf, limit=500)
            await self.storage.save_candles([c.to_document() for c in candles])
            self._initialize_entry_indicators(rt, candles)
            self._initialize_exit_indicators(rt, candles)
        else:
            entry_candles, exit_candles = await asyncio.gather(
                fetch_closed_klines(self.http, rt.symbol, entry_tf, limit=500),
                fetch_closed_klines(self.http, rt.symbol, exit_tf, limit=500),
            )
            await self.storage.save_candles(
                [c.to_document() for c in entry_candles] + [c.to_document() for c in exit_candles]
            )
            self._initialize_entry_indicators(rt, entry_candles)
            self._initialize_exit_indicators(rt, exit_candles)

        latest = await self.storage.latest_signal(rt.symbol)
        if latest and latest.get("signalAt"):
            cooldown_until = latest["signalAt"] + timedelta(seconds=int(rt.settings["cooldownSec"]))
            if cooldown_until > datetime.now(UTC):
                rt.state = "COOLDOWN"
                rt.cooldown_until = cooldown_until

        logger.info(
            "%s ready: entry EMA=%.8f ATR=%.8f, exit EMA=%.8f ATR=%.8f, state=%s",
            rt.symbol,
            rt.entry_ema,
            rt.entry_atr,
            rt.exit_ema,
            rt.exit_atr,
            rt.state,
        )

    def _initialize_entry_indicators(self, rt: SymbolRuntime, candles: list[Candle]) -> None:
        period_ema = int(rt.settings["emaPeriod"])
        period_atr = int(rt.settings["atrPeriod"])
        if len(candles) < max(period_ema, period_atr + 1):
            raise RuntimeError(f"{rt.symbol}: not enough closed entry candles")
        rt.entry_ema = ema([c.close for c in candles], period_ema)
        rt.entry_atr = wilder_atr(
            [c.high for c in candles], [c.low for c in candles], [c.close for c in candles], period_atr
        )
        last = candles[-1]
        rt.entry_last_open_time = last.open_time
        rt.entry_last_close_time = last.close_time
        rt.entry_last_close_price = last.close

    def _initialize_exit_indicators(self, rt: SymbolRuntime, candles: list[Candle]) -> None:
        period_ema = int(rt.settings["emaPeriod"])
        period_atr = int(rt.settings["atrPeriod"])
        if len(candles) < max(period_ema, period_atr + 1):
            raise RuntimeError(f"{rt.symbol}: not enough closed exit candles")
        rt.exit_ema = ema([c.close for c in candles], period_ema)
        rt.exit_atr = wilder_atr(
            [c.high for c in candles], [c.low for c in candles], [c.close for c in candles], period_atr
        )
        last = candles[-1]
        rt.exit_last_open_time = last.open_time
        rt.exit_last_close_time = last.close_time
        rt.exit_last_close_price = last.close

    async def _resync_closed_candles(self) -> None:
        for rt in self.runtimes.values():
            entry_tf = str(rt.settings["entryTimeframe"])
            exit_tf = str(rt.settings["exitTimeframe"])
            if entry_tf == exit_tf:
                candles = await fetch_closed_klines(self.http, rt.symbol, entry_tf, limit=500)
                missing = [c for c in candles if rt.entry_last_open_time is None or c.open_time > rt.entry_last_open_time]
                for candle in missing:
                    await self._apply_closed_candle(rt, candle, persist=False)
                await self.storage.save_candles([c.to_document() for c in missing])
            else:
                entry_candles, exit_candles = await asyncio.gather(
                    fetch_closed_klines(self.http, rt.symbol, entry_tf, limit=500),
                    fetch_closed_klines(self.http, rt.symbol, exit_tf, limit=500),
                )
                missing_entry = [
                    c for c in entry_candles if rt.entry_last_open_time is None or c.open_time > rt.entry_last_open_time
                ]
                missing_exit = [
                    c for c in exit_candles if rt.exit_last_open_time is None or c.open_time > rt.exit_last_open_time
                ]
                for candle in missing_entry:
                    await self._apply_closed_candle(rt, candle, persist=False)
                for candle in missing_exit:
                    await self._apply_closed_candle(rt, candle, persist=False)
                await self.storage.save_candles(
                    [c.to_document() for c in missing_entry] + [c.to_document() for c in missing_exit]
                )

    async def _apply_closed_candle(self, rt: SymbolRuntime, candle: Candle, persist: bool = True) -> None:
        period_ema = int(rt.settings["emaPeriod"])
        period_atr = int(rt.settings["atrPeriod"])
        entry_tf = str(rt.settings["entryTimeframe"])
        exit_tf = str(rt.settings["exitTimeframe"])

        changed = False
        if candle.timeframe == entry_tf and (
            rt.entry_last_open_time is None or candle.open_time > rt.entry_last_open_time
        ):
            if rt.entry_ema is None or rt.entry_atr is None or rt.entry_last_close_price is None:
                raise RuntimeError(f"{rt.symbol}: entry indicators are not initialized")
            tr = true_range(candle.high, candle.low, rt.entry_last_close_price)
            rt.entry_ema = ema_next(rt.entry_ema, candle.close, period_ema)
            rt.entry_atr = wilder_atr_next(rt.entry_atr, tr, period_atr)
            rt.entry_last_open_time = candle.open_time
            rt.entry_last_close_time = candle.close_time
            rt.entry_last_close_price = candle.close
            rt.previous_price = None
            changed = True
            logger.info(
                "%s %s closed: EMA=%.8f ATR=%.8f lower=%.8f upper=%.8f",
                rt.symbol,
                entry_tf,
                rt.entry_ema,
                rt.entry_atr,
                rt.lower_entry,
                rt.upper_entry,
            )

        if candle.timeframe == exit_tf and (
            rt.exit_last_open_time is None or candle.open_time > rt.exit_last_open_time
        ):
            if rt.exit_ema is None or rt.exit_atr is None or rt.exit_last_close_price is None:
                raise RuntimeError(f"{rt.symbol}: exit indicators are not initialized")
            tr = true_range(candle.high, candle.low, rt.exit_last_close_price)
            rt.exit_ema = ema_next(rt.exit_ema, candle.close, period_ema)
            rt.exit_atr = wilder_atr_next(rt.exit_atr, tr, period_atr)
            rt.exit_last_open_time = candle.open_time
            rt.exit_last_close_time = candle.close_time
            rt.exit_last_close_price = candle.close
            changed = True
            logger.info(
                "%s %s closed: EMA=%.8f ATR=%.8f",
                rt.symbol,
                exit_tf,
                rt.exit_ema,
                rt.exit_atr,
            )

        if changed and persist:
            await self.storage.save_candles([candle.to_document()])

    async def handle_market_event(self, data: dict[str, Any]) -> None:
        event = data.get("e")
        if data.get("st") not in (None, 1):
            return
        if event == "aggTrade":
            await self._handle_agg_trade(data)
        elif event == "kline":
            await self._handle_kline(data)

    async def handle_public_event(self, data: dict[str, Any]) -> None:
        if data.get("e") != "bookTicker" or data.get("st") not in (None, 1):
            return
        symbol = str(data.get("s", "")).upper()
        rt = self.runtimes.get(symbol)
        if rt is None:
            return
        bid = float(data["b"])
        ask = float(data["a"])
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        rt.best_bid = bid
        rt.best_ask = ask
        rt.last_book_event_ms = int(data.get("E") or data.get("T") or time.time() * 1000)
        rt.last_book_received_at = time.monotonic()

    async def _handle_kline(self, data: dict[str, Any]) -> None:
        kline = data.get("k") or {}
        if not kline.get("x"):
            return
        symbol = str(data.get("s") or kline.get("s") or "").upper()
        rt = self.runtimes.get(symbol)
        if rt is None:
            return
        timeframe = str(kline.get("i"))
        if timeframe not in {str(rt.settings["entryTimeframe"]), str(rt.settings["exitTimeframe"])}:
            return
        candle = Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=int(kline["t"]),
            close_time=int(kline["T"]),
            open=float(kline["o"]),
            high=float(kline["h"]),
            low=float(kline["l"]),
            close=float(kline["c"]),
            volume=float(kline["v"]),
        )
        await self._apply_closed_candle(rt, candle)

    async def _handle_agg_trade(self, data: dict[str, Any]) -> None:
        symbol = str(data.get("s", "")).upper()
        rt = self.runtimes.get(symbol)
        if rt is None:
            return

        price = float(data["p"])
        qty = float(data["q"])
        trade_time_ms = int(data["T"])
        event_time_ms = int(data.get("E") or trade_time_ms)
        buyer_is_maker = bool(data["m"])

        rt.last_trade_event_ms = event_time_ms
        rt.last_trade_received_at = time.monotonic()
        rt.last_price = price

        await self._update_vwap(rt, trade_time_ms, price, qty)
        await self._update_flow(rt, trade_time_ms, price, qty, buyer_is_maker)
        await self._update_outcomes(rt.symbol, price, trade_time_ms)

        now = datetime.now(UTC)
        if rt.state == "COOLDOWN":
            if rt.cooldown_until is not None and now >= rt.cooldown_until:
                rt.state = "IDLE"
                rt.cooldown_until = None
                rt.previous_price = price
                logger.info("%s cooldown finished -> IDLE", rt.symbol)
            else:
                rt.previous_price = price
                return

        if not self._indicator_data_aligned(rt, trade_time_ms):
            rt.previous_price = price
            return

        lower = rt.lower_entry
        upper = rt.upper_entry
        if lower is None or upper is None:
            rt.previous_price = price
            return

        previous = rt.previous_price
        if rt.state == "IDLE":
            if price < lower:
                rt.state = "LONG_ARMED"
                logger.info("%s -> LONG_ARMED at %.8f (lower %.8f)", rt.symbol, price, lower)
            elif price > upper:
                rt.state = "SHORT_ARMED"
                logger.info("%s -> SHORT_ARMED at %.8f (upper %.8f)", rt.symbol, price, upper)

        elif rt.state == "LONG_ARMED":
            if previous is not None and previous <= lower and price > lower:
                await self._try_signal(rt, "LONG", price, trade_time_ms)

        elif rt.state == "SHORT_ARMED":
            if previous is not None and previous >= upper and price < upper:
                await self._try_signal(rt, "SHORT", price, trade_time_ms)

        rt.previous_price = price

    async def _update_vwap(self, rt: SymbolRuntime, trade_time_ms: int, price: float, qty: float) -> None:
        interval_ms = interval_to_ms(str(rt.settings["entryTimeframe"]))
        bucket_start = (trade_time_ms // interval_ms) * interval_ms
        if rt.vwap_bucket_start is None:
            rt.vwap_bucket_start = bucket_start
            rt.vwap_notional_sum = 0.0
            rt.vwap_qty_sum = 0.0
            rt.candle_open = price
            rt.vwap_complete = False
        elif bucket_start > rt.vwap_bucket_start:
            rt.vwap_bucket_start = bucket_start
            rt.vwap_notional_sum = 0.0
            rt.vwap_qty_sum = 0.0
            rt.candle_open = price
            rt.vwap_complete = rt.market_stream_continuous
        elif bucket_start < rt.vwap_bucket_start:
            return

        rt.vwap_notional_sum += price * qty
        rt.vwap_qty_sum += qty

    async def _update_flow(
        self,
        rt: SymbolRuntime,
        trade_time_ms: int,
        price: float,
        qty: float,
        buyer_is_maker: bool,
    ) -> None:
        minute_start = (trade_time_ms // 60_000) * 60_000
        if rt.flow_bucket is None:
            rt.flow_bucket = FlowBucket(
                symbol=rt.symbol,
                start_ms=minute_start,
                complete_capture=False,
            )
        elif minute_start > rt.flow_bucket.start_ms:
            await self._close_flow_bucket(rt, rt.flow_bucket)
            next_start = rt.flow_bucket.start_ms + 60_000
            while next_start < minute_start:
                empty = FlowBucket(
                    symbol=rt.symbol,
                    start_ms=next_start,
                    complete_capture=rt.market_stream_continuous,
                )
                await self._close_flow_bucket(rt, empty)
                next_start += 60_000
            rt.flow_bucket = FlowBucket(
                symbol=rt.symbol,
                start_ms=minute_start,
                complete_capture=rt.market_stream_continuous,
            )
        elif minute_start < rt.flow_bucket.start_ms:
            return

        rt.flow_bucket.add(price * qty, buyer_is_maker)

    async def _close_flow_bucket(self, rt: SymbolRuntime, bucket: FlowBucket) -> None:
        await self.storage.save_flow_bucket(bucket.to_document())
        value = bucket.normalized_delta() if bucket.complete_capture else None
        rt.cvd_deltas.append(value)

    def _indicator_data_aligned(self, rt: SymbolRuntime, trade_time_ms: int) -> bool:
        entry_ms = interval_to_ms(str(rt.settings["entryTimeframe"]))
        exit_ms = interval_to_ms(str(rt.settings["exitTimeframe"]))
        expected_entry_close = (trade_time_ms // entry_ms) * entry_ms - 1
        expected_exit_close = (trade_time_ms // exit_ms) * exit_ms - 1
        return (
            rt.entry_last_close_time == expected_entry_close
            and rt.exit_last_close_time == expected_exit_close
        )

    async def _try_signal(self, rt: SymbolRuntime, side: str, price: float, trade_time_ms: int) -> None:
        result = evaluate_validation(rt, side, price, int(time.time() * 1000))
        if not result.accepted:
            detail = f" ({result.detail})" if result.detail else ""
            logger.info(
                "%s %s re-entry REJECTED at %s: %s%s",
                rt.symbol,
                side,
                format_number(price),
                result.reason,
                detail,
            )
            return
        validation = result.snapshot

        exit_guideline = rt.exit_guideline(side)
        if exit_guideline is None:
            return

        signal_at = datetime.fromtimestamp(trade_time_ms / 1000.0, tz=UTC)
        state_before = rt.state
        document = {
            "symbol": rt.symbol,
            "side": side,
            "signalAt": signal_at,
            "signalPrice": price,
            "stateBeforeSignal": state_before,
            "entry": {
                "timeframe": rt.settings["entryTimeframe"],
                "ema": rt.entry_ema,
                "atr": rt.entry_atr,
                "xEntry": float(rt.settings["xEntry"]),
                "lowerEntry": rt.lower_entry,
                "upperEntry": rt.upper_entry,
            },
            "exitGuideline": {
                "timeframe": rt.settings["exitTimeframe"],
                "ema": rt.exit_ema,
                "atr": rt.exit_atr,
                "xExit": float(rt.settings["xExit"]),
                "price": exit_guideline,
            },
            "validation": validation,
            "configSnapshot": dict(rt.settings),
            "return1m": None,
            "return3m": None,
            "return5m": None,
            "return10m": None,
            "return15m": None,
            "return20m": None,
            "MFE": 0.0,
            "MAE": 0.0,
            "timeToMFE": 0.0,
            "timeToMAE": 0.0,
            "measurementStatus": "ACTIVE",
            "measurementCompletedAt": None,
            "telegramSent": False,
            "telegramError": None,
            "createdAt": datetime.now(UTC),
            "updatedAt": datetime.now(UTC),
        }
        signal_id = await self.storage.insert_signal(document)

        rt.state = "COOLDOWN"
        rt.cooldown_until = signal_at + timedelta(seconds=int(rt.settings["cooldownSec"]))
        tracker = OutcomeTracker(
            signal_id=signal_id,
            symbol=rt.symbol,
            side=side,
            signal_price=price,
            signal_time_ms=trade_time_ms,
        )
        self.outcomes[rt.symbol].append(tracker)

        logger.info(
            "%s %s SIGNAL at %s | band edge %s | vwap %s | cvd slope %+.4f curv %+.4f | "
            "spread %.2fbps | %s exit guideline %s",
            rt.symbol,
            side,
            format_price(price),
            format_price(rt.lower_entry if side == "LONG" else rt.upper_entry),
            format_price(validation["vwap"]),
            validation["cvdSlope"],
            validation["cvdCurvature"],
            validation["spreadBps"],
            rt.settings["exitTimeframe"],
            format_price(exit_guideline),
        )
        text = format_signal_message(rt, side, price, signal_at, exit_guideline, validation)
        task = asyncio.create_task(self._send_telegram(signal_id, text))
        self.notification_tasks.add(task)
        task.add_done_callback(self.notification_tasks.discard)

    async def _send_telegram(self, signal_id, text: str) -> None:
        try:
            await self.telegram.send(text)
            await self.storage.update_signal(
                signal_id,
                {"telegramSent": True, "telegramSentAt": datetime.now(UTC), "telegramError": None},
            )
        except Exception as exc:
            logger.exception("Telegram send failed")
            await self.storage.update_signal(
                signal_id,
                {"telegramSent": False, "telegramError": str(exc)},
            )

    async def _update_outcomes(self, symbol: str, current_price: float, trade_time_ms: int) -> None:
        trackers = self.outcomes[symbol]
        for tracker in list(trackers):
            elapsed = max(0.0, (trade_time_ms - tracker.signal_time_ms) / 1000.0)
            current_return = directional_return_pct(tracker.side, tracker.signal_price, current_price)
            if current_return > tracker.mfe:
                tracker.mfe = current_return
                tracker.time_to_mfe = elapsed
            if current_return < tracker.mae:
                tracker.mae = current_return
                tracker.time_to_mae = elapsed

            checkpoint_changed = False
            for seconds in (60, 180, 300, 600, 900, 1200):
                if tracker.returns[seconds] is None and elapsed >= seconds:
                    tracker.returns[seconds] = current_return
                    checkpoint_changed = True

            if checkpoint_changed:
                fields = tracker.fields()
                if tracker.returns[1200] is not None:
                    fields["measurementStatus"] = "COMPLETED"
                    fields["measurementCompletedAt"] = datetime.fromtimestamp(trade_time_ms / 1000.0, tz=UTC)
                    trackers.remove(tracker)
                await self.storage.update_signal(tracker.signal_id, fields)

    def _mark_market_gap(self) -> None:
        for rt in self.runtimes.values():
            if rt.state != "COOLDOWN":
                rt.state = "IDLE"
            rt.previous_price = None
            rt.last_trade_event_ms = None
            rt.last_trade_received_at = None
            rt.vwap_bucket_start = None
            rt.vwap_notional_sum = 0.0
            rt.vwap_qty_sum = 0.0
            rt.candle_open = None
            rt.vwap_complete = False
            rt.flow_bucket = None
            rt.cvd_deltas = deque(maxlen=int(rt.settings["cvdLookback"]))
            rt.market_stream_continuous = False

    def _status_line(self, rt: SymbolRuntime) -> str:
        """One compact line telling where this symbol stands and what it waits for."""
        now_ms = int(time.time() * 1000)
        band = f"band=[{format_price(rt.lower_entry)}..{format_price(rt.upper_entry)}]"
        parts = [f"{rt.symbol:<10} {rt.state:<11}", f"px={format_price(rt.last_price)}", band]

        if rt.last_price is not None and rt.entry_atr and rt.lower_entry and rt.upper_entry:
            if rt.last_price < rt.lower_entry:
                parts.append(f"{(rt.lower_entry - rt.last_price) / rt.entry_atr:.2f}atr_below_lower")
            elif rt.last_price > rt.upper_entry:
                parts.append(f"{(rt.last_price - rt.upper_entry) / rt.entry_atr:.2f}atr_above_upper")
            else:
                parts.append(f"{(rt.last_price - rt.lower_entry) / rt.entry_atr:+.2f}atr_in_band")

        if rt.state == "COOLDOWN" and rt.cooldown_until is not None:
            remaining = max(0.0, (rt.cooldown_until - datetime.now(UTC)).total_seconds())
            parts.append(f"waiting={remaining:.0f}s_cooldown")
        else:
            parts.append(f"waiting={waiting_for(rt).replace(' ', '_')}")

        ready_buckets = sum(1 for value in rt.cvd_deltas if value is not None)
        spread = (
            f"{spread_bps(rt.best_bid, rt.best_ask):.2f}bps"
            if rt.best_bid and rt.best_ask
            else "n/a"
        )
        parts += [
            f"vwap={'ready' if rt.vwap_complete else 'warmup'}",
            f"cvd={ready_buckets}/{int(rt.settings['cvdLookback'])}",
            f"spread={spread}",
            f"age(trade/book)={_age_text(now_ms, rt.last_trade_event_ms)}"
            f"/{_age_text(now_ms, rt.last_book_event_ms)}",
            f"meas={len(self.outcomes[rt.symbol])}",
        ]
        return " ".join(parts)

    def _log_status(self) -> None:
        logger.info(
            "STATUS loop_lag_max=%.0fms market[%s] public[%s]",
            self.max_loop_lag_sec * 1000.0,
            self.market_stats.summary(),
            self.public_stats.summary(),
        )
        self.max_loop_lag_sec = 0.0
        for rt in self.runtimes.values():
            logger.info("  %s", self._status_line(rt))

    async def status_loop(self) -> None:
        while not self.stop_event.is_set():
            await self._sleep_or_stop(self.config.log_status_sec)
            if self.stop_event.is_set():
                return
            self._log_status()

    async def telegram_status_loop(self) -> None:
        """Periodic Telegram digest: recent signal performance and live state."""
        interval = self.config.telegram_status_sec
        if interval <= 0:
            logger.info("Telegram status digest disabled (telegramStatusSec=0)")
            return
        # Send one digest shortly after startup so the wiring is visible immediately,
        # then settle into the configured interval.
        delay = min(30, interval)
        while not self.stop_event.is_set():
            await self._sleep_or_stop(delay)
            if self.stop_event.is_set():
                return
            delay = interval
            try:
                await self._send_status_digest()
            except Exception:
                logger.exception("Telegram status digest failed")

    async def _send_status_digest(self) -> None:
        now = datetime.now(UTC)
        signals = await self.storage.signals_since(now - STATS_WINDOW)
        text = format_status_message(
            now=now,
            uptime_sec=(now - self.started_at).total_seconds(),
            runtimes=self.runtimes,
            active_measurements={s: len(t) for s, t in self.outcomes.items()},
            market_summary=self.market_stats.summary(),
            public_summary=self.public_stats.summary(),
            max_loop_lag_sec=self.max_loop_lag_sec,
            performance=summarize_signals(signals),
        )
        await self.telegram.send(text)
        logger.info("Telegram status digest sent (%d signals in the last 24h)", len(signals))

    async def loop_lag_loop(self) -> None:
        """Measure how late a 1s sleep wakes up: high values mean a saturated event loop."""
        while not self.stop_event.is_set():
            started = time.monotonic()
            await self._sleep_or_stop(1.0)
            if self.stop_event.is_set():
                return
            self.max_loop_lag_sec = max(self.max_loop_lag_sec, time.monotonic() - started - 1.0)

    async def market_loop(self) -> None:
        backoff = 1
        while not self.stop_event.is_set():
            self._mark_market_gap()
            try:
                await self._resync_closed_candles()
                for rt in self.runtimes.values():
                    rt.market_stream_continuous = True
                await run_market_stream(
                    self.symbols,
                    str(self.config.document["entryTimeframe"]),
                    str(self.config.document["exitTimeframe"]),
                    self.handle_market_event,
                    self.market_stats,
                )
                if not self.stop_event.is_set():
                    logger.warning(
                        "Binance market stream ended without error after %s",
                        self.market_stats.summary(),
                    )
                    await self._interrupt_active_measurements("market_stream_gap")
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Binance market stream closed after %s: %s: %s - reconnecting in %ss",
                    self.market_stats.summary(),
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                logger.debug("Market stream traceback", exc_info=True)
                await self._interrupt_active_measurements("market_stream_gap")
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, 30)

    async def public_loop(self) -> None:
        backoff = 1
        while not self.stop_event.is_set():
            try:
                await run_public_stream(self.symbols, self.handle_public_event, self.public_stats)
                if not self.stop_event.is_set():
                    logger.warning(
                        "Binance public stream ended without error after %s",
                        self.public_stats.summary(),
                    )
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Binance public stream closed after %s: %s: %s - reconnecting in %ss",
                    self.public_stats.summary(),
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                logger.debug("Public stream traceback", exc_info=True)
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, 30)

    async def heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            await self._write_heartbeat()
            await self._sleep_or_stop(self.config.heartbeat_sec)

    async def _write_heartbeat(self) -> None:
        now_ms = int(time.time() * 1000)
        symbols: dict[str, Any] = {}
        for symbol, rt in self.runtimes.items():
            trade_age = None
            book_age = None
            if rt.last_trade_event_ms is not None:
                trade_age = max(0.0, (now_ms - rt.last_trade_event_ms) / 1000.0)
            if rt.last_book_event_ms is not None:
                book_age = max(0.0, (now_ms - rt.last_book_event_ms) / 1000.0)
            symbols[symbol] = {
                "state": rt.state,
                "lastPrice": rt.last_price,
                "lowerEntry": rt.lower_entry,
                "upperEntry": rt.upper_entry,
                "tradeAgeSec": trade_age,
                "bookAgeSec": book_age,
                "vwapComplete": rt.vwap_complete,
                "closedCvdBucketsReady": sum(1 for value in rt.cvd_deltas if value is not None),
                "activeMeasurements": len(self.outcomes[symbol]),
            }
        await self.storage.insert_heartbeat(
            {
                "ts": datetime.now(UTC),
                "status": "ok",
                "symbols": symbols,
                "streams": {
                    "market": {
                        "connects": self.market_stats.connects,
                        "messages": self.market_stats.messages,
                        "uptimeSec": self.market_stats.uptime_sec,
                    },
                    "public": {
                        "connects": self.public_stats.connects,
                        "messages": self.public_stats.messages,
                        "uptimeSec": self.public_stats.uptime_sec,
                    },
                },
                "maxLoopLagSec": self.max_loop_lag_sec,
            }
        )

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        await self.bootstrap()
        tasks = [
            asyncio.create_task(self.market_loop(), name="market-stream"),
            asyncio.create_task(self.public_loop(), name="public-stream"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self.status_loop(), name="status"),
            asyncio.create_task(self.loop_lag_loop(), name="loop-lag"),
            asyncio.create_task(self.telegram_status_loop(), name="telegram-status"),
        ]
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._interrupt_active_measurements("process_shutdown")
            if self.notification_tasks:
                await asyncio.gather(*self.notification_tasks, return_exceptions=True)

    async def _interrupt_active_measurements(self, reason: str) -> None:
        for trackers in self.outcomes.values():
            for tracker in list(trackers):
                fields = tracker.fields()
                fields.update(
                    {
                        "measurementStatus": "INTERRUPTED",
                        "measurementInterruptedAt": datetime.now(UTC),
                        "measurementInterruptReason": reason,
                    }
                )
                await self.storage.update_signal(tracker.signal_id, fields)
                trackers.remove(tracker)


def _age_text(now_ms: int, event_ms: int | None) -> str:
    if event_ms is None:
        return "n/a"
    return f"{max(0.0, (now_ms - event_ms) / 1000.0):.1f}s"


def format_number(value: float | None) -> str:
    if value is None:
        return "n/a"
    text = f"{float(value):.8f}"
    return text.rstrip("0").rstrip(".")


async def resolve_symbols(http: httpx.AsyncClient, config: StrategyConfig) -> list[str]:
    """Config symbols, or the most liquid perpetuals when auto-populate is on.

    Never returns an empty list: any failure falls back to the configured symbols.
    """
    if not config.symbol_auto_populate:
        logger.info("Symbols from config: %s", ", ".join(config.symbols))
        return config.symbols

    min_volume = config.min_quote_volume_24h
    try:
        universe = await fetch_symbol_universe(http, config.quote_asset)
    except Exception:
        logger.exception("Symbol auto-populate failed, falling back to config symbols")
        return config.symbols

    selected = select_symbols(universe, min_volume, config.max_symbols)
    if not selected:
        logger.warning(
            "No %s perpetual reached minQuoteVolume24h=%.0f (best was %.0f), "
            "falling back to config symbols: %s",
            config.quote_asset,
            min_volume,
            max((volume for _symbol, volume in universe), default=0.0),
            ", ".join(config.symbols),
        )
        return config.symbols

    volumes = dict(universe)
    eligible = sum(1 for _symbol, volume in universe if volume >= min_volume)
    logger.info(
        "Symbol auto-populate: %d of %d %s perpetuals above %.0f %s 24h volume, using %d",
        eligible,
        len(universe),
        config.quote_asset,
        min_volume,
        config.quote_asset,
        len(selected),
    )
    for symbol in selected:
        logger.info("  %-14s 24h quote volume %15.0f %s", symbol, volumes[symbol], config.quote_asset)
    if eligible > len(selected):
        logger.info("  %d further symbol(s) skipped by maxSymbols=%d", eligible - len(selected), config.max_symbols)
    return selected


async def async_main() -> None:
    load_dotenv()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    mongo_uri = require_env("MONGO_URI")
    telegram_token = require_env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = require_env("TELEGRAM_CHAT_ID")

    storage = Storage(mongo_uri)
    http = httpx.AsyncClient(headers={"User-Agent": "binance-shadow-signal/1.0"})
    try:
        config = await storage.initialize()
        telegram = TelegramClient(http, telegram_token, telegram_chat_id)
        symbols = await resolve_symbols(http, config)
        app = ShadowSignalApp(config, storage, http, telegram, symbols)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, app.stop_event.set)
            except NotImplementedError:
                pass

        await app.run()
    finally:
        await http.aclose()
        await storage.close()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


if __name__ == "__main__":
    asyncio.run(async_main())
