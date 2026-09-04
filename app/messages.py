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
    """Where the price sits relative to the entry band (Hungarian: Telegram text)."""
    price, lower, upper, atr = rt.last_price, rt.lower_entry, rt.upper_entry, rt.entry_atr
    if price is None or lower is None or upper is None or not atr:
        return "még nincs sáv"
    if price < lower:
        return f"{(lower - price) / atr:.2f} ATR-rel a sáv alja alatt"
    if price > upper:
        return f"{(price - upper) / atr:.2f} ATR-rel a sáv teteje felett"
    return f"{(price - lower) / atr:+.2f} ATR-rel a sáv alja felett"


def waiting_for(rt: SymbolRuntime) -> str:
    """What has to happen next for this symbol. English: this one goes to the log."""
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
    """The Telegram signal in Hungarian, explaining why it fired."""
    icon = "🟢" if side == "LONG" else "🔴"
    entry_tf = html.escape(str(rt.settings["entryTimeframe"]))
    exit_tf = html.escape(str(rt.settings["exitTimeframe"]))
    open_price = format_price(validation["candleOpen"])
    vwap = format_price(validation["vwap"])
    shown_price = format_price(price)

    if side == "LONG":
        band_edge = rt.lower_entry
        crossing = f"az ár alulról átlépte a {entry_tf} sáv alját"
        vwap_order = f"nyitás {open_price} &lt; VWAP {vwap} &lt; ár {shown_price}"
        flow = "gyorsuló vételi nyomás"
    else:
        band_edge = rt.upper_entry
        crossing = f"az ár felülről átlépte a {entry_tf} sáv tetejét"
        vwap_order = f"ár {shown_price} &lt; VWAP {vwap} &lt; nyitás {open_price}"
        flow = "gyorsuló eladói nyomás"

    crossed_pct = (price - band_edge) / band_edge * 100.0 if band_edge else 0.0

    return "\n".join(
        [
            f"{icon} <b>{html.escape(side)} · {html.escape(rt.symbol)}</b>",
            f"Ár <b>{shown_price}</b> · {signal_at:%H:%M:%S} UTC",
            "",
            "<b>Miért</b>",
            f"• Visszalépés: {crossing} ({format_price(band_edge)}, {crossed_pct:+.2f}%)",
            f"• VWAP: {vwap_order}",
            f"• CVD: meredekség {validation['cvdSlope']:+.4f},"
            f" görbület {validation['cvdCurvature']:+.4f} — {flow}",
            f"• Spread {validation['spreadBps']:.2f} bps"
            f" (max {float(rt.settings['maxSpreadBps']):.0f})"
            f" · adat: trade {validation['tradeAgeSec']:.1f}s, book {validation['bookAgeSec']:.1f}s",
            "",
            "<b>Szintek</b>",
            f"• {entry_tf} sáv: {format_price(rt.lower_entry)} – {format_price(rt.upper_entry)}"
            f" (EMA {format_price(rt.entry_ema)}, ATR {format_price(rt.entry_atr)},"
            f" x{float(rt.settings['xEntry']):g})",
            f"• {exit_tf} kilépési iránymutató: <b>{format_price(exit_guideline)}</b>",
            "",
            "<i>Nem küld ordert. 20 percig méri az ármozgást.</i>",
        ]
    )


def summarize_signals(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate recent signal documents into the numbers worth reporting."""
    completed = [s for s in signals if s.get("measurementStatus") == "COMPLETED"]
    returns = [
        (float(s["return20m"]), str(s.get("symbol", "?")))
        for s in completed
        if s.get("return20m") is not None
    ]
    mfes = [float(s["MFE"]) for s in completed if s.get("MFE") is not None]
    maes = [float(s["MAE"]) for s in completed if s.get("MAE") is not None]
    peak_times = [float(s["timeToMFE"]) for s in completed if s.get("timeToMFE")]
    adverse_first = sum(
        1
        for s in completed
        if s.get("timeToMAE") and s.get("timeToMFE") and s["timeToMAE"] < s["timeToMFE"]
    )
    avg_return = sum(value for value, _symbol in returns) / len(returns) if returns else None
    avg_mfe = sum(mfes) / len(mfes) if mfes else None

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
        "avg_return": avg_return,
        "best": max(returns) if returns else None,
        "worst": min(returns) if returns else None,
        "avg_mfe": avg_mfe,
        "avg_mae": sum(maes) / len(maes) if maes else None,
        "worst_mae": min(maes) if maes else None,
        "avg_time_to_mfe": sum(peak_times) / len(peak_times) if peak_times else None,
        "adverse_first": adverse_first,
        # Share of the best moment still held at the 20 minute mark.
        "capture": (avg_return / avg_mfe) if (avg_mfe and avg_return is not None) else None,
    }
    return summary


def interpret_performance(summary: dict[str, Any]) -> list[str]:
    """Read the MFE/MAE aggregates back as plain Hungarian sentences."""
    if not summary.get("measured") or summary.get("avg_mfe") is None:
        return []

    avg_mfe = summary["avg_mfe"]
    avg_mae = summary["avg_mae"] or 0.0
    lines = [
        f"A signalok átlagosan {avg_mfe:+.2f}%-ig jutottak, "
        f"és közben {avg_mae:.2f}%-ot mentek ellened."
    ]

    if summary["avg_time_to_mfe"]:
        lines.append(
            f"A csúcs átlagosan {format_duration(summary['avg_time_to_mfe'])}-cel "
            f"a signal után jött."
        )

    capture = summary["capture"]
    if capture is not None:
        if capture <= 0:
            lines.append("A 20. percre átlagosan veszteségbe fordultak.")
        elif capture < 0.4:
            lines.append(
                f"A csúcsnak csak a {capture * 100:.0f}%-át tartották meg a 20. percre — "
                "a többit visszaadták, a 20 perc hosszú ablaknak tűnik."
            )
        elif capture < 0.8:
            lines.append(
                f"A csúcs {capture * 100:.0f}%-a maradt meg a 20. percre, "
                "a többit visszaadták."
            )
        else:
            lines.append("A 20. percre nagyjából a csúcs közelében zártak.")

    if abs(avg_mae) > avg_mfe > 0:
        lines.append(
            "Az átlagos visszaesés nagyobb volt, mint az elért csúcs — "
            "a belépések korainak tűnnek."
        )

    if summary["measured"] < 5:
        lines.append(f"({summary['measured']} lemért signal — kevés minta, óvatosan.)")
    return lines


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
    """The periodic Telegram digest, in Hungarian."""
    lines = [
        "📊 <b>Shadow Signal állapot</b>",
        f"{now:%Y-%m-%d %H:%M} UTC · {format_duration(uptime_sec)} óta fut"
        f" · {len(runtimes)} symbol",
        "",
        "<b>Elmúlt 24 óra</b>",
    ]

    if performance["total"] == 0:
        lines.append("Nem volt signal.")
    else:
        lines.append(
            f"{performance['total']} signal "
            f"({performance['long']} LONG / {performance['short']} SHORT)"
        )
        if performance["measured"]:
            lines.append(
                f"{performance['measured']} lemérve · átlagos 20 perces hozam "
                f"{performance['avg_return']:+.2f}% · "
                f"{performance['positive']} pozitív / {performance['negative']} negatív"
            )
            best_value, best_symbol = performance["best"]
            worst_value, worst_symbol = performance["worst"]
            lines.append(
                f"legjobb {best_value:+.2f}% {html.escape(best_symbol)} · "
                f"legrosszabb {worst_value:+.2f}% {html.escape(worst_symbol)}"
            )
        if performance["active"] or performance["interrupted"]:
            lines.append(
                f"{performance['active']} mérés folyamatban · "
                f"{performance['interrupted']} megszakadt"
            )

    interpretation = interpret_performance(performance)
    if interpretation:
        lines += ["", "<b>Mit mutat</b>"] + interpretation

    lines += ["", "<b>Symbolok</b>"]
    for symbol, rt in runtimes.items():
        detail = band_position(rt)
        if rt.state == "COOLDOWN" and rt.cooldown_until is not None:
            detail = f"{format_duration((rt.cooldown_until - now).total_seconds())} van hátra"
        measuring = active_measurements.get(symbol, 0)
        suffix = f" · {measuring} mérés" if measuring else ""
        lines.append(
            f"{html.escape(symbol)} {rt.state} {format_price(rt.last_price)}"
            f" ({detail}){suffix}"
        )

    lines += [
        "",
        "<b>Adatfolyam</b>",
        f"market {html.escape(market_summary)}",
        f"public {html.escape(public_summary)}",
        f"loop lag max {max_loop_lag_sec * 1000:.0f} ms",
    ]
    return "\n".join(lines)
