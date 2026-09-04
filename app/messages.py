"""Human-readable Telegram messages. Pure functions, no I/O."""

from __future__ import annotations

import html
import math
from datetime import datetime, timedelta
from typing import Any

from .models import SymbolRuntime

STATS_WINDOW = timedelta(hours=24)


def format_price(value: float | None) -> str:
    """Round to a magnitude-appropriate precision instead of eight decimals."""
    if value is None:
        return "n/a"
    number = float(value)
    if number == 0:
        return "0"
    magnitude = int(math.floor(math.log10(abs(number))))
    decimals = min(8, max(0, 6 - magnitude))
    return f"{number:.{decimals}f}".rstrip("0").rstrip(".")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def band_position(rt: SymbolRuntime) -> str:
    """Where the price sits relative to the entry band, in ATR units."""
    price, lower, upper, atr = rt.last_price, rt.lower_entry, rt.upper_entry, rt.entry_atr
    if price is None or lower is None or upper is None or not atr:
        return "no band yet"
    if price < lower:
        return f"{(lower - price) / atr:.2f} ATR below lower band"
    if price > upper:
        return f"{(price - upper) / atr:.2f} ATR above upper band"
    return f"{(price - lower) / atr:+.2f} ATR above lower band"


def waiting_for(rt: SymbolRuntime) -> str:
    """What has to happen next for this symbol, in plain words."""
    if rt.state == "IDLE":
        return "price to leave the entry band"
    if rt.state == "LONG_ARMED":
        return f"re-entry cross up through {format_price(rt.lower_entry)}"
    if rt.state == "SHORT_ARMED":
        return f"re-entry cross down through {format_price(rt.upper_entry)}"
    return "cooldown to expire"


def format_signal_message(
    rt: SymbolRuntime,
    side: str,
    price: float,
    signal_at: datetime,
    exit_guideline: float,
    validation: dict[str, Any],
) -> str:
    """The Telegram signal, explaining why it fired."""
    icon = "🟢" if side == "LONG" else "🔴"
    band_edge = rt.lower_entry if side == "LONG" else rt.upper_entry
    crossed_pct = (price - band_edge) / band_edge * 100.0 if band_edge else 0.0
    direction = "above" if side == "LONG" else "below"
    cvd_note = "buy pressure accelerating" if side == "LONG" else "sell pressure accelerating"
    entry_tf = html.escape(str(rt.settings["entryTimeframe"]))
    exit_tf = html.escape(str(rt.settings["exitTimeframe"]))

    lines = [
        f"{icon} <b>{html.escape(side)} · {html.escape(rt.symbol)}</b>",
        f"Price <b>{format_price(price)}</b> · {signal_at:%H:%M:%S} UTC",
        "",
        "<b>Why now</b>",
        f"• Re-entry: price crossed back {direction} the {entry_tf} band edge "
        f"{format_price(band_edge)} ({crossed_pct:+.2f}%)",
        f"• VWAP: open {format_price(validation['candleOpen'])}"
        f" · vwap {format_price(validation['vwap'])} · price {format_price(price)}",
        f"• CVD: slope {validation['cvdSlope']:+.4f}, curvature {validation['cvdCurvature']:+.4f}"
        f" ({cvd_note})",
        f"• Spread {validation['spreadBps']:.2f} bps (max {float(rt.settings['maxSpreadBps']):.0f})",
        f"• Data age: trade {validation['tradeAgeSec']:.1f}s · book {validation['bookAgeSec']:.1f}s",
        "",
        "<b>Levels</b>",
        f"• {entry_tf} EMA {format_price(rt.entry_ema)} · ATR {format_price(rt.entry_atr)}"
        f" · x {float(rt.settings['xEntry']):g}",
        f"• Entry band {format_price(rt.lower_entry)} – {format_price(rt.upper_entry)}",
        f"• {exit_tf} exit guideline <b>{format_price(exit_guideline)}</b>"
        f" (EMA {format_price(rt.exit_ema)}, ATR {format_price(rt.exit_atr)},"
        f" x {float(rt.settings['xExit']):g})",
        "",
        f"<i>No order is sent. Outcome is measured for 20 minutes.</i>",
    ]
    return "\n".join(lines)


def summarize_signals(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate recent signal documents into the numbers worth reporting."""
    completed = [s for s in signals if s.get("measurementStatus") == "COMPLETED"]
    returns = [
        (float(s["return20m"]), str(s.get("symbol", "?")))
        for s in completed
        if s.get("return20m") is not None
    ]
    summary: dict[str, Any] = {
        "total": len(signals),
        "long": sum(1 for s in signals if s.get("side") == "LONG"),
        "short": sum(1 for s in signals if s.get("side") == "SHORT"),
        "completed": len(completed),
        "active": sum(1 for s in signals if s.get("measurementStatus") == "ACTIVE"),
        "interrupted": sum(1 for s in signals if s.get("measurementStatus") == "INTERRUPTED"),
        "measured": len(returns),
        "positive": sum(1 for value, _symbol in returns if value > 0),
        "negative": sum(1 for value, _symbol in returns if value < 0),
        "avg_return": sum(value for value, _symbol in returns) / len(returns) if returns else None,
        "best": max(returns) if returns else None,
        "worst": min(returns) if returns else None,
    }
    return summary


def format_status_message(
    *,
    now: datetime,
    uptime_sec: float,
    runtimes: dict[str, SymbolRuntime],
    active_measurements: dict[str, int],
    market_summary: str,
    public_summary: str,
    max_loop_lag_sec: float,
    performance: dict[str, Any],
) -> str:
    """The periodic Telegram status digest."""
    lines = [
        "📊 <b>Shadow Signal status</b>",
        f"{now:%Y-%m-%d %H:%M} UTC · uptime {format_duration(uptime_sec)}"
        f" · {len(runtimes)} symbols",
        "",
        "<b>Last 24h</b>",
    ]

    if performance["total"] == 0:
        lines.append("No signals.")
    else:
        lines.append(
            f"{performance['total']} signals "
            f"({performance['long']} LONG / {performance['short']} SHORT)"
        )
        if performance["measured"]:
            lines.append(
                f"{performance['measured']} measured · avg 20m return "
                f"{performance['avg_return']:+.2f}% · "
                f"{performance['positive']} up / {performance['negative']} down"
            )
            best_value, best_symbol = performance["best"]
            worst_value, worst_symbol = performance["worst"]
            lines.append(
                f"best {best_value:+.2f}% {html.escape(best_symbol)} · "
                f"worst {worst_value:+.2f}% {html.escape(worst_symbol)}"
            )
        if performance["active"] or performance["interrupted"]:
            lines.append(
                f"{performance['active']} measuring · "
                f"{performance['interrupted']} interrupted"
            )

    lines += ["", "<b>Symbols</b>"]
    for symbol, rt in runtimes.items():
        detail = band_position(rt)
        if rt.state == "COOLDOWN" and rt.cooldown_until is not None:
            detail = f"{format_duration((rt.cooldown_until - now).total_seconds())} left"
        measuring = active_measurements.get(symbol, 0)
        suffix = f" · {measuring} measuring" if measuring else ""
        lines.append(
            f"{html.escape(symbol)} {rt.state} {format_price(rt.last_price)}"
            f" ({detail}){suffix}"
        )

    lines += [
        "",
        "<b>Feeds</b>",
        f"market {html.escape(market_summary)}",
        f"public {html.escape(public_summary)}",
        f"loop lag max {max_loop_lag_sec * 1000:.0f} ms",
    ]
    return "\n".join(lines)
