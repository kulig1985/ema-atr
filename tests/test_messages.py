from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.messages import (
    band_distance_atr,
    checkpoint_price,
    format_completed_signal_block,
    format_signal_detail_messages,
    format_duration,
    format_price,
    format_signal_message,
    format_status_message,
    render_table,
    summarize_signals,
    waiting_for,
)
from app.models import SymbolRuntime

UTC = timezone.utc
NOW = datetime(2026, 9, 4, 16, 0, tzinfo=UTC)

SETTINGS = {
    "entryTimeframe": "15m",
    "exitTimeframe": "1h",
    "xEntry": 1.75,
    "xExit": 1.75,
    "maxSpreadBps": 10,
    "cvdLookback": 5,
}

VALIDATION = {
    "candleOpen": 1.3965,
    "vwap": 1.39682956,
    "cvdSlope": 0.496481,
    "cvdCurvature": 0.384609,
    "spreadBps": 0.715,
    "tradeAgeSec": 0.118,
    "bookAgeSec": 0.142,
}


def runtime(state: str = "LONG_ARMED", price: float = 1.3981) -> SymbolRuntime:
    rt = SymbolRuntime(symbol="XRPUSDT", settings=dict(SETTINGS), state=state)
    rt.entry_ema, rt.entry_atr = 1.41801105, 0.01141185
    rt.exit_ema, rt.exit_atr = 1.42813172, 0.01935594
    rt.last_price = price
    return rt


@pytest.mark.parametrize(
    "value, expected",
    [
        (79116.67319415, "79116.67"),
        (2449.92, "2449.92"),
        (101.31, "101.31"),
        (1.39668062, "1.396681"),
        (0.01141185, "0.01141185"),
        (0.0, "0"),
        (None, "n/a"),
    ],
)
def test_price_precision_follows_magnitude(value, expected) -> None:
    assert format_price(value) == expected


@pytest.mark.parametrize(
    "seconds, expected", [(45, "45s"), (150, "2m 30s"), (8040, "2h 14m"), (-5, "0s")]
)
def test_duration_is_human_readable(seconds, expected) -> None:
    assert format_duration(seconds) == expected


def test_waiting_for_stays_english_because_it_goes_to_the_log() -> None:
    assert "cross up through" in waiting_for(runtime("LONG_ARMED"))
    assert "cross down through" in waiting_for(runtime("SHORT_ARMED"))
    assert waiting_for(runtime("IDLE")) == "price to leave the entry band"
    assert waiting_for(runtime("COOLDOWN")) == "cooldown to expire"


def test_signal_message_explains_every_validation_condition() -> None:
    message = format_signal_message(runtime(), "LONG", 1.3981, NOW, 1.46573117, VALIDATION)
    assert "🟢 <b>LONG · XRPUSDT</b>" in message
    assert "<b>Miért</b>" in message
    # lowerEntry = 1.41801105 - 1.75 * 0.01141185 = 1.39804
    assert "alulról átlépte a 15m sáv alját (1.39804," in message
    # The VWAP line must read in the order the condition actually requires.
    assert "nyitás 1.3965 &lt; VWAP 1.39683 &lt; ár 1.3981" in message
    assert "meredekség +0.4965" in message
    assert "görbület +0.3846" in message
    assert "gyorsuló vételi nyomás" in message
    assert "Spread 0.71 bps (max 10)" in message
    assert "trade 0.1s" in message
    assert "1h kilépési iránymutató: <b>1.465731</b>" in message
    assert "Nem küld ordert" in message


def test_short_signal_message_flips_direction_and_icon() -> None:
    message = format_signal_message(runtime("SHORT_ARMED"), "SHORT", 1.44, NOW, 1.39, VALIDATION)
    assert message.startswith("🔴 <b>SHORT · XRPUSDT</b>")
    # upperEntry = 1.41801105 + 1.75 * 0.01141185 = 1.437982
    assert "felülről átlépte a 15m sáv tetejét (1.437982, +0.14%)" in message
    assert "ár 1.44 &lt; VWAP 1.39683 &lt; nyitás 1.3965" in message
    assert "gyorsuló eladói nyomás" in message


def signal(**overrides) -> dict:
    base = {
        "symbol": "SOLUSDT",
        "side": "LONG",
        "measurementStatus": "COMPLETED",
        "return20m": 0.25,
        "MFE": 0.3,
        "MAE": -0.1,
        "timeToMFE": 600.0,
    }
    return {**base, **overrides}


def test_summary_counts_sides_and_measurement_states() -> None:
    summary = summarize_signals(
        [
            signal(),
            signal(symbol="XRPUSDT", return20m=-0.1),
            signal(side="SHORT", measurementStatus="ACTIVE", return20m=None),
            signal(measurementStatus="INTERRUPTED", return20m=None),
        ]
    )
    assert summary["total"] == 4
    assert summary["long"] == 3
    assert summary["short"] == 1
    assert summary["active"] == 1
    assert summary["interrupted"] == 1
    assert summary["measured"] == 2
    assert summary["positive"] == 1
    assert summary["negative"] == 1
    assert summary["avg_return"] == pytest.approx(0.075)
    assert summary["best"] == (0.25, "SOLUSDT")
    assert summary["worst"] == (-0.1, "XRPUSDT")


def test_summary_of_no_signals_has_no_averages() -> None:
    summary = summarize_signals([])
    assert summary["total"] == 0
    assert summary["avg_return"] is None
    assert summary["best"] is None


def status_message(**overrides) -> str:
    rt_armed = runtime("LONG_ARMED")
    rt_cooldown = runtime("COOLDOWN", price=101.31)
    rt_cooldown.symbol = "SOLUSDT"
    rt_cooldown.cooldown_until = NOW + timedelta(seconds=420)
    kwargs = {
        "now": NOW,
        "uptime_sec": 8040,
        "runtimes": {"XRPUSDT": rt_armed, "SOLUSDT": rt_cooldown},
        "active_measurements": {"XRPUSDT": 1, "SOLUSDT": 0},
        "market_summary": "4118.7s, 358555 msgs, 87.1/s",
        "public_summary": "4273.1s, 6028889 msgs, 1410.9/s",
        "max_loop_lag_sec": 0.051,
        "performance": summarize_signals([signal(), signal(symbol="XRPUSDT", return20m=-0.1)]),
    }
    kwargs.update(overrides)
    return format_status_message(**kwargs)


def test_status_message_reports_performance_symbols_and_feeds() -> None:
    message = status_message()
    assert "2h 14m óta fut · 2 symbol" in message
    assert "2 signal (2 LONG / 0 SHORT)" in message
    assert "átlagos 20 perces hozam +0.07%" in message
    assert "legjobb +0.25% SOLUSDT" in message
    assert "XRPUSDT" in message and "LONG_ARMED" in message and "1.3981" in message
    assert "SOLUSDT" in message and "COOLDOWN" in message and "7m 0s" in message
    assert "loop lag max 51 ms" in message
    assert "Mit mutat" not in message


def test_status_message_says_so_when_nothing_happened() -> None:
    assert "Nem volt signal." in status_message(performance=summarize_signals([]))


def test_status_message_shows_the_checkpoint_table_from_completed_signals() -> None:
    completed = [
        signal(return1m=0.1, return3m=0.2, return20m=0.2575),
        signal(symbol="XRPUSDT", return1m=-0.05, return3m=0.1, return20m=-0.1),
    ]
    message = status_message(performance=summarize_signals(completed))
    assert "Összesített lefutás" in message
    assert "<pre>" in message
    assert "1m" in message and "3m" in message and "20m" in message
    assert "MFE átlag" in message
    assert "MAE átlag" in message


def test_status_message_uses_real_state_names_only() -> None:
    message = status_message()
    for fake_state in ("WAIT", "SHORT_A"):
        assert fake_state not in message
    for real_state in ("LONG_ARMED", "COOLDOWN"):
        assert real_state in message


def test_checkpoint_price_round_trips_through_directional_return_pct() -> None:
    from app.indicators import directional_return_pct

    for side, price, actual in (("LONG", 100.0, 101.3), ("SHORT", 100.0, 97.5)):
        r = directional_return_pct(side, price, actual)
        rebuilt = checkpoint_price(price, side, r)
        assert rebuilt == pytest.approx(actual)


def test_checkpoint_price_is_none_without_a_stored_return() -> None:
    assert checkpoint_price(100.0, "LONG", None) is None


def test_summarize_signals_builds_checkpoint_averages_from_completed_only() -> None:
    signals = [
        signal(return1m=0.2, measurementStatus="COMPLETED"),
        signal(return1m=0.4, measurementStatus="COMPLETED"),
        signal(return1m=99.0, measurementStatus="ACTIVE"),
    ]
    checkpoints = summarize_signals(signals)["checkpoints"]
    one_minute = next(c for c in checkpoints if c["label"] == "1m")
    assert one_minute["count"] == 2
    assert one_minute["avg"] == pytest.approx(0.3)
    assert one_minute["positive"] == 2
    assert one_minute["negative"] == 0


def test_summarize_signals_checkpoint_ignores_missing_values() -> None:
    signals = [signal(return1m=0.5), signal(return1m=None)]
    one_minute = next(c for c in summarize_signals(signals)["checkpoints"] if c["label"] == "1m")
    assert one_minute["count"] == 1
    assert one_minute["avg"] == pytest.approx(0.5)


def test_render_table_aligns_columns_without_box_drawing() -> None:
    table = render_table(["A", "BB"], [["1", "22"], ["333", "4"]])
    assert table.startswith("<pre>")
    assert table.endswith("</pre>")
    for forbidden in ("│", "┃", "┌", "└", "|"):
        assert forbidden not in table


def test_band_distance_atr_reports_signed_distance_from_the_lower_edge() -> None:
    # lowerEntry = 1.39804, price 1.3981 -> tiny positive distance in ATR units
    assert band_distance_atr(runtime()) == pytest.approx(0.0052, abs=0.001)
    assert band_distance_atr(runtime(price=1.30)) < 0


def test_band_distance_atr_is_none_without_a_band() -> None:
    rt = runtime()
    rt.entry_atr = None
    assert band_distance_atr(rt) is None


def completed_signal(**overrides) -> dict:
    base = {
        "symbol": "SOLUSDT",
        "side": "LONG",
        "signalAt": NOW,
        "signalPrice": 100.0,
        "measurementStatus": "COMPLETED",
        "return1m": 0.1,
        "return3m": 0.2,
        "return5m": 0.15,
        "return10m": 0.3,
        "return15m": 0.28,
        "return20m": 0.2575,
        "MFE": 0.307,
        "MAE": -0.2278,
        "timeToMFE": 1132.6,
        "timeToMAE": 79.9,
    }
    return {**base, **overrides}


def test_completed_signal_block_shows_the_reconstructed_prices() -> None:
    block = format_completed_signal_block(completed_signal())
    assert "SOLUSDT" in block and "LONG" in block
    assert "100" in block
    assert "MFE +0.31%" in block
    assert "MAE -0.23%" in block
    assert "<pre>" in block


def test_completed_signal_block_shows_dash_for_a_missing_checkpoint() -> None:
    block = format_completed_signal_block(completed_signal(return15m=None))
    lines = [l for l in block.split("\n") if l.strip().startswith("15m")]
    assert len(lines) == 1
    # Price and return both fall back to "-" when the checkpoint was never stored.
    assert lines[0].split() == ["15m", "-", "-"]


def test_detail_messages_include_every_completed_signal() -> None:
    signals = [completed_signal(symbol="A"), completed_signal(symbol="B", side="SHORT")]
    messages = format_signal_detail_messages(signals)
    joined = "\n".join(messages)
    assert "A" in joined and "B" in joined
    assert "SHORT" in joined


def test_detail_messages_exclude_active_and_interrupted() -> None:
    signals = [
        completed_signal(symbol="DONE"),
        {**completed_signal(symbol="RUNNING"), "measurementStatus": "ACTIVE"},
        {**completed_signal(symbol="GAP"), "measurementStatus": "INTERRUPTED"},
    ]
    joined = "\n".join(format_signal_detail_messages(signals))
    assert "DONE" in joined
    assert "RUNNING" not in joined
    assert "GAP" not in joined


def test_detail_messages_return_empty_list_without_completed_signals() -> None:
    assert format_signal_detail_messages([{**completed_signal(), "measurementStatus": "ACTIVE"}]) == []


def test_detail_messages_never_split_a_single_signal_block() -> None:
    many = [completed_signal(symbol=f"SYM{i}") for i in range(40)]
    messages = format_signal_detail_messages(many, limit=800, max_messages=40)
    assert len(messages) > 1
    for message in messages:
        assert len(message) <= 900  # header + slack, never far over the limit
    joined = "\n".join(messages)
    for i in range(40):
        assert f"SYM{i}" in joined


def test_detail_messages_respect_the_telegram_size_limit() -> None:
    many = [completed_signal(symbol=f"SYM{i}") for i in range(200)]
    messages = format_signal_detail_messages(many)
    for message in messages:
        assert len(message) <= 4096


def test_detail_messages_cap_the_message_count_and_report_the_overflow() -> None:
    many = [completed_signal(symbol=f"SYM{i}") for i in range(500)]
    messages = format_signal_detail_messages(many, limit=500, max_messages=3)
    assert len(messages) == 3
    assert "további lefutás nem fért ki" in messages[-1]
