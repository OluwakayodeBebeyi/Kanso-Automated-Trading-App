import pytest

from app.config import Settings
from app.execution import BrokerUnavailable, LiveTradingDisabled, create_broker_adapter


def test_live_mode_cannot_be_enabled_by_configuration(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "LIVE")
    with pytest.raises(LiveTradingDisabled, match="Only PAPER"):
        Settings.from_env()


def test_broker_boundary_is_non_executing_without_market_selection():
    adapter = create_broker_adapter()
    with pytest.raises(BrokerUnavailable, match="No broker"):
        adapter.account_snapshot()
    with pytest.raises(LiveTradingDisabled, match="disabled"):
        adapter.submit_order(object())
