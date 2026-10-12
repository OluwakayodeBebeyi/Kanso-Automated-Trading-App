"""Environment-backed settings for the local paper trading application."""

from dataclasses import dataclass
from decimal import Decimal
import os


@dataclass(frozen=True)
class Settings:
    watchlist: tuple[str, ...] = ("AAPL", "MSFT", "SPY")
    short_window: int = 20
    long_window: int = 50
    initial_cash: Decimal = Decimal("10000")
    max_order_value: Decimal = Decimal("1000")
    max_position_value: Decimal = Decimal("2500")
    max_portfolio_exposure: Decimal = Decimal("8000")
    fee_rate: Decimal = Decimal("0.001")
    slippage_bps: Decimal = Decimal("5")
    max_data_age_hours: int = 72
    database_path: str = "data/trading.sqlite3"
    execution_mode: str = "PAPER"

    @classmethod
    def from_env(cls):
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        r = cls(
            watchlist=tuple(
                x.strip().upper()
                for x in os.getenv("WATCHLIST", "AAPL,MSFT,SPY").split(",")
                if x.strip()
            ),
            short_window=int(os.getenv("SHORT_WINDOW", "20")),
            long_window=int(os.getenv("LONG_WINDOW", "50")),
            initial_cash=Decimal(os.getenv("INITIAL_CASH", "10000")),
            max_order_value=Decimal(os.getenv("MAX_ORDER_VALUE", "1000")),
            max_position_value=Decimal(os.getenv("MAX_POSITION_VALUE", "2500")),
            max_portfolio_exposure=Decimal(os.getenv("MAX_PORTFOLIO_EXPOSURE", "8000")),
            fee_rate=Decimal(os.getenv("FEE_RATE", "0.001")),
            slippage_bps=Decimal(os.getenv("SLIPPAGE_BPS", "5")),
            max_data_age_hours=int(os.getenv("MAX_DATA_AGE_HOURS", "72")),
            database_path=os.getenv("DATABASE_PATH", "data/trading.sqlite3"),
            execution_mode=os.getenv("EXECUTION_MODE", "PAPER").strip().upper(),
        )
        from app.execution import validate_execution_mode

        validate_execution_mode(r.execution_mode)
        if not r.watchlist:
            raise ValueError("WATCHLIST must contain at least one symbol")
        if r.short_window < 1 or r.long_window <= r.short_window:
            raise ValueError("LONG_WINDOW must be greater than positive SHORT_WINDOW")
        if (
            min(
                r.initial_cash,
                r.max_order_value,
                r.max_position_value,
                r.max_portfolio_exposure,
            )
            <= 0
        ):
            raise ValueError("Cash and risk limits must be positive")
        if not Decimal(0) <= r.fee_rate < Decimal(1):
            raise ValueError("FEE_RATE must be between 0 and 1")
        if not Decimal(0) <= r.slippage_bps <= Decimal(1000):
            raise ValueError("SLIPPAGE_BPS must be between 0 and 1000")
        if r.max_data_age_hours < 1:
            raise ValueError("MAX_DATA_AGE_HOURS must be positive")
        return r
