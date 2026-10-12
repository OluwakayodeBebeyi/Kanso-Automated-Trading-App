"""Explainable moving-average trend strategy."""

from dataclasses import dataclass
from datetime import datetime, timezone
from .market_data import Candle


@dataclass(frozen=True)
class Signal:
    symbol: str
    evaluated_at: datetime
    price: float | None
    direction: str
    short_average: float | None
    long_average: float | None
    explanation: str
    actionable: bool


def evaluate(
    symbol: str, candles: list[Candle], short_window: int, long_window: int
) -> Signal:
    if short_window < 1 or long_window <= short_window:
        raise ValueError("long_window must be greater than positive short_window")
    stamp = candles[-1].timestamp if candles else datetime.now(timezone.utc)
    if len(candles) < long_window:
        return Signal(
            symbol,
            stamp,
            candles[-1].close if candles else None,
            "NO_DATA",
            None,
            None,
            f"Need {long_window} valid candles; received {len(candles)}.",
            False,
        )
    closes = [c.close for c in candles]
    short = sum(closes[-short_window:]) / short_window
    long = sum(closes[-long_window:]) / long_window
    direction = (
        "BUY_CANDIDATE"
        if short > long
        else "SELL_CANDIDATE"
        if short < long
        else "HOLD"
    )
    relation = "above" if short > long else "below" if short < long else "equal to"
    reason = f"{short_window}-period average {short:.2f} is {relation} {long_window}-period average {long:.2f}."
    return Signal(
        symbol, stamp, closes[-1], direction, short, long, reason, direction != "HOLD"
    )
