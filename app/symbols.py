from __future__ import annotations

from collections.abc import Iterable


def select_symbols(
    universe: Iterable[tuple[str, float]],
    min_quote_volume: float,
    max_symbols: int,
) -> list[str]:
    """Pick the most liquid symbols that clear the 24h quote volume threshold.

    `universe` holds (symbol, quoteVolume) pairs. The result is ordered by
    volume descending and truncated to `max_symbols`.
    """
    eligible = [
        (str(symbol).upper(), float(volume))
        for symbol, volume in universe
        if float(volume) >= float(min_quote_volume)
    ]
    eligible.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _volume in eligible[: int(max_symbols)]]
