from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "_id": "strategy",
    "symbols": ["BTCUSDT", "ETHUSDT"],
    "entryTimeframe": "15m",
    "exitTimeframe": "1h",
    "emaPeriod": 20,
    "atrPeriod": 14,
    "xEntry": 1.75,
    "xExit": 1.75,
    "cvdLookback": 5,
    "maxSpreadBps": 10,
    "bookMaxAgeSec": 3,
    "tradeMaxAgeSec": 3,
    "cooldownSec": 600,
    "heartbeatSec": 3600,
    "symbolOverrides": {},
    "logStatusSec": 60,
    "symbolAutoPopulate": True,
    "quoteAsset": "USDT",
    "minQuoteVolume24h": 500_000_000,
    "maxSymbols": 5,
    "updatedAt": None,
}

# Keys that stay global: they are never merged into per-symbol settings and
# cannot appear in symbolOverrides.
GLOBAL_ONLY_KEYS = {
    "_id",
    "symbols",
    "symbolOverrides",
    "updatedAt",
    "heartbeatSec",
    "logStatusSec",
    "symbolAutoPopulate",
    "quoteAsset",
    "minQuoteVolume24h",
    "maxSymbols",
}

OVERRIDABLE_KEYS = {
    "emaPeriod",
    "atrPeriod",
    "xEntry",
    "xExit",
    "cvdLookback",
    "maxSpreadBps",
    "bookMaxAgeSec",
    "tradeMaxAgeSec",
    "cooldownSec",
}


def interval_to_ms(interval: str) -> int:
    if len(interval) < 2:
        raise ValueError(f"Invalid timeframe: {interval}")
    unit = interval[-1]
    try:
        value = int(interval[:-1])
    except ValueError as exc:
        raise ValueError(f"Invalid timeframe: {interval}") from exc
    if value <= 0:
        raise ValueError(f"Invalid timeframe: {interval}")
    if unit == "m":
        return value * 60_000
    if unit == "h":
        return value * 3_600_000
    raise ValueError(f"Unsupported timeframe: {interval}")


@dataclass(frozen=True)
class StrategyConfig:
    document: dict[str, Any]

    @property
    def symbols(self) -> list[str]:
        return [str(symbol).upper() for symbol in self.document["symbols"]]

    @property
    def heartbeat_sec(self) -> int:
        return int(self.document["heartbeatSec"])

    @property
    def log_status_sec(self) -> int:
        return int(self.document["logStatusSec"])

    @property
    def symbol_auto_populate(self) -> bool:
        return bool(self.document["symbolAutoPopulate"])

    @property
    def quote_asset(self) -> str:
        return str(self.document["quoteAsset"]).upper()

    @property
    def min_quote_volume_24h(self) -> float:
        return float(self.document["minQuoteVolume24h"])

    @property
    def max_symbols(self) -> int:
        return int(self.document["maxSymbols"])

    def for_symbol(self, symbol: str) -> dict[str, Any]:
        settings = {
            key: deepcopy(value)
            for key, value in self.document.items()
            if key not in GLOBAL_ONLY_KEYS
        }
        overrides = self.document.get("symbolOverrides", {}).get(symbol.upper(), {})
        for key, value in overrides.items():
            if key not in OVERRIDABLE_KEYS:
                raise ValueError(f"Unsupported symbol override key for {symbol}: {key}")
            settings[key] = deepcopy(value)
        validate_symbol_settings(symbol, settings)
        return settings


def validate_config_document(document: dict[str, Any]) -> None:
    required = set(DEFAULT_CONFIG.keys())
    missing = required - set(document.keys())
    if missing:
        raise ValueError(f"Missing config fields: {sorted(missing)}")
    if document.get("_id") != "strategy":
        raise ValueError('Config document _id must be "strategy"')
    symbols = document.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("symbols must be a non-empty list")
    if len(set(str(s).upper() for s in symbols)) != len(symbols):
        raise ValueError("symbols contains duplicates")
    if not isinstance(document.get("symbolOverrides"), dict):
        raise ValueError("symbolOverrides must be an object")
    for symbol, overrides in document.get("symbolOverrides", {}).items():
        if not isinstance(overrides, dict):
            raise ValueError(f"{symbol}: symbol override must be an object")
        unsupported = set(overrides) - OVERRIDABLE_KEYS
        if unsupported:
            raise ValueError(f"{symbol}: unsupported override fields: {sorted(unsupported)}")
    if int(document["heartbeatSec"]) <= 0:
        raise ValueError("heartbeatSec must be > 0")
    if int(document["logStatusSec"]) <= 0:
        raise ValueError("logStatusSec must be > 0")
    if not isinstance(document["symbolAutoPopulate"], bool):
        raise ValueError("symbolAutoPopulate must be a boolean")
    quote_asset = document["quoteAsset"]
    if not isinstance(quote_asset, str) or not quote_asset.strip():
        raise ValueError("quoteAsset must be a non-empty string")
    if float(document["minQuoteVolume24h"]) < 0:
        raise ValueError("minQuoteVolume24h must be >= 0")
    if int(document["maxSymbols"]) < 1:
        raise ValueError("maxSymbols must be >= 1")
    base = {
        key: document[key]
        for key in DEFAULT_CONFIG
        if key not in GLOBAL_ONLY_KEYS
    }
    for symbol in symbols:
        settings = dict(base)
        settings["cooldownSec"] = document["cooldownSec"]
        settings.update(document.get("symbolOverrides", {}).get(str(symbol).upper(), {}))
        validate_symbol_settings(str(symbol).upper(), settings)


def validate_symbol_settings(symbol: str, settings: dict[str, Any]) -> None:
    interval_to_ms(str(settings["entryTimeframe"]))
    interval_to_ms(str(settings["exitTimeframe"]))
    if int(settings["emaPeriod"]) < 2:
        raise ValueError(f"{symbol}: emaPeriod must be >= 2")
    if int(settings["atrPeriod"]) < 2:
        raise ValueError(f"{symbol}: atrPeriod must be >= 2")
    if float(settings["xEntry"]) <= 0:
        raise ValueError(f"{symbol}: xEntry must be > 0")
    if float(settings["xExit"]) <= 0:
        raise ValueError(f"{symbol}: xExit must be > 0")
    if int(settings["cvdLookback"]) < 3:
        raise ValueError(f"{symbol}: cvdLookback must be >= 3 for quadratic curvature")
    if float(settings["maxSpreadBps"]) < 0:
        raise ValueError(f"{symbol}: maxSpreadBps must be >= 0")
    if float(settings["bookMaxAgeSec"]) <= 0:
        raise ValueError(f"{symbol}: bookMaxAgeSec must be > 0")
    if float(settings["tradeMaxAgeSec"]) <= 0:
        raise ValueError(f"{symbol}: tradeMaxAgeSec must be > 0")
    if int(settings["cooldownSec"]) < 0:
        raise ValueError(f"{symbol}: cooldownSec must be >= 0")
