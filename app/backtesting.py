"""Chronological, long-only historical strategy simulation.

Signals use each symbol's data only through its current completed candle. Orders are
submitted after that close and can fill no earlier than the next eligible candle open.
"""

from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from app.market_data import Candle
from app.strategy import evaluate
from app.risk import check_order

D = Decimal


@dataclass(frozen=True)
class BacktestConfig:
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    initial_capital: Decimal = D("10000")
    short_window: int = 20
    long_window: int = 50
    commission_rate: Decimal = D("0.001")
    other_cost_rate: Decimal = D("0")
    slippage_bps: Decimal = D("5")
    position_fraction: Decimal = D("0.95")
    max_order_value: Decimal | None = None
    max_position_value: Decimal | None = None
    max_portfolio_exposure: Decimal | None = None
    allowed_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)

    def __post_init__(self):
        for name in (
            "initial_capital",
            "commission_rate",
            "other_cost_rate",
            "slippage_bps",
            "position_fraction",
            "max_order_value",
            "max_position_value",
            "max_portfolio_exposure",
        ):
            value = getattr(self, name)
            if value is not None:
                try:
                    normalized = D(str(value))
                except (InvalidOperation, ValueError) as error:
                    raise ValueError(f"{name} must be a valid decimal") from error
                if not normalized.is_finite():
                    raise ValueError(f"{name} must be finite")
                object.__setattr__(self, name, normalized)
        object.__setattr__(
            self, "symbols", tuple(s.strip().upper() for s in self.symbols if s.strip())
        )
        if any(
            value is not None and value <= 0
            for value in (
                self.max_order_value,
                self.max_position_value,
                self.max_portfolio_exposure,
            )
        ):
            raise ValueError("Optional risk limits must be positive")
        if not self.symbols:
            raise ValueError("At least one symbol is required")
        if self.start_date > self.end_date:
            raise ValueError("Start date must be on or before end date")
        if self.initial_capital <= 0:
            raise ValueError("Initial capital must be positive")
        if self.short_window < 1 or self.long_window <= self.short_window:
            raise ValueError("Long window must exceed positive short window")
        if not D(0) <= self.other_cost_rate < D(1):
            raise ValueError("Other cost rate must be in [0,1)")
        if not D(0) <= self.commission_rate < D(1):
            raise ValueError("Commission rate must be in [0,1)")
        if not D(0) <= self.slippage_bps <= D(1000):
            raise ValueError("Slippage bps must be in [0,1000]")
        if not D(0) < self.position_fraction <= D(1):
            raise ValueError("Position fraction must be in (0,1]")
        if not self.allowed_weekdays or any(
            day not in range(7) for day in self.allowed_weekdays
        ):
            raise ValueError(
                "Weekdays must be integers from 0 (Monday) through 6 (Sunday)"
            )

    def as_jsonable(self):
        data = asdict(self)
        for key in ("start_date", "end_date"):
            data[key] = data[key].isoformat()
        for key in (
            "initial_capital",
            "commission_rate",
            "other_cost_rate",
            "slippage_bps",
            "position_fraction",
            "max_order_value",
            "max_position_value",
            "max_portfolio_exposure",
        ):
            if data[key] is not None:
                data[key] = str(data[key])
        return data


def _to_date(value):
    return value.date() if isinstance(value, datetime) else value


def _validate_history(bars):
    if not bars:
        return []
    last = None
    for bar in bars:
        if bar.timestamp.tzinfo is None:
            raise ValueError("Historical candle timestamps must be timezone-aware")
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if (
            not all(math.isfinite(float(v)) for v in values)
            or min(values[:4]) <= 0
            or bar.volume < 0
        ):
            raise ValueError("Historical data contains invalid OHLCV values")
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
            bar.open, bar.close, bar.high
        ):
            raise ValueError("Historical candle has inconsistent high/low values")
        if last is not None and bar.timestamp <= last:
            raise ValueError("Historical candles must be unique and chronological")
        last = bar.timestamp
    return bars


def _metrics(
    equity_curve,
    trades,
    initial,
    benchmark_final,
    fees,
    slippage,
    commissions,
    other_costs,
):
    final = D(equity_curve[-1]["equity"]) if equity_curve else initial
    total_return = final / initial - D(1) if initial else None
    high = initial
    max_dd = D(0)
    for point in equity_curve:
        value = D(point["equity"])
        high = max(high, value)
        if high > 0:
            max_dd = max(max_dd, (high - value) / high)
    dated = [date.fromisoformat(x["date"]) for x in equity_curve]
    days = (dated[-1] - dated[0]).days if len(dated) > 1 else 0
    annualized = (
        float((final / initial) ** (D("365.25") / D(days)) - D(1))
        if days >= 365 and final > 0
        else None
    )
    profits = [D(t["realized_pnl"]) for t in trades]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    daily_returns = []
    for prior, current in zip(equity_curve, equity_curve[1:]):
        before = D(prior["equity"])
        after = D(current["equity"])
        if before > 0:
            daily_returns.append(float(after / before - D(1)))
    sharpe = None
    if len(daily_returns) >= 30:
        mean = sum(daily_returns) / len(daily_returns)
        variance = sum((x - mean) ** 2 for x in daily_returns) / (
            len(daily_returns) - 1
        )
        deviation = math.sqrt(variance)
        if deviation > 0:
            sharpe = mean / deviation * math.sqrt(252)
    return {
        "initial_capital": str(initial),
        "final_equity": str(final),
        "total_return": float(total_return),
        "annualized_return": annualized,
        "max_drawdown": float(max_dd),
        "completed_trades": len(trades),
        "win_rate": len(wins) / len(profits) if profits else None,
        "average_winning_trade": str(sum(wins, D(0)) / len(wins)) if wins else None,
        "average_losing_trade": str(sum(losses, D(0)) / len(losses))
        if losses
        else None,
        "profit_factor": float(sum(wins, D(0)) / abs(sum(losses, D(0))))
        if losses and sum(losses, D(0)) != 0
        else None,
        "sharpe_ratio": sharpe,
        "fees": str(fees),
        "commissions": str(commissions),
        "other_costs": str(other_costs),
        "slippage_cost": str(slippage),
        "buy_hold_final_equity": str(benchmark_final),
        "buy_hold_return": float(benchmark_final / initial - D(1)) if initial else None,
    }


def run_backtest(data: dict[str, list[Candle]], config: BacktestConfig):
    """Run using close decisions and next eligible open fills; data may include warmup bars."""
    start = _to_date(config.start_date)
    end = _to_date(config.end_date)
    bars = {s: _validate_history(data.get(s, [])) for s in config.symbols}
    relevant = {
        s: [b for b in series if b.timestamp.date() <= end]
        for s, series in bars.items()
    }
    sessions = sorted(
        {
            bar.timestamp
            for series in relevant.values()
            for bar in series
            if start <= bar.timestamp.date() <= end
        }
    )
    cash = D(config.initial_capital)
    positions = {}
    marks = {}
    pending = {}
    signals = []
    orders = []
    fills = []
    cash_movements = []
    position_changes = []
    trades = []
    curve = []
    warnings = []
    warnings.append(
        "The selected symbol universe is user supplied; survivorship bias from delisted instruments is not corrected."
    )
    fees = D(0)
    commissions = D(0)
    other_costs = D(0)
    slippage = D(0)
    order_seq = 0
    benchmark_shares = {}
    benchmark_cash = D(config.initial_capital)
    buy_alloc = config.initial_capital / D(len(config.symbols))
    max_order = config.max_order_value or config.initial_capital
    max_position = config.max_position_value or config.initial_capital
    max_exposure = config.max_portfolio_exposure or config.initial_capital
    indexes = {s: 0 for s in config.symbols}
    by_timestamp = {}
    for symbol, series in relevant.items():
        for i, bar in enumerate(series):
            by_timestamp.setdefault(bar.timestamp, []).append((symbol, i, bar))
        if not any(start <= b.timestamp.date() <= end for b in series):
            warnings.append(f"{symbol}: no candles in the requested period")
    benchmark_fee = D(config.commission_rate) + D(config.other_cost_rate)
    slip_rate = D(config.slippage_bps) / D(10000)
    valuations = {}
    for timestamp in sessions:
        events = by_timestamp.get(timestamp, [])
        for symbol, index, bar in events:
            series = relevant[symbol]
            indexes[symbol] = index
            if bar.timestamp.weekday() not in config.allowed_weekdays:
                continue
            marks[symbol] = D(str(bar.open))
            if (
                symbol in pending
                and datetime.fromisoformat(pending[symbol]["decision_time"]) < timestamp
            ):
                order = pending.pop(symbol)
                side = order["side"]
                qty = D(order["quantity"])
                market = D(str(bar.open))
                fill = market * (
                    D(1) + slip_rate if side == "BUY" else D(1) - slip_rate
                )
                commission = qty * fill * D(config.commission_rate)
                other_cost = qty * fill * D(config.other_cost_rate)
                fee = commission + other_cost
                reason = check_order(
                    symbol,
                    side,
                    qty,
                    market,
                    fill,
                    [(s, str(p["quantity"])) for s, p in positions.items()],
                    marks,
                    cash,
                    fee,
                    max_order,
                    max_position,
                    max_exposure,
                )
                order["fill_time"] = timestamp.isoformat()
                order["market_price"] = str(market)
                order["fill_price"] = str(fill)
                order["fee"] = str(fee if reason is None else D(0))
                order["commission"] = str(commission if reason is None else D(0))
                order["other_cost"] = str(other_cost if reason is None else D(0))
                order["slippage_cost"] = str(
                    qty * abs(fill - market) if reason is None else D(0)
                )
                order["status"] = "REJECTED" if reason else "FILLED"
                order["reason"] = reason
                cash_before = cash
                quantity_before = positions.get(symbol, {"quantity": D(0)})["quantity"]
                if reason is None:
                    fees += fee
                    commissions += commission
                    other_costs += other_cost
                    slippage += qty * abs(fill - market)
                    if side == "BUY":
                        cash -= qty * fill + fee
                        old = positions.get(
                            symbol, {"quantity": D(0), "cost_basis": D(0)}
                        )
                        positions[symbol] = {
                            "quantity": old["quantity"] + qty,
                            "cost_basis": old["cost_basis"] + qty * fill + fee,
                        }
                    else:
                        old = positions[symbol]
                        avg_cost = old["cost_basis"] / old["quantity"]
                        pnl = qty * fill - fee - qty * avg_cost
                        cash += qty * fill - fee
                        old["quantity"] -= qty
                        old["cost_basis"] -= qty * avg_cost
                        trades.append(
                            {
                                "symbol": symbol,
                                "entry_cost": str(qty * avg_cost),
                                "exit_proceeds": str(qty * fill - fee),
                                "realized_pnl": str(pnl),
                                "exit_time": timestamp.isoformat(),
                            }
                        )
                        if old["quantity"] <= D("0.0000000001"):
                            positions.pop(symbol)
                    fills.append(
                        {
                            "order_id": order["order_id"],
                            "symbol": symbol,
                            "side": side,
                            "quantity": str(qty),
                            "fill_price": str(fill),
                            "fee": str(fee),
                            "commission": str(commission),
                            "other_cost": str(other_cost),
                            "timestamp": timestamp.isoformat(),
                        }
                    )
                if reason is None:
                    cash_movements.append(
                        {
                            "order_id": order["order_id"],
                            "timestamp": timestamp.isoformat(),
                            "symbol": symbol,
                            "cash_delta": str(cash - cash_before),
                            "commission": str(commission),
                            "other_cost": str(other_cost),
                            "fee": str(fee),
                        }
                    )
                    quantity_after = positions.get(symbol, {"quantity": D(0)})[
                        "quantity"
                    ]
                    position_changes.append(
                        {
                            "order_id": order["order_id"],
                            "timestamp": timestamp.isoformat(),
                            "symbol": symbol,
                            "quantity_before": str(quantity_before),
                            "quantity_after": str(quantity_after),
                            "quantity_delta": str(quantity_after - quantity_before),
                        }
                    )
                continue_signal = False
            else:
                continue_signal = True
            if continue_signal and start <= bar.timestamp.date() <= end:
                history = series[: index + 1]
                signal = evaluate(
                    symbol, history, config.short_window, config.long_window
                )
                signals.append(
                    {
                        "symbol": symbol,
                        "timestamp": timestamp.isoformat(),
                        "direction": signal.direction,
                        "price": str(signal.price)
                        if signal.price is not None
                        else None,
                        "short_average": signal.short_average,
                        "long_average": signal.long_average,
                        "explanation": signal.explanation,
                    }
                )
                marks[symbol] = D(str(bar.close))
                if (
                    symbol not in pending
                    and signal.direction == "BUY_CANDIDATE"
                    and symbol not in positions
                ):
                    equity = cash + sum(
                        (
                            p["quantity"] * marks.get(s, D(0))
                            for s, p in positions.items()
                        ),
                        D(0),
                    )
                    qty = (
                        equity * D(config.position_fraction) / D(len(config.symbols))
                    ) / D(str(bar.close))
                    order_seq += 1
                    order = {
                        "order_id": f"BT-{order_seq:06d}",
                        "symbol": symbol,
                        "side": "BUY",
                        "quantity": str(qty),
                        "decision_time": timestamp.isoformat(),
                        "decision_price": str(bar.close),
                        "status": "PENDING",
                        "reason": None,
                    }
                    orders.append(order)
                    pending[symbol] = order
                elif (
                    symbol not in pending
                    and signal.direction == "SELL_CANDIDATE"
                    and symbol in positions
                ):
                    order_seq += 1
                    order = {
                        "order_id": f"BT-{order_seq:06d}",
                        "symbol": symbol,
                        "side": "SELL",
                        "quantity": str(positions[symbol]["quantity"]),
                        "decision_time": timestamp.isoformat(),
                        "decision_price": str(bar.close),
                        "status": "PENDING",
                        "reason": None,
                    }
                    orders.append(order)
                    pending[symbol] = order
        # Equal-weight buy-and-hold benchmark enters on the first eligible open in range.
        for symbol, index, bar in events:
            if (
                bar.timestamp.weekday() in config.allowed_weekdays
                and start <= bar.timestamp.date() <= end
                and symbol not in benchmark_shares
            ):
                budget = buy_alloc
                open_px = D(str(bar.open))
                fill = open_px * (D(1) + slip_rate)
                qty = budget / (fill * (D(1) + benchmark_fee))
                cost = qty * fill
                fee = cost * benchmark_fee
                benchmark_cash -= cost + fee
                benchmark_shares[symbol] = qty
        for symbol, index, bar in events:
            if bar.timestamp.weekday() in config.allowed_weekdays:
                marks[symbol] = D(str(bar.close))
        equity = cash + sum(
            (p["quantity"] * marks.get(s, D(0)) for s, p in positions.items()), D(0)
        )
        bench = benchmark_cash + sum(
            (q * marks.get(s, D(0)) for s, q in benchmark_shares.items()), D(0)
        )
        day = timestamp.date().isoformat()
        valuations[day] = {
            "date": day,
            "equity": str(equity),
            "cash": str(cash),
            "positions": [
                {
                    "symbol": s,
                    "quantity": str(p["quantity"]),
                    "market_value": str(p["quantity"] * marks[s]),
                }
                for s, p in positions.items()
                if s in marks
            ],
            "benchmark_equity": str(bench),
            "drawdown": "0",
        }
    for order in pending.values():
        order["status"] = "UNFILLED"
        order["reason"] = "No subsequent eligible candle in requested date range"
    curve = list(valuations.values())
    peak = D(config.initial_capital)
    for point in curve:
        value = D(point["equity"])
        peak = max(peak, value)
        point["drawdown"] = str((peak - value) / peak if peak else D(0))
    benchmark_final = (
        D(curve[-1]["benchmark_equity"]) if curve else D(config.initial_capital)
    )
    metrics = _metrics(
        curve,
        trades,
        D(config.initial_capital),
        benchmark_final,
        fees,
        slippage,
        commissions,
        other_costs,
    )
    config_json = config.as_jsonable()
    payload = {
        "configuration": config_json,
        "input_candles": {
            symbol: [
                [
                    bar.timestamp.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                ]
                for bar in relevant[symbol]
            ]
            for symbol in config.symbols
        },
        "metrics": metrics,
        "signals": signals,
        "orders": orders,
        "fills": fills,
        "cash_movements": cash_movements,
        "position_changes": position_changes,
        "trades": trades,
        "equity_curve": curve,
        "warnings": warnings,
    }
    fingerprint = {
        "configuration": config_json,
        "candles": {
            s: [
                [b.timestamp.isoformat(), b.open, b.high, b.low, b.close, b.volume]
                for b in relevant[s]
            ]
            for s in config.symbols
        },
    }
    run_id = hashlib.sha256(
        json.dumps(
            fingerprint, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
    payload["run_id"] = run_id
    payload["label"] = (
        "HISTORICAL SIMULATION - not a prediction or guarantee of future results"
    )
    return payload


class BacktestRepository:
    """Store reproducible run inputs/results in the configured SQLite database."""

    def __init__(self, path):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(":memory:") if path == ":memory:" else None
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS backtest_runs(run_id TEXT PRIMARY KEY,saved_at TEXT NOT NULL,configuration TEXT NOT NULL,result_json TEXT NOT NULL)"
            )

    def connect(self):
        db = self._memory if self._memory is not None else sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def save(self, result):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO backtest_runs VALUES(?,?,?,?)",
                (
                    result["run_id"],
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(result["configuration"], sort_keys=True),
                    json.dumps(result, sort_keys=True, default=str),
                ),
            )
        return result["run_id"]

    def list_runs(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT run_id,saved_at,configuration FROM backtest_runs ORDER BY saved_at DESC"
                )
            ]

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT result_json FROM backtest_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def reproduce(self, run_id):
        """Re-run a saved simulation from its stored candles and configuration."""
        saved = self.get(run_id)
        if saved is None:
            raise KeyError(f"Unknown backtest run: {run_id}")
        raw = dict(saved["configuration"])
        raw["symbols"] = tuple(raw["symbols"])
        raw["allowed_weekdays"] = tuple(raw["allowed_weekdays"])
        raw["start_date"] = date.fromisoformat(raw["start_date"])
        raw["end_date"] = date.fromisoformat(raw["end_date"])
        config = BacktestConfig(**raw)
        data = {
            symbol: [Candle(datetime.fromisoformat(row[0]), *row[1:]) for row in rows]
            for symbol, rows in saved["input_candles"].items()
        }
        return run_backtest(data, config)
