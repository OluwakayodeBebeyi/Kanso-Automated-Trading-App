from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from app.backtesting import BacktestConfig, BacktestRepository, run_backtest
from app.market_data import Candle

START = date(2024, 1, 1)
END = date(2024, 1, 10)


def bars(closes, opens=None):
    opens = opens or closes
    days = []
    current = datetime(2024, 1, 1, tzinfo=timezone.utc)
    while len(days) < len(closes):
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return [
        Candle(t, o, max(o, c), min(o, c), c, 1000)
        for t, o, c in zip(days, opens, closes)
    ]


def cfg(**kwargs):
    base = dict(
        symbols=("XYZ",),
        start_date=START,
        end_date=END,
        initial_capital=Decimal("1000"),
        short_window=1,
        long_window=2,
        commission_rate=Decimal("0.01"),
        slippage_bps=Decimal("100"),
        position_fraction=Decimal("0.5"),
        max_order_value=Decimal("1000"),
        max_position_value=Decimal("1000"),
        max_portfolio_exposure=Decimal("1000"),
    )
    base.update(kwargs)
    return BacktestConfig(**base)


def test_next_bar_execution_costs_and_ledger_reconciliation():
    history = bars([10, 10, 12, 13, 9, 8], [10, 10, 12, 20, 9, 7])
    result = run_backtest({"XYZ": history}, cfg())
    buy = next(o for o in result["orders"] if o["side"] == "BUY")
    assert (
        buy["decision_price"] == "12"
        and buy["decision_time"] == history[2].timestamp.isoformat()
    )
    assert buy["fill_time"] == history[3].timestamp.isoformat()
    assert Decimal(buy["fill_price"]) == Decimal("20.20")
    assert Decimal(buy["fee"]) > 0 and Decimal(buy["slippage_cost"]) > 0
    assert result["fills"] and result["cash_movements"] and result["position_changes"]
    final = Decimal(result["metrics"]["final_equity"])
    last = Decimal(result["equity_curve"][-1]["cash"]) + sum(
        Decimal(p["market_value"]) for p in result["equity_curve"][-1]["positions"]
    )
    assert final == last
    assert (
        result["metrics"]["fees"] != "0" and result["metrics"]["slippage_cost"] != "0"
    )


def test_no_lookahead_decision_stable_when_later_data_changes():
    original = bars([10, 10, 12, 13, 9, 8], [10, 10, 12, 20, 9, 7])
    changed = bars([10, 10, 12, 13, 100, 200], [10, 10, 12, 20, 99, 199])
    a = run_backtest({"XYZ": original}, cfg())
    b = run_backtest({"XYZ": changed}, cfg())
    first_a = next(o for o in a["orders"] if o["side"] == "BUY")
    first_b = next(o for o in b["orders"] if o["side"] == "BUY")
    assert (
        first_a["decision_time"],
        first_a["decision_price"],
        first_a["fill_time"],
        first_a["fill_price"],
    ) == (
        first_b["decision_time"],
        first_b["decision_price"],
        first_b["fill_time"],
        first_b["fill_price"],
    )


def test_costs_reduce_returns_and_benchmark_is_reported():
    data = {"XYZ": bars([10, 10, 12, 13, 9, 8], [10, 10, 12, 13, 9, 8])}
    costly = run_backtest(data, cfg())
    free = run_backtest(data, cfg(commission_rate=Decimal(0), slippage_bps=Decimal(0)))
    assert costly["metrics"]["final_equity"] != free["metrics"]["final_equity"]
    assert costly["metrics"]["buy_hold_return"] is not None
    assert "benchmark_equity" in costly["equity_curve"][-1]
    assert 0 <= costly["metrics"]["max_drawdown"] <= 1


def test_short_history_empty_data_and_unfilled_orders_are_safe():
    short = run_backtest({"XYZ": bars([10, 11])}, cfg())
    assert not short["fills"] and short["metrics"]["completed_trades"] == 0
    empty = run_backtest({}, cfg())
    assert empty["metrics"]["total_return"] == 0 and empty["warnings"]
    one = run_backtest({"XYZ": bars([10, 10, 12])}, cfg())
    assert one["orders"][-1]["status"] == "UNFILLED"


def test_session_filter_and_reproducible_saved_run(tmp_path):
    history = bars([10, 10, 12, 13, 9], [10, 10, 12, 13, 9])
    result = run_backtest({"XYZ": history}, cfg(allowed_weekdays=(0, 1, 2, 3, 4)))
    assert (
        result["run_id"]
        == run_backtest({"XYZ": history}, cfg(allowed_weekdays=(0, 1, 2, 3, 4)))[
            "run_id"
        ]
    )
    repo = BacktestRepository(str(tmp_path / "runs.sqlite3"))
    assert repo.save(result) == result["run_id"]
    assert repo.save(result) == result["run_id"]
    assert repo.get(result["run_id"])["metrics"] == result["metrics"]
    assert len(repo.list_runs()) == 1

    replayed = repo.reproduce(result["run_id"])
    assert replayed["run_id"] == result["run_id"]
    assert replayed["metrics"] == result["metrics"]


def test_metrics_and_additional_costs_are_explicit():
    history = bars([10, 10, 12, 13, 9, 8], [10, 10, 12, 20, 9, 7])
    result = run_backtest({"XYZ": history}, cfg(other_cost_rate=Decimal("0.002")))
    metrics = result["metrics"]
    assert metrics["completed_trades"] == 1
    assert metrics["win_rate"] == 0
    assert Decimal(metrics["average_losing_trade"]) < 0
    assert metrics["annualized_return"] is None
    assert metrics["sharpe_ratio"] is None
    assert Decimal(metrics["commissions"]) > 0
    assert Decimal(metrics["other_costs"]) > 0
    assert (
        len(result["fills"])
        == len(result["cash_movements"])
        == len(result["position_changes"])
    )
    cash_reconciled = Decimal(metrics["initial_capital"]) + sum(
        Decimal(x["cash_delta"]) for x in result["cash_movements"]
    )
    assert cash_reconciled == Decimal(result["equity_curve"][-1]["cash"])


def test_config_rejects_invalid_costs_and_missing_history_is_not_fabricated():
    import pytest

    with pytest.raises(ValueError, match="Other cost rate"):
        cfg(other_cost_rate=Decimal("1"))
    result = run_backtest({}, cfg())
    assert result["fills"] == [] and result["orders"] == []
    assert Decimal(result["metrics"]["final_equity"]) == Decimal("1000")


def test_annualized_return_and_sharpe_require_sufficient_history():
    history = bars([100 + (i % 30) for i in range(270)])
    config = cfg(
        start_date=history[0].timestamp.date(), end_date=history[-1].timestamp.date()
    )
    metrics = run_backtest({"XYZ": history}, config)["metrics"]
    assert metrics["annualized_return"] is not None
    assert metrics["sharpe_ratio"] is not None


def test_session_constraints_delay_order_until_next_allowed_session():
    history = bars([10, 10, 10, 10, 10, 12, 13, 14, 15, 16, 17])
    config = cfg(
        start_date=history[0].timestamp.date(),
        end_date=history[-1].timestamp.date(),
        allowed_weekdays=(0,),
    )
    result = run_backtest({"XYZ": history}, config)
    buy = next(order for order in result["orders"] if order["side"] == "BUY")
    assert datetime.fromisoformat(buy["decision_time"]).weekday() == 0
    assert datetime.fromisoformat(buy["fill_time"]).weekday() == 0
    assert buy["fill_time"] > buy["decision_time"]


def test_drawdown_metric_matches_equity_curve():
    history = bars([10, 10, 12, 13, 9, 8], [10, 10, 12, 20, 9, 7])
    result = run_backtest({"XYZ": history}, cfg())
    peak = Decimal("1000")
    expected = Decimal(0)
    for point in result["equity_curve"]:
        value = Decimal(point["equity"])
        peak = max(peak, value)
        if peak:
            expected = max(expected, (peak - value) / peak)
    assert abs(Decimal(str(result["metrics"]["max_drawdown"])) - expected) < Decimal(
        "0.00000001"
    )
