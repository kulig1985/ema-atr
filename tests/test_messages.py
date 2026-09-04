from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.messages import (
    band_position,
    interpret_performance,
    format_duration,
    format_price,
    format_signal_message,
    format_status_message,
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


def test_band_position_reports_atr_distance_inside_the_band() -> None:
    assert "ATR-rel a sáv alja felett" in band_position(runtime())


def test_band_position_reports_distance_below_the_band() -> None:
    assert "ATR-rel a sáv alja alatt" in band_position(runtime(price=1.30))


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
    assert "XRPUSDT LONG_ARMED 1.3981" in message
    assert "1 mérés" in message
    assert "SOLUSDT COOLDOWN 101.31 (7m 0s van hátra)" in message
    assert "loop lag max 51 ms" in message


def test_status_message_says_so_when_nothing_happened() -> None:
    assert "Nem volt signal." in status_message(performance=summarize_signals([]))


# --- path quality (MFE/MAE) aggregation and its plain-language reading ---


def measured(**overrides) -> dict:
    base = {
        "symbol": "SOLUSDT",
        "side": "LONG",
        "measurementStatus": "COMPLETED",
        "return20m": 0.2575,
        "MFE": 0.307,
        "MAE": -0.2278,
        "timeToMFE": 1132.6,
        "timeToMAE": 79.9,
    }
    return {**base, **overrides}


def test_summary_aggregates_the_path_metrics() -> None:
    summary = summarize_signals([measured(), measured(MFE=0.5, MAE=-0.1, timeToMFE=600.0)])
    assert summary["avg_mfe"] == pytest.approx(0.4035)
    assert summary["avg_mae"] == pytest.approx(-0.1639)
    assert summary["avg_time_to_mfe"] == pytest.approx(866.3)
    assert summary["worst_mae"] == pytest.approx(-0.2278)
    # Both signals dipped before they peaked.
    assert summary["adverse_first"] == 2


def test_capture_is_the_share_of_the_peak_still_held_at_20_minutes() -> None:
    summary = summarize_signals([measured(return20m=0.15, MFE=0.30)])
    assert summary["capture"] == pytest.approx(0.5)


def test_capture_is_undefined_when_nothing_ever_moved_in_our_favour() -> None:
    assert summarize_signals([measured(MFE=0.0, return20m=-0.1)])["capture"] is None


def test_interpretation_reports_reach_and_drawdown() -> None:
    text = " ".join(interpret_performance(summarize_signals([measured()] * 6)))
    assert "+0.31%-ig jutottak" in text
    assert "-0.23%-ot mentek ellened" in text
    assert "18m 52s" in text  # 1132.6s, truncated by format_duration


def test_interpretation_flags_giving_back_most_of_the_move() -> None:
    signals = [measured(return20m=0.05, MFE=0.60)] * 6
    text = " ".join(interpret_performance(summarize_signals(signals)))
    assert "visszaadták" in text
    assert "20 perc hosszú ablaknak tűnik" in text


def test_interpretation_praises_holding_the_peak() -> None:
    signals = [measured(return20m=0.29, MFE=0.30)] * 6
    text = " ".join(interpret_performance(summarize_signals(signals)))
    assert "csúcs közelében zártak" in text


def test_interpretation_warns_when_drawdown_exceeds_the_reach() -> None:
    signals = [measured(return20m=-0.4, MFE=0.1, MAE=-0.9)] * 6
    text = " ".join(interpret_performance(summarize_signals(signals)))
    assert "nagyobb volt, mint az elért csúcs" in text


def test_interpretation_says_when_the_sample_is_too_small_to_trust() -> None:
    text = " ".join(interpret_performance(summarize_signals([measured()] * 2)))
    assert "kevés minta" in text


def test_no_interpretation_without_completed_measurements() -> None:
    assert interpret_performance(summarize_signals([])) == []


def test_status_message_includes_the_interpretation() -> None:
    message = status_message(performance=summarize_signals([measured()] * 6))
    assert "<b>Mit mutat</b>" in message
    assert "jutottak" in message
