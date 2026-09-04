from __future__ import annotations

import pytest

from app.config import DEFAULT_CONFIG, StrategyConfig, validate_config_document


def document(**overrides) -> dict:
    return {**DEFAULT_CONFIG, **overrides}


def test_default_config_is_valid() -> None:
    validate_config_document(document())


def test_global_keys_do_not_leak_into_per_symbol_settings() -> None:
    settings = StrategyConfig(document=document()).for_symbol("BTCUSDT")
    for key in ("logStatusSec", "symbolAutoPopulate", "minQuoteVolume24h", "maxSymbols"):
        assert key not in settings


def test_symbol_overrides_still_apply_on_top_of_the_globals() -> None:
    config = StrategyConfig(document=document(symbolOverrides={"BTCUSDT": {"xEntry": 2.5}}))
    assert config.for_symbol("BTCUSDT")["xEntry"] == 2.5
    assert config.for_symbol("ETHUSDT")["xEntry"] == DEFAULT_CONFIG["xEntry"]


def test_the_new_global_keys_cannot_be_overridden_per_symbol() -> None:
    with pytest.raises(ValueError, match="unsupported override fields"):
        validate_config_document(document(symbolOverrides={"BTCUSDT": {"maxSymbols": 3}}))


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"logStatusSec": 0}, "logStatusSec must be > 0"),
        ({"symbolAutoPopulate": "yes"}, "symbolAutoPopulate must be a boolean"),
        ({"minQuoteVolume24h": -1}, "minQuoteVolume24h must be >= 0"),
        ({"maxSymbols": 0}, "maxSymbols must be >= 1"),
    ],
)
def test_rejects_invalid_values_for_the_new_keys(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_config_document(document(**overrides))


def test_reads_the_new_keys_through_typed_properties() -> None:
    config = StrategyConfig(
        document=document(logStatusSec=30, symbolAutoPopulate=True, minQuoteVolume24h=1e8, maxSymbols=7)
    )
    assert config.log_status_sec == 30
    assert config.symbol_auto_populate is True
    assert config.min_quote_volume_24h == 1e8
    assert config.max_symbols == 7
