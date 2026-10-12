"""Paper portfolio report exports."""

import csv
import html
import json
from datetime import date
from decimal import Decimal
from pathlib import Path


def build_report(store, prices=None, *, period="daily", start=None, end=None):
    """Build a current portfolio snapshot with orders filtered to a date range."""
    portfolio = store.portfolio(prices)
    report_start = (
        date.fromisoformat(start) if isinstance(start, str) else start or date.today()
    )
    report_end = (
        date.fromisoformat(end) if isinstance(end, str) else end or date.today()
    )
    if report_start > report_end:
        raise ValueError("Report start must be on or before report end")
    orders = [
        order
        for order in reversed(store.orders())
        if report_start <= date.fromisoformat(order["created_at"][:10]) <= report_end
    ]
    selected_order_ids = {order["id"] for order in orders}
    executions = [
        item for item in store.executions() if item["order_id"] in selected_order_ids
    ]
    expected_fills = sum(order["status"] == "FILLED" for order in orders)
    realized_pnl = (
        str(sum((Decimal(item["realized_pnl"]) for item in executions), Decimal(0)))
        if len(executions) == expected_fills
        else None
    )
    holdings = []
    unrealized = Decimal(0)
    all_marks_available = True
    for position in portfolio["positions"]:
        mark = position["market_price"]
        pnl = (
            (mark - position["avg_cost"]) * position["quantity"]
            if mark is not None
            else None
        )
        if pnl is not None:
            unrealized += pnl
        else:
            all_marks_available = False
        holdings.append(
            {
                "symbol": position["symbol"],
                "quantity": str(position["quantity"]),
                "average_cost": str(position["avg_cost"]),
                "market_price": str(mark) if mark is not None else None,
                "market_value": str(position["market_value"])
                if position["market_value"] is not None
                else None,
                "unrealized_pnl": str(pnl) if pnl is not None else None,
            }
        )
    return {
        "label": "PAPER TRADING REPORT",
        "period": period,
        "start": report_start.isoformat(),
        "end": report_end.isoformat(),
        "snapshot_type": "CURRENT PAPER PORTFOLIO SNAPSHOT; orders filtered to report period",
        "cash": str(portfolio["cash"]),
        "equity": str(portfolio["equity"]) if portfolio["equity"] is not None else None,
        "unrealized_pnl": str(unrealized) if all_marks_available else None,
        "realized_pnl": realized_pnl,
        "orders_count": len(orders),
        "filled_orders": sum(o["status"] == "FILLED" for o in orders),
        "rejected_orders": sum(o["status"] == "REJECTED" for o in orders),
        "fees": str(sum((Decimal(o["fee"]) for o in orders), Decimal(0))),
        "holdings": holdings,
        "orders": orders,
        "limitations": "Snapshot values reflect the current paper portfolio. Historical start/end valuations, cash flows, and realized P/L cannot be reconstructed from the current ledger.",
    }


def export_report(report, output_dir="reports", formats=("csv",)):
    """Export report data as CSV, JSON, and/or HTML without external services."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    stem = f"{report['period']}-{report['end']}"
    outputs = []
    for fmt in formats:
        path = root / f"{stem}.{fmt}"
        if fmt == "json":
            path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        elif fmt == "html":
            rows = "".join(
                f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
                for k, v in report.items()
                if k not in ("orders", "holdings")
            )
            path.write_text(
                "<!doctype html><meta charset='utf-8'><title>Paper report</title><h1>PAPER TRADING REPORT</h1><table>"
                + rows
                + "</table>",
                encoding="utf-8",
            )
        elif fmt == "csv":
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "paper trading report",
                        report["period"],
                        report["start"],
                        report["end"],
                    ]
                )
                writer.writerow(["metric", "value"])
                for key, value in report.items():
                    if key not in ("orders", "holdings"):
                        writer.writerow([key, value])
                writer.writerow([])
                writer.writerow(
                    [
                        "order id",
                        "created at",
                        "symbol",
                        "side",
                        "quantity",
                        "fill price",
                        "fee",
                        "slippage cost",
                        "status",
                        "reason",
                    ]
                )
                for order in report["orders"]:
                    writer.writerow(
                        [
                            order.get(k)
                            for k in (
                                "id",
                                "created_at",
                                "symbol",
                                "side",
                                "quantity",
                                "price",
                                "fee",
                                "slippage",
                                "status",
                                "reason",
                            )
                        ]
                    )
                writer.writerow([])
                writer.writerow(
                    [
                        "symbol",
                        "quantity",
                        "average cost",
                        "market price",
                        "market value",
                        "unrealized P/L",
                    ]
                )
                for holding in report["holdings"]:
                    writer.writerow(list(holding.values()))
        else:
            raise ValueError(f"Unsupported report format: {fmt}")
        outputs.append(path)
    return outputs


def daily_report(store, output_dir="reports", prices=None):
    report = build_report(store, prices, period="daily")
    return export_report(report, output_dir, ("csv",))[0]
