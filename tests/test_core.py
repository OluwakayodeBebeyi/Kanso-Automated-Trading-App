from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import pytest
from app.market_data import Candle, validate_candles
from app.strategy import evaluate
from app.database import Store
from app.reporting import build_report, daily_report

NOW = datetime.now(timezone.utc)


def candles(values, start=None):
    start = start or NOW - timedelta(days=len(values) - 1)
    return [
        Candle(start + timedelta(days=i), v, v, v, v, 100) for i, v in enumerate(values)
    ]


def make_trade(store, **changes):
    args = dict(
        symbol="XYZ",
        side="BUY",
        quantity=2,
        price=100,
        fee_rate=".01",
        max_order=500,
        max_position=500,
        max_exposure=800,
        prices={"XYZ": 100},
        slippage_bps=50,
        idempotency_key="request-1",
        price_timestamp=NOW,
    )
    args.update(changes)
    return store.trade(**args)


def test_signal():
    assert evaluate("X", candles([1, 2]), 1, 3).direction == "NO_DATA"
    signal = evaluate("X", candles([1, 1, 1, 3]), 1, 3)
    assert signal.short_average == 3 and abs(signal.long_average - (5 / 3)) < 1e-12
    assert (
        signal.direction == "BUY_CANDIDATE"
        and signal.actionable
        and "above" in signal.explanation
    )


def test_data_validation_rejects_stale_duplicate_invalid():
    with pytest.raises(ValueError, match="stale"):
        validate_candles(candles([10], NOW - timedelta(days=5)))
    duplicate = candles([10])
    duplicate.append(duplicate[-1])
    with pytest.raises(ValueError, match="duplicate"):
        validate_candles(duplicate)
    with pytest.raises(ValueError, match="high/low"):
        validate_candles([Candle(NOW, 10, 9, 8, 10, 100)])


def test_fill_costs_risk_idempotency_and_report(tmp_path):
    store = Store(":memory:", 1000)
    filled = make_trade(store)
    assert filled["status"] == "FILLED" and filled["fill_price"] == Decimal("100.5")
    assert filled["fee"] == Decimal("2.010") and filled["slippage"] == Decimal("1.0")
    portfolio = store.portfolio({"XYZ": 100})
    assert (
        portfolio["cash"] == Decimal("796.99000")
        and portfolio["positions"][0]["quantity"] == 2
    )
    assert make_trade(store)["status"] == "DUPLICATE" and len(store.orders()) == 1
    assert len(store.executions()) == 1
    assert [event["state"] for event in store.order_events()] == [
        "CREATED",
        "VALIDATED",
        "FILLED",
    ]
    assert store.reconcile()["complete"] is True
    report = daily_report(store, str(tmp_path), {"XYZ": 100})
    assert report.exists() and "paper trading report" in report.read_text()


def test_failure_scenarios_are_rejected():
    assert (
        make_trade(Store(":memory:", 100), idempotency_key="failure-case")["reason"]
        == "Insufficient paper cash"
    )
    assert (
        make_trade(
            Store(":memory:", 1000), quantity=10, idempotency_key="failure-case"
        )["reason"]
        == "Order exceeds maximum order value"
    )
    with pytest.raises(ValueError, match="stale"):
        make_trade(Store(":memory:", 1000), price_timestamp=NOW - timedelta(days=5))
    with pytest.raises(ValueError, match="positive"):
        make_trade(Store(":memory:", 1000), quantity=0)
    result = Store(":memory:", 1000).trade(
        symbol="XYZ",
        side="SELL",
        quantity=1,
        price=10,
        fee_rate=0,
        max_order=100,
        max_position=100,
        max_exposure=500,
        prices={},
        price_timestamp=NOW,
        idempotency_key="unsellable",
    )
    assert (
        result["status"] == "REJECTED"
        and result["reason"] == "Cannot sell more shares than currently held"
    )


def test_restart_preserves_account_and_order(tmp_path):
    path = str(tmp_path / "account.sqlite3")
    store = Store(path, 1000)
    assert make_trade(store)["status"] == "FILLED"
    restarted = Store(path, 1)
    assert restarted.portfolio({"XYZ": 100})["cash"] == Decimal("796.99000")
    assert restarted.portfolio()["positions"][0]["quantity"] == 2
    assert restarted.orders()[0]["status"] == "FILLED"
    assert restarted.reconcile()["complete"] is True


def test_end_to_end_signal_to_persistent_paper_report(tmp_path):
    path = str(tmp_path / "workflow.sqlite3")
    sample = candles([100, 101, 102, 103])
    validate_candles(sample)
    signal = evaluate("XYZ", sample, 2, 3)
    store = Store(path, 1000)
    store.save_signal(signal)
    assert signal.direction == "BUY_CANDIDATE"
    result = store.trade(
        "XYZ",
        "BUY",
        1,
        signal.price,
        ".001",
        500,
        500,
        800,
        {"XYZ": signal.price},
        5,
        "workflow-1",
        NOW,
        72,
    )
    assert result["status"] == "FILLED"
    after_restart = Store(path, 5000)
    assert (
        after_restart.portfolio({"XYZ": signal.price})["positions"][0]["quantity"] == 1
    )
    report = daily_report(after_restart, str(tmp_path), {"XYZ": signal.price})
    text = report.read_text()
    assert "slippage cost" in text and "unrealized P/L" in text and "FILLED" in text
    with after_restart.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM signals WHERE symbol='XYZ'").fetchone()[0]
            == 1
        )


def test_position_exposure_and_unknown_mark_limits():
    store = Store(":memory:", 1000)
    base = dict(
        side="BUY",
        quantity=1,
        price=100,
        fee_rate=0,
        max_order=500,
        max_position=500,
        max_exposure=500,
        prices={"XYZ": 100},
        price_timestamp=NOW,
        idempotency_key="base-order",
    )
    assert store.trade(symbol="XYZ", **base)["status"] == "FILLED"
    position_limit = store.trade(
        symbol="XYZ",
        side="BUY",
        quantity=1,
        price=100,
        fee_rate=0,
        max_order=500,
        max_position=150,
        max_exposure=500,
        prices={"XYZ": 100},
        price_timestamp=NOW,
        idempotency_key="position-limit",
    )
    assert position_limit["reason"] == "Position exceeds maximum position value"
    exposure_limit = store.trade(
        symbol="ABC",
        side="BUY",
        quantity=1,
        price=100,
        fee_rate=0,
        max_order=500,
        max_position=500,
        max_exposure=150,
        prices={"XYZ": 100, "ABC": 100},
        price_timestamp=NOW,
        idempotency_key="exposure-limit",
    )
    assert exposure_limit["reason"] == "Portfolio exposure limit exceeded"
    missing_mark = store.trade(
        symbol="ABC",
        side="BUY",
        quantity=1,
        price=100,
        fee_rate=0,
        max_order=500,
        max_position=500,
        max_exposure=500,
        prices={"ABC": 100},
        price_timestamp=NOW,
        idempotency_key="missing-mark",
    )
    assert "exposure cannot be verified" in missing_mark["reason"]
    assert store.portfolio()["equity"] is None


def test_backend_risk_rejects_invalid_existing_position_mark():
    store = Store(":memory:", 1000)
    first = make_trade(
        store,
        quantity=2,
        max_order=500,
        max_position=500,
        max_exposure=300,
        idempotency_key="seed-position",
    )
    assert first["status"] == "FILLED"
    result = make_trade(
        store,
        symbol="ABC",
        prices={"XYZ": -100, "ABC": 100},
        max_order=500,
        max_position=500,
        max_exposure=300,
        idempotency_key="bad-mark-attempt",
    )
    assert result["status"] == "REJECTED"
    assert "invalid current price" in result["reason"]
    assert len(store.orders()) == 2
    assert store.portfolio({"XYZ": 100})["positions"][0]["quantity"] == 2


def test_report_scope_and_slippage_export_are_truthful(tmp_path):
    from app.reporting import build_report, export_report

    store = Store(":memory:", 1000)
    assert make_trade(store)["status"] == "FILLED"
    current = date.today()
    outside = build_report(
        store,
        {"XYZ": 100},
        start=current - timedelta(days=2),
        end=current - timedelta(days=1),
    )
    assert outside["orders_count"] == 0 and outside["fees"] == "0"
    assert "CURRENT PAPER PORTFOLIO SNAPSHOT" in outside["snapshot_type"]
    unmarked = build_report(store)
    assert unmarked["unrealized_pnl"] is None
    paths = export_report(
        build_report(store, {"XYZ": 100}), str(tmp_path), ("csv", "html", "json")
    )
    assert {path.suffix for path in paths} == {".csv", ".html", ".json"}
    csv_text = next(path for path in paths if path.suffix == ".csv").read_text()
    assert "slippage cost" in csv_text and "1.000,FILLED" in csv_text


def test_order_submission_requires_idempotency_key():
    store = Store(":memory:", 1000)
    with pytest.raises(ValueError, match="idempotency key"):
        make_trade(store, idempotency_key=None)
    assert store.orders() == []
    assert store.portfolio()["cash"] == Decimal("1000")


def test_kill_switch_blocks_orders_and_persists_after_restart(tmp_path):
    path = str(tmp_path / "kill-switch.sqlite3")
    store = Store(path, 1000)
    enabled = store.set_kill_switch(True, "manual safety stop")
    assert enabled["enabled"] is True
    blocked = make_trade(store, idempotency_key="blocked-order")
    assert blocked["status"] == "REJECTED"
    assert blocked["reason"] == "Kill switch active: manual safety stop"
    assert store.portfolio()["cash"] == Decimal("1000")
    restarted = Store(path, 1000)
    assert restarted.kill_switch_status()["enabled"] is True
    assert (
        make_trade(restarted, idempotency_key="blocked-after-restart")["status"]
        == "REJECTED"
    )
    restarted.set_kill_switch(False)
    assert make_trade(restarted, idempotency_key="after-disable")["status"] == "FILLED"


def test_legacy_database_migrates_without_overwriting_cash(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.sqlite3")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE account(id INTEGER PRIMARY KEY CHECK(id=1),cash TEXT NOT NULL);
        CREATE TABLE positions(symbol TEXT PRIMARY KEY,quantity TEXT NOT NULL,avg_cost TEXT NOT NULL);
        CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity TEXT NOT NULL,price TEXT NOT NULL,fee TEXT NOT NULL,status TEXT NOT NULL,reason TEXT);
        CREATE TABLE signals(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,symbol TEXT NOT NULL,direction TEXT NOT NULL,price TEXT,explanation TEXT NOT NULL);
        INSERT INTO account VALUES(1,'4321.50');
        INSERT INTO positions VALUES('XYZ','2','100');
    """)
    db.commit()
    db.close()
    migrated = Store(path, 9999)
    assert migrated.portfolio()["cash"] == Decimal("4321.50")
    assert migrated.portfolio()["positions"][0]["quantity"] == Decimal("2")
    assert migrated.reconcile()["complete"] is False
    with migrated.connect() as connection:
        assert "idempotency_key" in {
            row[1] for row in connection.execute("PRAGMA table_info(orders)")
        }
        assert "executions" in {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_realized_pnl_includes_entry_and_exit_fees_and_reports(tmp_path):
    store = Store(":memory:", 1000)
    buy = make_trade(store, idempotency_key="realized-buy")
    assert buy["status"] == "FILLED"
    sell = make_trade(
        store,
        side="SELL",
        price=110,
        prices={"XYZ": 110},
        idempotency_key="realized-sell",
    )
    assert sell["status"] == "FILLED"
    expected = Decimal("13.7010")
    assert Decimal(store.executions()[-1]["realized_pnl"]) == expected
    assert store.reconcile()["complete"] is True
    report = build_report(store, {"XYZ": 110})
    assert Decimal(report["realized_pnl"]) == expected
