from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from websockets.asyncio.client import connect as websocket_connect

from .models import Candle

logger = logging.getLogger(__name__)

REST_BASE = "https://fapi.binance.com"
MARKET_STREAM_URL = "wss://fstream.binance.com/market/stream"
PUBLIC_STREAM_URL = "wss://fstream.binance.com/public/stream"

EventHandler = Callable[[dict], Awaitable[None]]

SILENT_STREAM_WARN_SEC = 15.0


@dataclass
class StreamStats:
    """Live counters for one WebSocket connection, read by the status logger."""

    name: str
    connects: int = 0
    messages: int = 0
    connected_at: float | None = field(default=None)
    last_message_at: float | None = field(default=None)

    def on_connect(self) -> None:
        self.connects += 1
        self.connected_at = time.monotonic()
        self.messages = 0
        self.last_message_at = None

    def on_message(self) -> None:
        self.messages += 1
        self.last_message_at = time.monotonic()

    @property
    def uptime_sec(self) -> float:
        if self.connected_at is None:
            return 0.0
        return time.monotonic() - self.connected_at

    @property
    def messages_per_sec(self) -> float | None:
        """None until a full second of uptime makes the rate meaningful."""
        uptime = self.uptime_sec
        return self.messages / uptime if uptime >= 1.0 else None

    @property
    def silent_sec(self) -> float | None:
        reference = self.last_message_at or self.connected_at
        if reference is None:
            return None
        return time.monotonic() - reference

    def summary(self) -> str:
        rate = self.messages_per_sec
        rate_text = f"{rate:.1f}/s" if rate is not None else "n/a"
        return f"{self.uptime_sec:.1f}s, {self.messages} msgs, {rate_text}"


async def _warn_if_silent(stats: StreamStats) -> None:
    """Say so when a freshly subscribed connection delivers nothing."""
    await asyncio.sleep(SILENT_STREAM_WARN_SEC)
    if stats.messages == 0:
        logger.warning(
            "%s received no data %.0fs after SUBSCRIBE - the streams may be wrong or filtered",
            stats.name,
            SILENT_STREAM_WARN_SEC,
        )


async def fetch_closed_klines(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    limit: int = 500,
) -> list[Candle]:
    response = await client.get(
        f"{REST_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=15.0,
    )
    response.raise_for_status()
    now_ms = int(time.time() * 1000)
    candles: list[Candle] = []
    for row in response.json():
        close_time = int(row[6])
        if close_time >= now_ms:
            continue
        candles.append(
            Candle(
                symbol=symbol,
                timeframe=interval,
                open_time=int(row[0]),
                close_time=close_time,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    return candles


async def _subscribe_loop(
    url: str,
    streams: list[str],
    handler: EventHandler,
    stats: StreamStats,
) -> None:
    request_id = uuid.uuid4().hex
    request = {"method": "SUBSCRIBE", "params": streams, "id": request_id}
    async with websocket_connect(
        url,
        ping_interval=30,
        ping_timeout=10,
        close_timeout=5,
        max_queue=4096,
    ) as websocket:
        await websocket.send(json.dumps(request))
        stats.on_connect()
        logger.info("%s connected to %s, subscribing to %d streams", stats.name, url, len(streams))
        logger.debug("%s streams: %s", stats.name, ", ".join(streams))
        watchdog = asyncio.create_task(_warn_if_silent(stats))
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if "id" in message and message.get("id") == request_id:
                    if message.get("result", "missing") is None:
                        logger.info("%s SUBSCRIBE acknowledged", stats.name)
                    else:
                        logger.error("%s SUBSCRIBE rejected: %s", stats.name, message)
                    continue
                if "result" in message and "id" in message:
                    continue
                if message.get("error"):
                    logger.error("%s stream error message: %s", stats.name, message)
                    continue
                data = message.get("data", message)
                if isinstance(data, dict):
                    stats.on_message()
                    await handler(data)
        finally:
            watchdog.cancel()


def market_streams(symbols: list[str], entry_timeframe: str, exit_timeframe: str) -> list[str]:
    result: list[str] = []
    for symbol in symbols:
        s = symbol.lower()
        result.append(f"{s}@aggTrade")
        result.append(f"{s}@kline_{entry_timeframe}")
        if exit_timeframe != entry_timeframe:
            result.append(f"{s}@kline_{exit_timeframe}")
    return result


def public_streams(symbols: list[str]) -> list[str]:
    return [f"{symbol.lower()}@bookTicker" for symbol in symbols]


async def run_market_stream(
    symbols: list[str],
    entry_timeframe: str,
    exit_timeframe: str,
    handler: EventHandler,
    stats: StreamStats,
) -> None:
    await _subscribe_loop(
        MARKET_STREAM_URL,
        market_streams(symbols, entry_timeframe, exit_timeframe),
        handler,
        stats,
    )


async def run_public_stream(symbols: list[str], handler: EventHandler, stats: StreamStats) -> None:
    await _subscribe_loop(PUBLIC_STREAM_URL, public_streams(symbols), handler, stats)


async def fetch_symbol_universe(
    client: httpx.AsyncClient,
    quote_asset: str = "USDT",
) -> list[tuple[str, float]]:
    """Tradable perpetuals of one quote asset, with their 24h quote volume."""
    info_response, ticker_response = await asyncio.gather(
        client.get(f"{REST_BASE}/fapi/v1/exchangeInfo", timeout=20.0),
        client.get(f"{REST_BASE}/fapi/v1/ticker/24hr", timeout=20.0),
    )
    info_response.raise_for_status()
    ticker_response.raise_for_status()

    wanted_quote = str(quote_asset).upper()
    tradable = {
        str(item["symbol"]).upper()
        for item in info_response.json().get("symbols", [])
        if item.get("contractType") == "PERPETUAL"
        and item.get("status") == "TRADING"
        and str(item.get("quoteAsset", "")).upper() == wanted_quote
    }
    universe: list[tuple[str, float]] = []
    for row in ticker_response.json():
        symbol = str(row.get("symbol", "")).upper()
        if symbol in tradable:
            universe.append((symbol, float(row.get("quoteVolume", 0.0))))
    return universe
