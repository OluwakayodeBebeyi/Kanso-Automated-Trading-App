"""Execution adapter boundary. This build intentionally has no live adapter."""

from dataclasses import dataclass
from typing import Protocol


class LiveTradingDisabled(RuntimeError):
    """Raised whenever code requests live execution in this paper-only build."""


class BrokerUnavailable(RuntimeError):
    """Raised when a concrete broker has not been selected and configured."""


@dataclass(frozen=True)
class BrokerOrder:
    symbol: str
    side: str
    quantity: str
    order_type: str = "MARKET"
    limit_price: str | None = None
    client_order_id: str = ""


class BrokerAdapter(Protocol):
    """Replaceable integration contract; no implementation is shipped here."""

    def account_snapshot(self): ...

    def submit_order(self, order: BrokerOrder): ...

    def cancel_order(self, broker_order_id: str): ...


class UnconfiguredBrokerAdapter:
    def account_snapshot(self):
        raise BrokerUnavailable("No broker or market has been selected")

    def submit_order(self, order):
        raise LiveTradingDisabled("Live order submission is disabled in this build")

    def cancel_order(self, broker_order_id):
        raise BrokerUnavailable("No broker or market has been selected")


def create_broker_adapter():
    """Return a non-executing adapter until a broker is explicitly selected."""
    return UnconfiguredBrokerAdapter()


def validate_execution_mode(value):
    """Fail closed: configuration alone can never enable live order execution."""
    if not isinstance(value, str) or value.strip().upper() != "PAPER":
        raise LiveTradingDisabled(
            "Only PAPER mode is available; this build has no live-trading authorization path"
        )
    return "PAPER"
