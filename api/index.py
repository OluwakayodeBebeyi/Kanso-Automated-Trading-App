"""Vercel ASGI entrypoint for read-only market analysis."""
from __future__ import annotations

import re
from datetime import timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from app.config import Settings
from app.market_data import YahooFinanceProvider
from app.strategy import evaluate

app = FastAPI(
    title="Stock Paper Trader",
    description="Read-only analysis; no orders are submitted.",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_PERIODS = {"5d", "1mo", "3mo", "6mo", "1y", "2y"}


@app.get("/api/health")
def health():
    """Confirm the function responds; provider availability is checked separately."""
    return {"status": "ok", "mode": "PAPER", "trading_enabled": False}


@app.get("/api/watchlist")
def watchlist():
    return {"symbols": list(Settings.from_env().watchlist), "mode": "PAPER"}


@app.get("/api/market/{symbol}")
def market_analysis(
    symbol: str,
    period: str = Query(default="6mo"),
    short_window: int | None = Query(default=None, ge=1, le=200),
    long_window: int | None = Query(default=None, ge=2, le=500),
):
    """Fetch validated provider data and calculate the existing MA signal."""
    symbol = symbol.strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise HTTPException(status_code=422, detail="Invalid ticker symbol")
    if period not in _PERIODS:
        raise HTTPException(status_code=422, detail="Unsupported history period")

    settings = Settings.from_env()
    short = settings.short_window if short_window is None else short_window
    long = settings.long_window if long_window is None else long_window
    if long <= short:
        raise HTTPException(
            status_code=422,
            detail="long_window must be greater than short_window",
        )

    try:
        candles = YahooFinanceProvider(settings.max_data_age_hours).history(
            symbol, period=period
        )
    except ValueError as exc:
        # These messages come from our own candle validation and contain no secrets.
        # Preserve the reason so stale and malformed feeds can be diagnosed.
        raise HTTPException(
            status_code=503,
            detail=f"Market data validation failed: {exc}",
        ) from exc
    except Exception as exc:
        # Avoid returning arbitrary upstream exception text to a public caller.
        raise HTTPException(
            status_code=503,
            detail=f"Market data provider failed ({type(exc).__name__})",
        ) from exc

    signal = evaluate(symbol, candles, short, long)
    latest = candles[-1]
    return JSONResponse(
        {
            "mode": "PAPER",
            "data_kind": "provider_market_data",
            "symbol": symbol,
            "period": period,
            "as_of": latest.timestamp.astimezone(timezone.utc).isoformat(),
            "price": latest.close,
            "signal": {
                "direction": signal.direction,
                "actionable": signal.actionable,
                "short_average": signal.short_average,
                "long_average": signal.long_average,
                "explanation": signal.explanation,
            },
            "disclaimer": (
                "Informational analysis only. No order was submitted. Historical "
                "or delayed market data may not reflect a current executable price."
            ),
        },
        headers={"Cache-Control": "no-store"},
    )
