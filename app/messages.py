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


def band_distance_atr(rt: SymbolRuntime) -> float | None:
    """Signed distance from the lower band edge, in ATR units."""
    price, lower, atr = rt.last_price, rt.lower_entry, rt.entry_atr
    if price is None or lower is None or not atr:
        return None
    return (price - lower) / atr


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Narrow monospace table for Telegram: space padded, no box drawing."""
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def line(cells: list[str]) -> str:
        # The last column is not padded, so the table stays as narrow as the data.
        padded = [cell.ljust(widths[i]) for i, cell in enumerate(cells)]
        return html.escape("  ".join(padded).rstrip())

    body = "\n".join(line(row) for row in rows)
    return f"<pre>{line(headers)}\n{body}</pre>"


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


CHECKPOINTS: tuple[tuple[str, str], ...] = (
    ("1m", "return1m"),
    ("3m", "return3m"),
    ("5m", "return5m"),
    ("10m", "return10m"),
    ("15m", "return15m"),
    ("20m", "return20m"),
)


def checkpoint_price(signal_price: float, side: str, return_pct: float | None) -> float | None:
    """Rebuild the checkpoint price from the stored return.

    Mongo keeps only the direction-adjusted percentages, so this inverts
    indicators.directional_return_pct exactly:
        LONG   r = (p - s) / s * 100  ->  p = s * (1 + r/100)
        SHORT  r = (s - p) / s * 100  ->  p = s * (1 - r/100)
    """
    if return_pct is None:
        return None
    factor = 1.0 + float(return_pct) / 100.0 if side == "LONG" else 1.0 - float(return_pct) / 100.0
    return float(signal_price) * factor


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

    checkpoints = []
    for label, field in CHECKPOINTS:
        values = [float(s[field]) for s in completed if s.get(field) is not None]
        checkpoints.append(
            {
                "label": label,
                "count": len(values),
                "avg": sum(values) / len(values) if values else None,
                "positive": sum(1 for value in values if value > 0),
                "negative": sum(1 for value in values if value < 0),
            }
        )

    return {
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
        "avg_mfe": sum(mfes) / len(mfes) if mfes else None,
        "avg_mae": sum(maes) / len(maes) if maes else None,
        "avg_time_to_mfe": sum(peak_times) / len(peak_times) if peak_times else None,
        "checkpoints": checkpoints,
    }


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

    checkpoints = [c for c in performance["checkpoints"] if c["count"]]
    if checkpoints:
        lines += ["", "<b>Összesített lefutás</b>"]
        lines.append(
            render_table(
                ["Idő", "Átlag", "+", "-"],
                [
                    [
                        c["label"],
                        f"{c['avg']:+.2f}%",
                        str(c["positive"]),
                        str(c["negative"]),
                    ]
                    for c in checkpoints
                ],
            )
        )
        footer = []
        if performance["avg_mfe"] is not None:
            footer.append(["MFE átlag", f"{performance['avg_mfe']:+.2f}%"])
        if performance["avg_mae"] is not None:
            footer.append(["MAE átlag", f"{performance['avg_mae']:+.2f}%"])
        if performance["avg_time_to_mfe"] is not None:
            footer.append(["timeToMFE átl.", format_duration(performance["avg_time_to_mfe"])])
        if footer:
            lines.append(render_table(["", ""], footer))

    lines += ["", "<b>Symbolok</b>"]
    symbol_rows = []
    for symbol, rt in runtimes.items():
        if rt.state == "COOLDOWN" and rt.cooldown_until is not None:
            note = format_duration((rt.cooldown_until - now).total_seconds())
        else:
            distance = band_distance_atr(rt)
            note = f"{distance:+.2f}" if distance is not None else "-"
        measuring = active_measurements.get(symbol, 0)
        symbol_rows.append(
            [symbol, rt.state, format_price(rt.last_price), note, str(measuring) if measuring else ""]
        )
    lines.append(render_table(["Symbol", "State", "Ár", "Sáv", "M"], symbol_rows))

    lines += [
        "",
        "<b>Adatfolyam</b>",
        f"market {html.escape(market_summary)}",
        f"public {html.escape(public_summary)}",
        f"loop lag max {max_loop_lag_sec * 1000:.0f} ms",
    ]
    return "\n".join(lines)


TELEGRAM_SAFE_LIMIT = 3900
MAX_DETAIL_MESSAGES = 10


def format_completed_signal_block(document: dict[str, Any]) -> str:
    """Full 20 minute walk of one COMPLETED signal."""
    symbol = str(document.get("symbol", "?"))
    side = str(document.get("side", "?"))
    signal_price = float(document["signalPrice"])
    signal_at = document.get("signalAt")
    icon = "🟢" if side == "LONG" else "🔴"

    rows = []
    for label, field in CHECKPOINTS:
        value = document.get(field)
        price = checkpoint_price(signal_price, side, value)
        rows.append(
            [
                label,
                format_price(price) if price is not None else "-",
                f"{float(value):+.2f}%" if value is not None else "-",
            ]
        )

    lines = [
        f"{icon} <b>{html.escape(symbol)} {html.escape(side)}</b>",
        f"{signal_at:%Y-%m-%d %H:%M:%S} UTC" if signal_at else "időpont ismeretlen",
        f"Belépő ár: {format_price(signal_price)}",
        render_table(["Idő", "Ár", "Return"], rows),
    ]

    mfe, mae = document.get("MFE"), document.get("MAE")
    if mfe is not None:
        lines.append(f"MFE {float(mfe):+.2f}% @ {format_duration(document.get('timeToMFE') or 0)}")
    if mae is not None:
        lines.append(f"MAE {float(mae):+.2f}% @ {format_duration(document.get('timeToMAE') or 0)}")
    return "\n".join(lines)


def format_signal_detail_messages(
    signals: list[dict[str, Any]],
    limit: int = TELEGRAM_SAFE_LIMIT,
    max_messages: int = MAX_DETAIL_MESSAGES,
) -> list[str]:
    """Pack every COMPLETED signal into Telegram sized messages, newest first.

    A single signal block is never split across two messages.
    """
    completed = sorted(
        (s for s in signals if s.get("measurementStatus") == "COMPLETED"),
        key=lambda s: s.get("signalAt") or datetime.min,
        reverse=True,
    )
    if not completed:
        return []

    blocks = [format_completed_signal_block(document) for document in completed]

    # Group blocks into pages first, so the "(n/m)" header knows the total.
    pages: list[list[str]] = []
    current: list[str] = []
    current_len = 40  # rough room for the page header
    for block in blocks:
        addition = len(block) + 2
        if current and current_len + addition > limit:
            pages.append(current)
            current, current_len = [], 40
        current.append(block)
        current_len += addition
    if current:
        pages.append(current)

    dropped = 0
    if len(pages) > max_messages:
        dropped = sum(len(page) for page in pages[max_messages:])
        pages = pages[:max_messages]

    messages = []
    for index, page in enumerate(pages, start=1):
        header = f"📄 <b>Lefutások ({index}/{len(pages)})</b>"
        body = "\n\n".join(page)
        if dropped and index == len(pages):
            body += f"\n\n<i>+{dropped} további lefutás nem fért ki.</i>"
        messages.append(f"{header}\n\n{body}")
    return messages
