"""V2.0 Trading Helper Functions

Helper functions for LLM trading bot V2.0 features
"""

from typing import Dict, Any, Optional
from datetime import datetime, time as dt_time


def should_execute_reflection(
    last_reflection_time: Optional[datetime],
    current_time: datetime,
    market_phase: str,
) -> bool:
    """Determine if reflection should be triggered

    Args:
        last_reflection_time: Last time reflection was executed
        current_time: Current time
        market_phase: Current market phase

    Returns:
        True if reflection should be triggered
    """
    # Trigger at midday (12:00 ET)
    if market_phase == "MIDDAY":
        current_hour = current_time.hour
        if current_hour == 12 and current_time.minute < 5:
            if last_reflection_time is None or last_reflection_time.hour < 12:
                return True

    # Trigger at end of day (after market close, 16:05 ET)
    if market_phase == "AFTERHOURS":
        current_hour = current_time.hour
        if current_hour == 16 and current_time.minute >= 5 and current_time.minute < 10:
            if last_reflection_time is None or last_reflection_time.hour < 16:
                return True

    return False


def get_market_phase_description(phase: str, mins_to_close: Optional[int] = None) -> str:
    """Get human-readable market phase description

    Args:
        phase: Market phase enum value
        mins_to_close: Minutes to market close (if applicable)

    Returns:
        Human-readable description
    """
    descriptions = {
        "PREMARKET": "Premarket trading - prepare for market open",
        "OPEN": "Market just opened - high volatility period",
        "MIDDAY": "Midday trading - normal market conditions",
        "CLOSE": "Market closing soon - consider closing positions",
        "AFTERHOURS": "After-hours trading - lower liquidity",
        "CLOSED": "Market is closed",
    }

    desc = descriptions.get(phase, "Unknown market phase")

    if phase == "CLOSE" and mins_to_close:
        desc += f" ({mins_to_close} minutes remaining)"

    return desc


def compress_market_features(market_data: Dict[str, Any]) -> Dict[str, Any]:
    """Compress market data to reduce token usage

    Args:
        market_data: Full market data dict

    Returns:
        Compressed market data
    """
    compressed = {
        "symbol": market_data.get("symbol"),
        "timestamp": market_data.get("timestamp"),
        "current_price": market_data.get("current_price", 0),
    }

    # Extract key statistics from 5s features
    features_5s = market_data.get("features_5s", [])
    if features_5s:
        latest = features_5s[0] if isinstance(features_5s, list) else features_5s
        compressed["latest_5s"] = {
            "close": latest.get("close"),
            "high": latest.get("high"),
            "low": latest.get("low"),
            "volume": latest.get("volume"),
            "ret_5s": latest.get("ret_5s"),
            "vwap": latest.get("vwap"),
        }

        # Calculate momentum from recent bars
        if isinstance(features_5s, list) and len(features_5s) >= 5:
            recent_prices = [f.get("close", 0) for f in features_5s[:5]]
            if recent_prices:
                compressed["momentum_5bars"] = {
                    "avg_price": sum(recent_prices) / len(recent_prices),
                    "price_trend": "up" if recent_prices[0] > recent_prices[-1] else "down",
                    "volatility": max(recent_prices) - min(recent_prices),
                }

    # Extract key SFS features
    sfs_features = market_data.get("sfs_features", {})
    if sfs_features:
        compressed["sfs"] = {
            "vwap_1m": sfs_features.get("vwap_1m_tumble"),
            "atr_1m": sfs_features.get("atr_1m_tumble"),
            "regime": sfs_features.get("regime"),
            "orb5_high": sfs_features.get("orb5_high"),
            "orb5_low": sfs_features.get("orb5_low"),
        }

    return compressed


def format_decision_for_memory(
    symbol: str,
    decision: Any,  # LLMResponse
    market_phase: str,
    market_features: Dict[str, Any],
) -> Dict[str, Any]:
    """Format decision for memory storage

    Args:
        symbol: Stock symbol
        decision: LLM decision response
        market_phase: Current market phase
        market_features: Compressed market features

    Returns:
        Formatted dict for memory
    """
    return {
        "symbol": symbol,
        "action": decision.decision,
        "quantity": decision.quantity or 0,
        "confidence": decision.confidence or 0.0,
        "reasoning": decision.reasoning or "",
        "market_phase": market_phase,
        "market_features": market_features,
    }


def calculate_reflection_stats(trade_history: list) -> Dict[str, Any]:
    """Calculate statistics for reflection

    Args:
        trade_history: List of trade records

    Returns:
        Dict with statistics
    """
    if not trade_history:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl": 0.0,
        }

    # Filter sell trades (have realized P&L)
    sell_trades = [t for t in trade_history if t.get("action") == "SELL"]

    total_pnl = sum(t.get("realized_pnl", 0) for t in sell_trades)
    winning = sum(1 for t in sell_trades if t.get("realized_pnl", 0) > 0)
    losing = sum(1 for t in sell_trades if t.get("realized_pnl", 0) <= 0)

    return {
        "total_trades": len(sell_trades),
        "winning_trades": winning,
        "losing_trades": losing,
        "total_pnl": total_pnl,
        "win_rate": winning / max(len(sell_trades), 1),
    }
