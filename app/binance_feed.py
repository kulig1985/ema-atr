from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

import httpx
from websockets.asyncio.client import connect as websocket_connect

from .models import Candle

logger = logging.getLogger(__name__)

REST_BASE = "https://fapi.binance.com"
MARKET_STREAM_URL = "wss://fstream.binance.com/market/stream"
PUBLIC_STREAM_URL = "wss://fstream.binance.com/public/stream"

EventHandler = Callable[[dict], Awaitable[None]]


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
    connection_name: str,
) -> None:
    request = {
        "method": "SUBSCRIBE",
        "params": streams,
        "id": uuid.uuid4().hex,
    }
    async with websocket_connect(
        url,
        ping_interval=30,
        ping_timeout=10,
        close_timeout=5,
        max_queue=4096,
    ) as websocket:
        await websocket.send(json.dumps(request))
        logger.info("%s connected with %d streams", connection_name, len(streams))
        async for raw in websocket:
            message = json.loads(raw)
            if "result" in message and "id" in message:
                continue
            data = message.get("data", message)
            if isinstance(data, dict):
                await handler(data)


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
) -> None:
    await _subscribe_loop(
        MARKET_STREAM_URL,
        market_streams(symbols, entry_timeframe, exit_timeframe),
        handler,
        "Binance market stream",
    )


async def run_public_stream(symbols: list[str], handler: EventHandler) -> None:
    await _subscribe_loop(
        PUBLIC_STREAM_URL,
        public_streams(symbols),
        handler,
        "Binance public stream",
    )
