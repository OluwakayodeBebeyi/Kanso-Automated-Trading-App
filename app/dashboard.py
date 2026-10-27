import streamlit as st
import uuid
from datetime import date, timedelta
from decimal import Decimal
import pandas as pd
from app.backtesting import BacktestConfig, BacktestRepository, run_backtest
from app.config import Settings
from app.database import Store
from app.market_data import YahooFinanceProvider
from app.strategy import evaluate
from app.reporting import daily_report


@st.cache_data(ttl=300, show_spinner=False)
def market_history(symbol, period="1y", start=None, end=None, max_age_hours=72):
    return YahooFinanceProvider(max_age_hours).history(
        symbol, period=period, start=start, end=end
    )


@st.cache_resource
def resources():
    config = Settings.from_env()
    return (
        config,
        Store(config.database_path, config.initial_cash),
        YahooFinanceProvider(config.max_data_age_hours),
    )


settings, store, provider = resources()
st.set_page_config(
    page_title="Kanso Consulting | Paper Trading Analysis",
    page_icon="public/kanso-icon.png",
    layout="wide",
)
st.title("Market Analysis and Paper Trading")
st.info("PAPER TRADING - simulated orders only. No live broker integration exists.")
with st.sidebar:
    st.image("public/kanso-logo.png", width=240)
    st.subheader("Paper trading safety")
    kill_state = store.kill_switch_status()
    if kill_state["enabled"]:
        st.error(f"Kill switch enabled: {kill_state['reason']}")
        with st.form("disable_paper_kill_switch"):
            confirm_disable = st.checkbox(
                "I confirm that paper order entry should be re-enabled"
            )
            disable_requested = st.form_submit_button("Disable kill switch")
        if disable_requested and confirm_disable:
            store.set_kill_switch(False)
            st.rerun()
    elif st.button("Enable kill switch"):
        store.set_kill_switch(True, "Enabled from dashboard")
        st.rerun()
watchlist = st.text_input("Watchlist (comma separated)", ",".join(settings.watchlist))
symbols = [s.strip().upper() for s in watchlist.split(",") if s.strip()]
signals = []
prices = {}
timestamps = {}
for symbol in symbols:
    try:
        candles = market_history(symbol, max_age_hours=settings.max_data_age_hours)
        prices[symbol] = candles[-1].close
        timestamps[symbol] = candles[-1].timestamp
        signal = evaluate(symbol, candles, settings.short_window, settings.long_window)
        store.save_signal(signal)
        signals.append(signal)
    except Exception as exc:
        st.warning(f"{symbol}: market data unavailable or invalid: {exc}")
st.subheader("Signals")
if signals:
    st.dataframe(
        [
            {
                "Symbol": x.symbol,
                "Direction": x.direction,
                "Price": x.price,
                "Short SMA": x.short_average,
                "Long SMA": x.long_average,
                "Reason": x.explanation,
            }
            for x in signals
        ],
        width="stretch",
    )
else:
    st.caption(
        "No valid fresh market data; orders for unavailable symbols are disabled."
    )
portfolio = store.portfolio(prices)
left, right = st.columns(2)
left.metric("Paper cash", f"${portfolio['cash']:,.2f}")
right.metric(
    "Estimated equity",
    f"${portfolio['equity']:,.2f}"
    if portfolio["equity"] is not None
    else "Unavailable",
)
st.subheader("Positions")
st.dataframe(portfolio["positions"], width="stretch")
token = st.session_state.setdefault("paper_order_token", uuid.uuid4().hex)
with st.form("paper_order"):
    st.caption(
        "Orders fill at the latest valid downloaded close plus configured slippage and fees. Sell orders only reduce long holdings."
    )
    selected = st.selectbox("Symbol", symbols or ["AAPL"])
    side = st.selectbox("Side", ["BUY", "SELL"])
    quantity = st.number_input("Shares", min_value=0.01, value=1.0, step=1.0)
    submitted = st.form_submit_button("Submit paper order")
if submitted:
    try:
        if selected not in prices:
            raise ValueError(
                "Fresh market price is required; no simulated fallback price is used"
            )
        result = store.trade(
            selected,
            side,
            quantity,
            prices[selected],
            settings.fee_rate,
            settings.max_order_value,
            settings.max_position_value,
            settings.max_portfolio_exposure,
            prices,
            settings.slippage_bps,
            token,
            timestamps[selected],
            settings.max_data_age_hours,
        )
        if result["status"] == "FILLED":
            st.success(
                f"Paper fill: {result['fill_price']} (fee {result['fee']}, slippage cost {result['slippage']})"
            )
        else:
            st.error(result.get("reason", "Duplicate request safely ignored"))
    except Exception as exc:
        st.error(str(exc))
    finally:
        st.session_state["paper_order_token"] = uuid.uuid4().hex
st.subheader("Paper order history")
st.dataframe(store.orders(), width="stretch")
if st.button("Generate daily report"):
    st.success(f"Saved paper report: {daily_report(store, prices=prices)}")
st.header("Historical backtesting")
st.caption(
    "Historical simulation only. Backtest results do not predict or guarantee future performance."
)

repository = BacktestRepository(settings.database_path)
with st.form("backtest_parameters"):
    backtest_symbols = st.multiselect(
        "Instruments",
        options=symbols or list(settings.watchlist),
        default=symbols or list(settings.watchlist),
    )
    today = date.today()
    start_date = st.date_input(
        "Start date", today - timedelta(days=365), key="bt_start"
    )
    end_date = st.date_input("End date", today, key="bt_end")
    initial_capital = st.number_input(
        "Initial capital", min_value=1.0, value=float(settings.initial_cash), step=100.0
    )
    short_window = st.number_input(
        "Short moving average", min_value=1, value=settings.short_window, step=1
    )
    long_window = st.number_input(
        "Long moving average", min_value=2, value=settings.long_window, step=1
    )
    commission = st.number_input(
        "Commission rate (fraction)",
        min_value=0.0,
        value=float(settings.fee_rate),
        format="%.5f",
    )
    other_cost_rate = st.number_input(
        "Other transaction cost rate (fraction)",
        min_value=0.0,
        value=0.0,
        format="%.5f",
    )
    slippage = st.number_input(
        "Slippage (basis points)",
        min_value=0.0,
        value=float(settings.slippage_bps),
        step=1.0,
    )
    position_fraction = st.number_input(
        "Equity allocation per entry",
        min_value=0.01,
        max_value=1.0,
        value=0.95,
        step=0.05,
    )
    session_names = st.multiselect(
        "Allowed trading sessions",
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        default=["Mon", "Tue", "Wed", "Thu", "Fri"],
    )
    run_requested = st.form_submit_button("Run historical simulation")

if run_requested:
    try:
        if not backtest_symbols:
            raise ValueError("Select at least one instrument")
        if short_window >= long_window:
            raise ValueError("Long moving average must exceed short moving average")
        if start_date > end_date:
            raise ValueError("Start date must be on or before end date")
        weekdays = tuple(
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index(x)
            for x in session_names
        )
        config = BacktestConfig(
            symbols=tuple(backtest_symbols),
            start_date=start_date,
            end_date=end_date,
            initial_capital=Decimal(str(initial_capital)),
            short_window=int(short_window),
            long_window=int(long_window),
            commission_rate=Decimal(str(commission)),
            other_cost_rate=Decimal(str(other_cost_rate)),
            slippage_bps=Decimal(str(slippage)),
            position_fraction=Decimal(str(position_fraction)),
            max_order_value=settings.max_order_value,
            max_position_value=settings.max_position_value,
            max_portfolio_exposure=settings.max_portfolio_exposure,
            allowed_weekdays=weekdays,
        )
        history = {}
        warmup_start = start_date - timedelta(days=int(long_window) * 3)
        for ticker in config.symbols:
            try:
                history[ticker] = market_history(
                    ticker,
                    max_age_hours=settings.max_data_age_hours,
                    start=warmup_start.isoformat(),
                    end=(end_date + timedelta(days=1)).isoformat(),
                )
            except Exception as error:
                st.warning(f"{ticker}: historical data unavailable: {error}")
        result = run_backtest(history, config)
        repository.save(result)
        st.session_state["latest_backtest_id"] = result["run_id"]
    except Exception as error:
        st.error(f"Backtest could not run: {error}")

saved_runs = repository.list_runs()
run_ids = [run["run_id"] for run in saved_runs]
preferred = st.session_state.get("latest_backtest_id")
if run_ids:
    default_index = run_ids.index(preferred) if preferred in run_ids else 0
    chosen_run = st.selectbox(
        "Saved historical simulations", run_ids, index=default_index
    )
    selected_result = repository.get(chosen_run)
    if st.button("Reproduce selected simulation from saved input candles"):
        reproduced = repository.reproduce(chosen_run)
        st.success(
            f"Reproduced run {reproduced['run_id']} from its saved data and settings"
        )
    if selected_result:
        st.info(selected_result["label"])
        metrics = selected_result["metrics"]
        columns = st.columns(4)
        columns[0].metric("Total return", f"{metrics['total_return']:.2%}")
        columns[1].metric("Max drawdown", f"{metrics['max_drawdown']:.2%}")
        columns[2].metric("Completed trades", metrics["completed_trades"])
        columns[3].metric("Buy-and-hold return", f"{metrics['buy_hold_return']:.2%}")
        st.json(
            {
                key: metrics[key]
                for key in (
                    "annualized_return",
                    "win_rate",
                    "average_winning_trade",
                    "average_losing_trade",
                    "profit_factor",
                    "sharpe_ratio",
                    "fees",
                    "commissions",
                    "other_costs",
                    "slippage_cost",
                    "final_equity",
                )
            }
        )
        equity_frame = pd.DataFrame(selected_result["equity_curve"])
        if not equity_frame.empty:
            equity_frame = equity_frame.set_index("date")[
                ["equity", "benchmark_equity"]
            ].apply(pd.to_numeric)
            st.subheader("Equity curve vs equal-weight buy-and-hold")
            st.line_chart(equity_frame)
            st.subheader("Drawdown")
            st.line_chart(
                pd.DataFrame(
                    {
                        "drawdown": [
                            float(point["drawdown"])
                            for point in selected_result["equity_curve"]
                        ]
                    },
                    index=[point["date"] for point in selected_result["equity_curve"]],
                )
            )
        st.subheader("Orders and fills")
        st.dataframe(selected_result["orders"], width="stretch")
        st.subheader("Completed trades")
        st.dataframe(selected_result["trades"], width="stretch")
        if selected_result["warnings"]:
            for warning in selected_result["warnings"]:
                st.warning(warning)
else:
    st.caption("No saved historical simulations yet.")
