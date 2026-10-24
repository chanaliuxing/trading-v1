
import datetime
from typing import Any, Dict, List, Optional

NY_TZ = datetime.timezone.utc # Use UTC for generic strategy logic


class QQQPutSpreadSuggestion:
    """Represents a suggested QQQ Put Spread trade."""
    def __init__(self, status: str, short_strike: float = 0.0, long_strike: float = 0.0,
                 width: float = 0.0, reason: str = "", notes: Optional[List[str]] = None):
        self.status = status
        self.short_strike = short_strike
        self.long_strike = long_strike
        self.width = width
        self.reason = reason
        self.notes = notes if notes is not None else []

def qqq_put_spread_suggest(
    price: float,
    ma50: float,
    ma100: float,
    ma200: float,
    trend: str,
    time_et: str, # HH:MM string
    long_leg_quotes: Optional[Dict[str, Any]] = None, # For sanity checks
) -> QQQPutSpreadSuggestion:
    """
    Generates a QQQ Put Spread suggestion based on market conditions.
    This is a simplified example.
    """
    if trend == "down" and price < ma50 and price < ma100:
        # Bearish trend, price below key moving averages
        # Suggest an OTM put spread
        short_strike = price * 0.95 # 5% OTM
        long_strike = price * 0.90  # 10% OTM
        width = short_strike - long_strike

        # Further refinement based on time of day
        hour = int(time_et.split(":")[0])
        if 9 <= hour < 12: # Morning session
            reason = "bearish_morning_otm_put_spread"
        else:
            reason = "bearish_afternoon_otm_put_spread"

        return QQQPutSpreadSuggestion(
            status="ready",
            short_strike=round(short_strike / 2.5) * 2.5, # Round to nearest $2.5 strike
            long_strike=round(long_strike / 2.5) * 2.5,
            width=round(width, 2),
            reason=reason
        )
    elif trend == "up" and price > ma50 and price > ma100:
        # Bullish trend, price above key moving averages
        # Suggest a credit put spread (selling puts below market)
        short_strike = price * 0.975 # Slightly OTM put
        long_strike = price * 0.95   # Further OTM put
        width = short_strike - long_strike

        reason = "uptrend_otm_put_spread"
        return QQQPutSpreadSuggestion(
            status="ready",
            short_strike=round(short_strike / 2.5) * 2.5,
            long_strike=round(long_strike / 2.5) * 2.5,
            width=round(width, 2),
            reason=reason
        )
    else:
        return QQQPutSpreadSuggestion(status="no_clear_signal", reason="market_neutral_or_unclear_trend")
