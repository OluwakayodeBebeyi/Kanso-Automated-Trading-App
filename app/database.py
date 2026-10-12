"""SQLite paper ledger; no live broker adapter exists."""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from app.risk import check_order

D = Decimal


class Store:
    def __init__(self, path, initial_cash):
        self.path = path
        self._memory = sqlite3.connect(":memory:") if path == ":memory:" else None
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1),cash TEXT NOT NULL,opening_cash TEXT NOT NULL DEFAULT '0');
            CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY,quantity TEXT NOT NULL,avg_cost TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity TEXT NOT NULL,price TEXT NOT NULL,fee TEXT NOT NULL,status TEXT NOT NULL,reason TEXT);
            CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,symbol TEXT NOT NULL,direction TEXT NOT NULL,price TEXT,explanation TEXT NOT NULL);""")
            account_cols = {r[1] for r in db.execute("PRAGMA table_info(account)")}
            if "opening_cash" not in account_cols:
                db.execute(
                    "ALTER TABLE account ADD COLUMN opening_cash TEXT NOT NULL DEFAULT '0'"
                )
                db.execute("UPDATE account SET opening_cash=cash WHERE id=1")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS order_events(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,details TEXT);
                CREATE TABLE IF NOT EXISTS executions(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL UNIQUE,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity TEXT NOT NULL,fill_price TEXT NOT NULL,fee TEXT NOT NULL,slippage TEXT NOT NULL,realized_pnl TEXT NOT NULL DEFAULT '0',executed_at TEXT NOT NULL,mode TEXT NOT NULL CHECK(mode='PAPER'));
                CREATE TABLE IF NOT EXISTS cash_movements(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL UNIQUE,amount TEXT NOT NULL,balance_after TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS position_movements(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL UNIQUE,symbol TEXT NOT NULL,quantity_delta TEXT NOT NULL,quantity_after TEXT NOT NULL,created_at TEXT NOT NULL);
            """)
            execution_cols = {r[1] for r in db.execute("PRAGMA table_info(executions)")}
            if "realized_pnl" not in execution_cols:
                db.execute(
                    "ALTER TABLE executions ADD COLUMN realized_pnl TEXT NOT NULL DEFAULT '0'"
                )
            cols = {r[1] for r in db.execute("PRAGMA table_info(orders)")}
            if "slippage" not in cols:
                db.execute(
                    "ALTER TABLE orders ADD COLUMN slippage TEXT NOT NULL DEFAULT '0'"
                )
            if "idempotency_key" not in cols:
                db.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency ON orders(idempotency_key)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS trading_controls(id INTEGER PRIMARY KEY CHECK(id=1),kill_switch INTEGER NOT NULL,reason TEXT,updated_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO trading_controls VALUES(1,0,NULL,?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.execute(
                "INSERT OR IGNORE INTO account(id,cash,opening_cash) VALUES(1,?,?)",
                (str(D(str(initial_cash))), str(D(str(initial_cash)))),
            )

    def connect(self):
        db = self._memory if self._memory is not None else sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def portfolio(self, prices=None):
        prices = prices or {}
        with self.connect() as db:
            cash = D(db.execute("SELECT cash FROM account WHERE id=1").fetchone()[0])
            rows = list(db.execute("SELECT * FROM positions ORDER BY symbol"))
        ps = []
        for r in rows:
            qty = D(r["quantity"])
            mark = D(str(prices[r["symbol"]])) if r["symbol"] in prices else None
            ps.append(
                {
                    "symbol": r["symbol"],
                    "quantity": qty,
                    "avg_cost": D(r["avg_cost"]),
                    "market_price": mark,
                    "market_value": qty * mark if mark is not None else None,
                }
            )
        complete_marks = all(p["market_value"] is not None for p in ps)
        return {
            "cash": cash,
            "positions": ps,
            "equity": cash + sum((p["market_value"] or D(0) for p in ps), D(0))
            if complete_marks
            else None,
        }

    def executions(self):
        with self.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM executions ORDER BY id")
            ]

    def order_events(self, order_id=None):
        with self.connect() as db:
            if order_id is None:
                rows = db.execute("SELECT * FROM order_events ORDER BY id")
            else:
                rows = db.execute(
                    "SELECT * FROM order_events WHERE order_id=? ORDER BY id",
                    (order_id,),
                )
            return [dict(row) for row in rows]

    def reconcile(self):
        with self.connect() as db:
            account = db.execute(
                "SELECT cash,opening_cash FROM account WHERE id=1"
            ).fetchone()
            cash_rows = list(db.execute("SELECT amount FROM cash_movements"))
            position_rows = list(
                db.execute("SELECT symbol,quantity_delta FROM position_movements")
            )
            live_positions = {
                row["symbol"]: D(row["quantity"])
                for row in db.execute("SELECT symbol,quantity FROM positions")
            }
            filled_count = db.execute(
                "SELECT COUNT(*) FROM orders WHERE status='FILLED'"
            ).fetchone()[0]
            movement_count = db.execute(
                "SELECT COUNT(*) FROM cash_movements"
            ).fetchone()[0]
        cash_reconstructed = D(account["opening_cash"]) + sum(
            (D(row["amount"]) for row in cash_rows), D(0)
        )
        moved_positions = {}
        for row in position_rows:
            moved_positions[row["symbol"]] = moved_positions.get(
                row["symbol"], D(0)
            ) + D(row["quantity_delta"])
        moved_positions = {
            symbol: qty for symbol, qty in moved_positions.items() if qty
        }
        complete = (
            movement_count == filled_count
            and moved_positions == live_positions
            and cash_reconstructed == D(account["cash"])
        )
        return {
            "complete": complete,
            "cash_matches": cash_reconstructed == D(account["cash"]),
            "positions_match": moved_positions == live_positions,
            "filled_orders": filled_count,
            "recorded_cash_movements": movement_count,
            "cash_balance": str(account["cash"]),
            "reconstructed_cash": str(cash_reconstructed),
        }

    def save_signal(self, s):
        with self.connect() as db:
            db.execute(
                "INSERT INTO signals(created_at,symbol,direction,price,explanation) VALUES(?,?,?,?,?)",
                (
                    s.evaluated_at.isoformat(),
                    s.symbol,
                    s.direction,
                    str(s.price) if s.price is not None else None,
                    s.explanation,
                ),
            )

    def orders(self):
        with self.connect() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM orders ORDER BY id DESC")
            ]

    def kill_switch_status(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT kill_switch,reason,updated_at FROM trading_controls WHERE id=1"
            ).fetchone()
        return {
            "enabled": bool(row["kill_switch"]),
            "reason": row["reason"],
            "updated_at": row["updated_at"],
        }

    def set_kill_switch(self, enabled, reason=None):
        if not isinstance(enabled, bool):
            raise TypeError("Kill switch state must be boolean")
        if enabled and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("A reason is required when enabling the kill switch")
        with self.connect() as db:
            db.execute(
                "UPDATE trading_controls SET kill_switch=?,reason=?,updated_at=? WHERE id=1",
                (
                    int(enabled),
                    reason.strip() if enabled else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return self.kill_switch_status()

    def trade(
        self,
        symbol,
        side,
        quantity,
        price,
        fee_rate,
        max_order,
        max_position,
        max_exposure,
        prices=None,
        slippage_bps=0,
        idempotency_key=None,
        price_timestamp=None,
        max_age_hours=72,
    ):
        if not isinstance(symbol, str) or not isinstance(side, str):
            raise ValueError("Symbol and side must be text")
        symbol = symbol.strip().upper()
        side = side.strip().upper()
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("A nonempty idempotency key is required")
        idempotency_key = idempotency_key.strip()
        try:
            qty = D(str(quantity))
            market = D(str(price))
            fee_pct = D(str(fee_rate))
            slip = D(str(slippage_bps))
        except (InvalidOperation, ValueError) as e:
            raise ValueError("Order values must be valid numbers") from e
        if (
            not symbol
            or side not in ("BUY", "SELL")
            or any(not x.is_finite() for x in (qty, market, fee_pct, slip))
            or qty <= 0
            or market <= 0
        ):
            raise ValueError(
                "Provide symbol, BUY/SELL, and finite positive quantity and price"
            )
        if fee_pct < 0 or slip < 0:
            raise ValueError("Fees and slippage cannot be negative")
        if price_timestamp is None:
            raise ValueError("A timestamped market price is required")
        stamp = (
            price_timestamp
            if price_timestamp.tzinfo
            else price_timestamp.replace(tzinfo=timezone.utc)
        )
        age = (
            datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
        ).total_seconds() / 3600
        if age < -0.1 or age > max_age_hours:
            raise ValueError("Market price is stale or from the future")
        fill = market * (
            D(1) + slip / D(10000) if side == "BUY" else D(1) - slip / D(10000)
        )
        fee = qty * fill * fee_pct
        slip_cost = qty * abs(fill - market)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                old = db.execute(
                    "SELECT id FROM orders WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if old:
                    return {
                        "status": "DUPLICATE",
                        "existing_order_id": old["id"],
                        "reason": "Idempotency key already processed",
                    }
            cash = D(db.execute("SELECT cash FROM account WHERE id=1").fetchone()[0])
            rows = db.execute("SELECT symbol,quantity FROM positions").fetchall()
            control = db.execute(
                "SELECT kill_switch,reason FROM trading_controls WHERE id=1"
            ).fetchone()
            reason = (
                ("Kill switch active: " + (control["reason"] or "no reason provided"))
                if control["kill_switch"]
                else check_order(
                    symbol,
                    side,
                    qty,
                    market,
                    fill,
                    [(r["symbol"], r["quantity"]) for r in rows],
                    prices or {},
                    cash,
                    fee,
                    max_order,
                    max_position,
                    max_exposure,
                )
            )
            status = "REJECTED" if reason else "FILLED"
            realized_pnl = D(0)
            if status == "FILLED":
                old = db.execute(
                    "SELECT quantity,avg_cost FROM positions WHERE symbol=?", (symbol,)
                ).fetchone()
                old_q = D(old["quantity"]) if old else D(0)
                old_avg = D(old["avg_cost"]) if old else D(0)
                db.execute(
                    "UPDATE account SET cash=? WHERE id=1",
                    (
                        str(
                            cash - qty * fill - fee
                            if side == "BUY"
                            else cash + qty * fill - fee
                        ),
                    ),
                )
                if side == "BUY":
                    new_q = old_q + qty
                    db.execute(
                        "INSERT OR REPLACE INTO positions VALUES(?,?,?)",
                        (
                            symbol,
                            str(new_q),
                            str((old_q * old_avg + qty * fill + fee) / new_q),
                        ),
                    )
                else:
                    realized_pnl = qty * fill - fee - qty * old_avg
                    remain = old_q - qty
                    if remain == 0:
                        db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
                    else:
                        db.execute(
                            "UPDATE positions SET quantity=? WHERE symbol=?",
                            (str(remain), symbol),
                        )
            order_time = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT INTO orders(created_at,symbol,side,quantity,price,fee,slippage,status,reason,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    order_time,
                    symbol,
                    side,
                    str(qty),
                    str(fill),
                    str(fee if status == "FILLED" else D(0)),
                    str(slip_cost if status == "FILLED" else D(0)),
                    status,
                    reason,
                    idempotency_key,
                ),
            )
            oid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for event_state in ("CREATED", "VALIDATED", status):
                db.execute(
                    "INSERT INTO order_events(order_id,state,created_at,details) VALUES(?,?,?,?)",
                    (
                        oid,
                        event_state,
                        order_time,
                        reason if event_state == status and reason else None,
                    ),
                )
            if status == "FILLED":
                cash_after = (
                    cash - qty * fill - fee
                    if side == "BUY"
                    else cash + qty * fill - fee
                )
                quantity_after = old_q + qty if side == "BUY" else old_q - qty
                quantity_delta = qty if side == "BUY" else -qty
                db.execute(
                    "INSERT INTO executions(order_id,symbol,side,quantity,fill_price,fee,slippage,realized_pnl,executed_at,mode) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        oid,
                        symbol,
                        side,
                        str(qty),
                        str(fill),
                        str(fee),
                        str(slip_cost),
                        str(realized_pnl),
                        order_time,
                        "PAPER",
                    ),
                )
                db.execute(
                    "INSERT INTO cash_movements(order_id,amount,balance_after,created_at) VALUES(?,?,?,?)",
                    (oid, str(cash_after - cash), str(cash_after), order_time),
                )
                db.execute(
                    "INSERT INTO position_movements(order_id,symbol,quantity_delta,quantity_after,created_at) VALUES(?,?,?,?,?)",
                    (oid, symbol, str(quantity_delta), str(quantity_after), order_time),
                )
        return {
            "status": status,
            "order_id": oid,
            "fill_price": fill if status == "FILLED" else None,
            "fee": fee if status == "FILLED" else D(0),
            "slippage": slip_cost if status == "FILLED" else D(0),
            "reason": reason,
        }
