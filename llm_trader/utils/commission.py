"""Commission calculation utilities

Fee structure:
- 0.01% (万分之一) per trade (both buy and sell)
- Minimum fee: $1.00 per trade
"""

def calculate_commission(trade_value: float) -> float:
    """Calculate commission for a trade

    Args:
        trade_value: Dollar value of the trade (price * quantity)

    Returns:
        Commission amount in dollars

    Examples:
        >>> calculate_commission(10000)  # $10,000 trade
        1.0  # 0.01% = $1.00 (at minimum)

        >>> calculate_commission(50000)  # $50,000 trade
        5.0  # 0.01% = $5.00

        >>> calculate_commission(5000)   # $5,000 trade
        1.0  # 0.01% = $0.50, but minimum is $1.00
    """
    fee = trade_value * 0.0001  # 0.01% = 0.0001
    return max(fee, 1.0)  # Minimum $1.00


def estimate_round_trip_commission(price: float, quantity: int) -> float:
    """Estimate total commission for a round trip (buy + sell)

    Args:
        price: Expected price per share
        quantity: Number of shares

    Returns:
        Total commission for buy + sell

    Examples:
        >>> estimate_round_trip_commission(100, 50)  # 50 shares @ $100
        2.0  # $1 buy + $1 sell

        >>> estimate_round_trip_commission(500, 200)  # 200 shares @ $500
        20.0  # $10 buy + $10 sell
    """
    trade_value = price * quantity
    one_way = calculate_commission(trade_value)
    return one_way * 2  # Buy + Sell


def commission_as_percentage(price: float, quantity: int) -> float:
    """Calculate round-trip commission as percentage of trade value

    Args:
        price: Price per share
        quantity: Number of shares

    Returns:
        Round-trip commission as percentage (e.g., 0.02 = 2%)

    Examples:
        >>> commission_as_percentage(100, 10)  # $1,000 trade
        0.2  # $2 commission on $1,000 = 0.2%

        >>> commission_as_percentage(100, 500)  # $50,000 trade
        0.02  # $10 commission on $50,000 = 0.02%
    """
    trade_value = price * quantity
    if trade_value == 0:
        return 0.0
    total_commission = estimate_round_trip_commission(price, quantity)
    return (total_commission / trade_value) * 100


def breakeven_move_bps(price: float, quantity: int) -> float:
    """Calculate price move in basis points needed to break even after commission

    Args:
        price: Entry price per share
        quantity: Number of shares

    Returns:
        Basis points (1 bp = 0.01%) price move needed to break even

    Examples:
        >>> breakeven_move_bps(100, 100)  # $10,000 trade
        2.0  # Need 2 bps (0.02%) move to cover $2 commission

        >>> breakeven_move_bps(500, 1000)  # $500,000 trade
        1.0  # Need 1 bp (0.01%) move to cover $50 commission
    """
    if price == 0:
        return 0.0
    total_commission = estimate_round_trip_commission(price, quantity)
    cost_basis = price * quantity
    breakeven_pct = (total_commission / cost_basis) * 100  # Percentage
    return breakeven_pct * 100  # Convert to basis points (1% = 100 bps)


if __name__ == "__main__":
    # Test cases
    print("Commission Calculator Test")
    print("=" * 60)

    test_cases = [
        (100, 10, "Small trade - min commission applies"),
        (100, 100, "Medium trade"),
        (100, 500, "Large trade"),
        (440, 22, "TSLA example from tests"),
        (175, 285, "Real opening trade"),
    ]

    for price, qty, desc in test_cases:
        trade_value = price * qty
        one_way = calculate_commission(trade_value)
        round_trip = estimate_round_trip_commission(price, qty)
        pct = commission_as_percentage(price, qty)
        bps = breakeven_move_bps(price, qty)

        print(f"\n{desc}")
        print(f"  Trade: {qty} shares @ ${price:.2f} = ${trade_value:,.2f}")
        print(f"  Commission (one-way): ${one_way:.2f}")
        print(f"  Commission (round-trip): ${round_trip:.2f}")
        print(f"  As % of trade value: {pct:.3f}%")
        print(f"  Breakeven move needed: {bps:.1f} bps ({pct/2:.3f}%)")
