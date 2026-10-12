"""Historical market data boundary and Yahoo Finance adapter."""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import math
from typing import Protocol


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketDataProvider(Protocol):
    def history(
        self, symbol: str, period: str = "1y", start=None, end=None
    ) -> list[Candle]: ...


def validate_candles(candles, max_age_hours=72, now=None, check_freshness=True):
    if not candles:
        raise ValueError("No market data returned")
    previous = None
    for candle in candles:
        if candle.timestamp.tzinfo is None:
            raise ValueError("Market candle timestamp must include a timezone")
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if (
            not all(math.isfinite(value) for value in values)
            or min(values[:4]) <= 0
            or candle.volume < 0
        ):
            raise ValueError("Market data contains invalid OHLCV values")
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(
            candle.open, candle.close, candle.high
        ):
            raise ValueError("Market candle has inconsistent high/low values")
        if previous is not None and candle.timestamp <= previous:
            raise ValueError("Market data contains duplicate or out-of-order candles")
        previous = candle.timestamp
    if check_freshness:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        latest = candles[-1].timestamp.astimezone(timezone.utc)
        if latest > current + timedelta(minutes=5):
            raise ValueError("Market data timestamp is in the future")
        if current - latest > timedelta(hours=max_age_hours):
            raise ValueError(
                f"Market data is stale (latest candle {latest.isoformat()})"
            )
    return candles


class YahooFinanceProvider:
    def __init__(self, max_age_hours=72):
        self.max_age_hours = max_age_hours

    def history(self, symbol, period="1y", start=None, end=None):
        try:
            import yfinance as yf

            ticker = yf.Ticker(symbol)
            if start is not None:
                frame = ticker.history(start=start, end=end, auto_adjust=True)
            else:
                frame = ticker.history(period=period, auto_adjust=True)
        except Exception as exc:
            raise RuntimeError(
                f"Market data request failed for {symbol}: {exc}"
            ) from exc
        if frame.empty:
            raise RuntimeError(
                f"No market data returned for {symbol}; check symbol/provider availability"
            )
        candles = []
        for index, row in frame.iterrows():
            stamp = index.to_pydatetime()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            candles.append(
                Candle(
                    stamp,
                    *[
                        float(row[key])
                        for key in ("Open", "High", "Low", "Close", "Volume")
                    ],
                )
            )
        return validate_candles(
            candles, self.max_age_hours, check_freshness=start is None
        )
