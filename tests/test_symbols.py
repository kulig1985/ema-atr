from __future__ import annotations

from app.symbols import select_symbols


def test_keeps_only_symbols_above_the_volume_threshold() -> None:
    universe = [("BTCUSDT", 5_000.0), ("XRPUSDT", 900.0), ("ETHUSDT", 1_000.0)]
    assert select_symbols(universe, min_quote_volume=1_000.0, max_symbols=10) == [
        "BTCUSDT",
        "ETHUSDT",
    ]


def test_orders_by_quote_volume_descending() -> None:
    universe = [("ETHUSDT", 2_000.0), ("BTCUSDT", 9_000.0), ("SOLUSDT", 5_000.0)]
    assert select_symbols(universe, min_quote_volume=0.0, max_symbols=10) == [
        "BTCUSDT",
        "SOLUSDT",
        "ETHUSDT",
    ]


def test_caps_the_result_at_max_symbols_keeping_the_largest() -> None:
    universe = [("A", 1.0), ("B", 3.0), ("C", 2.0), ("D", 4.0)]
    assert select_symbols(universe, min_quote_volume=0.0, max_symbols=2) == ["D", "B"]


def test_returns_empty_list_when_nothing_reaches_the_threshold() -> None:
    universe = [("BTCUSDT", 10.0), ("ETHUSDT", 20.0)]
    assert select_symbols(universe, min_quote_volume=1_000.0, max_symbols=10) == []


def test_threshold_is_inclusive() -> None:
    assert select_symbols([("BTCUSDT", 1_000.0)], min_quote_volume=1_000.0, max_symbols=10) == [
        "BTCUSDT"
    ]


def test_symbols_are_normalized_to_uppercase() -> None:
    assert select_symbols([("btcusdt", 5.0)], min_quote_volume=0.0, max_symbols=10) == ["BTCUSDT"]


def test_stream_stats_reports_no_rate_before_a_second_of_uptime() -> None:
    from app.binance_feed import StreamStats

    stats = StreamStats("test stream")
    stats.on_connect()
    stats.on_message()
    assert stats.messages_per_sec is None
    assert "n/a" in stats.summary()


class FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


class FakeHttp:
    """Serves canned exchangeInfo / ticker payloads, or raises for the failure path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def get(self, url: str, **kwargs):
        if self.fail:
            raise RuntimeError("binance unreachable")
        if url.endswith("/fapi/v1/exchangeInfo"):
            return FakeResponse(
                {
                    "symbols": [
                        {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
                        {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
                        {"symbol": "SOLUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
                        {"symbol": "BTCUSDC", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDC"},
                        {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "status": "SETTLING", "quoteAsset": "USDT"},
                        {"symbol": "BTCUSDT_240628", "contractType": "CURRENT_QUARTER", "status": "TRADING", "quoteAsset": "USDT"},
                    ]
                }
            )
        return FakeResponse(
            [
                {"symbol": "BTCUSDT", "quoteVolume": "9000"},
                {"symbol": "ETHUSDT", "quoteVolume": "5000"},
                {"symbol": "SOLUSDT", "quoteVolume": "100"},
                {"symbol": "BTCUSDC", "quoteVolume": "8000"},
                {"symbol": "OLDUSDT", "quoteVolume": "7000"},
                {"symbol": "BTCUSDT_240628", "quoteVolume": "6000"},
            ]
        )


def _universe():
    import asyncio

    from app.binance_feed import fetch_symbol_universe

    return asyncio.run(fetch_symbol_universe(FakeHttp()))


def test_universe_keeps_only_tradable_usdt_perpetuals() -> None:
    assert sorted(symbol for symbol, _volume in _universe()) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _resolve(document: dict, http) -> list[str]:
    import asyncio

    from app.config import DEFAULT_CONFIG, StrategyConfig
    from app.main import resolve_symbols

    config = StrategyConfig(document={**DEFAULT_CONFIG, **document})
    return asyncio.run(resolve_symbols(http, config))


def test_auto_populate_disabled_uses_the_configured_symbols() -> None:
    assert _resolve({"symbolAutoPopulate": False}, FakeHttp()) == ["BTCUSDT", "ETHUSDT"]


def test_auto_populate_picks_the_most_liquid_symbols() -> None:
    resolved = _resolve(
        {"symbolAutoPopulate": True, "minQuoteVolume24h": 1_000, "maxSymbols": 2}, FakeHttp()
    )
    assert resolved == ["BTCUSDT", "ETHUSDT"]


def test_auto_populate_falls_back_when_the_threshold_excludes_everything() -> None:
    resolved = _resolve(
        {"symbolAutoPopulate": True, "minQuoteVolume24h": 1e12, "symbols": ["XRPUSDT"]}, FakeHttp()
    )
    assert resolved == ["XRPUSDT"]


def test_auto_populate_falls_back_when_binance_is_unreachable() -> None:
    resolved = _resolve(
        {"symbolAutoPopulate": True, "symbols": ["XRPUSDT"]}, FakeHttp(fail=True)
    )
    assert resolved == ["XRPUSDT"]
