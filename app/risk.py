"""Independent pre-trade risk validation."""

from decimal import Decimal, InvalidOperation

D = Decimal


def _decimal(value):
    try:
        number = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def check_order(
    symbol,
    side,
    quantity,
    market_price,
    fill_price,
    positions,
    marks,
    cash,
    fee,
    max_order,
    max_position,
    max_exposure,
):
    """Fail closed when any value required to calculate risk is invalid."""
    qty = _decimal(quantity)
    market = _decimal(market_price)
    fill = _decimal(fill_price)
    available_cash = _decimal(cash)
    transaction_fee = _decimal(fee)
    order_limit = _decimal(max_order)
    position_limit = _decimal(max_position)
    exposure_limit = _decimal(max_exposure)
    if any(
        value is None
        for value in (
            qty,
            market,
            fill,
            available_cash,
            transaction_fee,
            order_limit,
            position_limit,
            exposure_limit,
        )
    ):
        return "Invalid numeric value; risk cannot be verified"
    if qty <= 0 or market <= 0 or fill <= 0:
        return "Order quantity and prices must be positive"
    if transaction_fee < 0 or min(order_limit, position_limit, exposure_limit) <= 0:
        return "Fees and risk limits must be nonnegative and limits positive"
    if side not in ("BUY", "SELL"):
        return "Unsupported order side"

    exposure = D(0)
    normalized_positions = []
    for held_symbol, raw_quantity in positions:
        held_qty = _decimal(raw_quantity)
        mark = _decimal(marks.get(held_symbol))
        if held_qty is None or held_qty < 0 or mark is None or mark <= 0:
            return "Missing or invalid current price for existing holding; exposure cannot be verified"
        exposure += held_qty * mark
        normalized_positions.append((held_symbol, held_qty))

    if qty * market > order_limit:
        return "Order exceeds maximum order value"
    held = next(
        (q for held_symbol, q in normalized_positions if held_symbol == symbol), D(0)
    )
    if side == "BUY" and (held + qty) * fill > position_limit:
        return "Position exceeds maximum position value"
    if side == "BUY" and exposure + qty * fill > exposure_limit:
        return "Portfolio exposure limit exceeded"
    if side == "BUY" and available_cash < qty * fill + transaction_fee:
        return "Insufficient paper cash"
    if side == "SELL" and qty > held:
        return "Cannot sell more shares than currently held"
    return None
