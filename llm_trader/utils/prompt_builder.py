"""Prompt Builder System - Three types of prompts for autonomous trading

1. Decision Prompt: Real-time trading decisions
2. Feedback Prompt: Post-trade outcome evaluation
3. Reflection Prompt: Periodic self-assessment and strategy update
"""

from typing import Dict, Any, List, Optional
from .time_awareness import TimeAwareness
from .memory_system import MemorySystem


class PromptBuilder:
    """Builds context-aware prompts for LLM trading agent"""

    def __init__(self, time_awareness: TimeAwareness, memory_system: MemorySystem):
        """Initialize prompt builder

        Args:
            time_awareness: Time awareness module
            memory_system: Memory system module
        """
        self.time_awareness = time_awareness
        self.memory_system = memory_system

    def build_decision_prompt(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        portfolio_state: Dict[str, Any],
        include_memory: bool = True,
    ) -> str:
        """Build real-time trading decision prompt

        Args:
            symbol: Stock symbol
            market_data: Current market data
            portfolio_state: Current portfolio state
            include_memory: Whether to include memory context

        Returns:
            Complete prompt for decision making
        """
        prompt = self._build_system_header()
        prompt += "\n"

        # Time context
        prompt += self.time_awareness.format_for_prompt()
        prompt += "\n"

        # Memory context (if enabled)
        if include_memory:
            prompt += self.memory_system.compress_for_prompt(max_episodes=15, max_reflections=2)
            prompt += "\n"

        # Current market data
        prompt += self._build_market_data_section(symbol, market_data)
        prompt += "\n"

        # Position context for this symbol (⚠️ EMPTY or ✓ ACTIVE)
        prompt += self._build_position_context(symbol, portfolio_state)
        prompt += "\n"

        # Portfolio state
        prompt += self._build_portfolio_section(portfolio_state)
        prompt += "\n"

        # Decision request
        prompt += self._build_decision_request(symbol, portfolio_state)

        return prompt

    def build_feedback_prompt(
        self,
        symbol: str,
        action_taken: str,
        quantity: int,
        entry_price: float,
        exit_price: Optional[float],
        realized_pnl: Optional[float],
        expected_reasoning: str,
    ) -> str:
        """Build feedback prompt for post-trade evaluation

        Args:
            symbol: Stock symbol
            action_taken: BUY or SELL
            quantity: Shares traded
            entry_price: Entry price
            exit_price: Exit price (None if still holding)
            realized_pnl: Realized P&L (None if still holding)
            expected_reasoning: Original reasoning for the trade

        Returns:
            Feedback prompt
        """
        prompt = "=" * 80 + "\n"
        prompt += "TRADE OUTCOME FEEDBACK\n"
        prompt += "=" * 80 + "\n\n"

        prompt += f"Trade Details:\n"
        prompt += f"- Symbol: {symbol}\n"
        prompt += f"- Action: {action_taken}\n"
        prompt += f"- Quantity: {quantity} shares\n"
        prompt += f"- Entry Price: ${entry_price:.2f}\n"

        if exit_price is not None:
            prompt += f"- Exit Price: ${exit_price:.2f}\n"
            prompt += f"- Price Change: {((exit_price - entry_price) / entry_price * 100):.2f}%\n"

        if realized_pnl is not None:
            prompt += f"- Realized P&L: ${realized_pnl:.2f}\n"
            prompt += f"- Outcome: {'✅ PROFIT' if realized_pnl > 0 else '❌ LOSS'}\n"
        else:
            prompt += f"- Status: Position still open\n"

        prompt += f"\nYour Original Reasoning:\n{expected_reasoning}\n\n"

        prompt += "Please provide a brief self-assessment:\n"
        prompt += "1. Was your prediction accurate?\n"
        prompt += "2. What factors did you miss?\n"
        prompt += "3. What would you do differently next time?\n\n"

        prompt += "Respond in JSON format:\n"
        prompt += """{
  "assessment": "accurate|partially_accurate|inaccurate",
  "missed_factors": ["factor1", "factor2"],
  "lessons_learned": "brief summary",
  "confidence_calibration": "overconfident|well_calibrated|underconfident"
}"""

        return prompt

    def build_reflection_prompt(
        self,
        period: str,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        total_pnl: float,
        recent_trades_summary: List[str],
    ) -> str:
        """Build reflection prompt for periodic self-assessment

        Args:
            period: Reflection period (midday, end_of_day, weekly)
            total_trades: Total trades in period
            winning_trades: Number of winning trades
            losing_trades: Number of losing trades
            total_pnl: Total P&L
            recent_trades_summary: Summary of recent trades

        Returns:
            Reflection prompt
        """
        win_rate = (winning_trades / max(total_trades, 1)) * 100

        prompt = "=" * 80 + "\n"
        prompt += f"{period.upper()} REFLECTION SESSION\n"
        prompt += "=" * 80 + "\n\n"

        prompt += f"Performance Summary:\n"
        prompt += f"- Total Trades: {total_trades}\n"
        prompt += f"- Winning Trades: {winning_trades}\n"
        prompt += f"- Losing Trades: {losing_trades}\n"
        prompt += f"- Win Rate: {win_rate:.1f}%\n"
        prompt += f"- Total P&L: ${total_pnl:.2f}\n"
        prompt += f"- Avg P&L per Trade: ${total_pnl / max(total_trades, 1):.2f}\n\n"

        if recent_trades_summary:
            prompt += "Recent Trades:\n"
            for trade_summary in recent_trades_summary[-10:]:
                prompt += f"  {trade_summary}\n"
            prompt += "\n"

        prompt += "Reflection Questions:\n"
        prompt += "1. What patterns did you notice in your winning trades?\n"
        prompt += "2. What mistakes were repeated in losing trades?\n"
        prompt += "3. Which market conditions suited your strategy best?\n"
        prompt += "4. Which market conditions should you avoid?\n"
        prompt += "5. What adjustments should you make to your strategy?\n\n"

        prompt += "Respond in JSON format:\n"
        prompt += """{
  "key_insights": ["insight1", "insight2", "insight3"],
  "repeated_mistakes": ["mistake1", "mistake2"],
  "best_conditions": "description of favorable conditions",
  "worst_conditions": "description of unfavorable conditions",
  "strategy_adjustments": {
    "confidence_threshold": 0.6,
    "preferred_symbols": ["AAPL", "TSLA"],
    "avoid_symbols": ["XYZ"],
    "preferred_market_phases": ["OPEN", "CLOSE"]
  },
  "planned_improvements": ["improvement1", "improvement2"]
}"""

        return prompt

    def _build_system_header(self) -> str:
        """Build system header with agent identity"""
        return """You are an autonomous quantitative trading agent with learning capabilities.

Your task is to analyze market data and make trading decisions while continuously learning from outcomes.

CORE PRINCIPLES:
- Use time context to adjust strategy (e.g., be cautious near market close)
- Learn from past trades (check your memory for patterns)
- Only trades with confidence ≥ 0.6 will execute. Use 0.7-0.9 for strong conviction
- Respect position limits and risk management rules
- Adapt your strategy based on what works
"""

    def _build_market_data_section(self, symbol: str, market_data: Dict[str, Any]) -> str:
        """Build market data section for prompt"""
        current_price = market_data.get("current_price", 0)
        features_5s = market_data.get("features_5s", [])
        sfs_features = market_data.get("sfs_features", {})
        bars_1s = market_data.get("bars_1s", [])

        section = f"CURRENT MARKET DATA - {symbol}\n"
        section += "=" * 80 + "\n"
        section += f"Current Price: ${current_price:.2f}\n\n"

        # Latest 5s features
        if features_5s:
            latest = features_5s[0] if isinstance(features_5s, list) else features_5s
            section += f"Latest 5s Bar:\n"
            section += f"  OHLC: ${latest.get('open', 0):.2f} / ${latest.get('high', 0):.2f} / "
            section += f"${latest.get('low', 0):.2f} / ${latest.get('close', 0):.2f}\n"
            section += f"  Volume: {latest.get('volume', 0):,.0f} | Trades: {latest.get('trades', 0)}\n"
            section += f"  5s Return: {latest.get('ret_5s', 0)*100:.2f}%\n"
            section += f"  VWAP: ${latest.get('vwap', 0):.2f}\n"

        # 5s Historical Momentum (last 1 minute = 12 x 5s bars)
        if features_5s and len(features_5s) > 1:
            section += f"\n5s Historical Momentum (last 1 min):\n"
            # Calculate momentum stats
            returns = [f.get('ret_5s', 0) for f in features_5s[:12]]
            volumes = [f.get('volume', 0) for f in features_5s[:12]]

            avg_return = sum(returns) / len(returns) if returns else 0
            positive_bars = sum(1 for r in returns if r > 0)
            momentum_score = positive_bars / len(returns) if returns else 0
            avg_volume = sum(volumes) / len(volumes) if volumes else 0

            section += f"  Avg Return: {avg_return*100:.3f}% | Positive Bars: {positive_bars}/12 ({momentum_score*100:.0f}%)\n"
            section += f"  Avg Volume: {avg_volume:,.0f}\n"

        # 1s microstructure (if available)
        if bars_1s and len(bars_1s) > 0:
            section += f"\n1s Microstructure (last 30s):\n"
            # Calculate tick-level stats
            upticks = sum(1 for i in range(1, len(bars_1s))
                         if bars_1s[i].get('close', 0) > bars_1s[i-1].get('close', 0))
            downticks = sum(1 for i in range(1, len(bars_1s))
                           if bars_1s[i].get('close', 0) < bars_1s[i-1].get('close', 0))
            total_ticks = len(bars_1s) - 1

            if total_ticks > 0:
                uptick_ratio = upticks / total_ticks
                section += f"  Upticks/Downticks: {upticks}/{downticks} (ratio: {uptick_ratio:.2f})\n"

                # Volume profile
                volumes_1s = [b.get('volume', 0) for b in bars_1s]
                avg_vol_1s = sum(volumes_1s) / len(volumes_1s) if volumes_1s else 0
                section += f"  Avg 1s Volume: {avg_vol_1s:,.0f}\n"

        # SFS features (all available indicators)
        if sfs_features:
            section += f"\nTechnical Indicators (1m window):\n"
            section += f"  VWAP: ${sfs_features.get('vwap_1m_tumble', 0):.2f}\n"
            section += f"  ATR: ${sfs_features.get('atr_1m_tumble', 0):.4f}\n"

            if sfs_features.get('atr_1m_tumble'):
                atr_pct = (sfs_features.get('atr_1m_tumble', 0) / current_price) * 100 if current_price > 0 else 0
                section += f"  ATR %: {atr_pct:.2f}% of price\n"

            section += f"  Regime: {sfs_features.get('regime', 'UNKNOWN')}\n"

            # ORB5 range
            if sfs_features.get('orb5_high'):
                orb5_range = sfs_features.get('orb5_high', 0) - sfs_features.get('orb5_low', 0)
                orb5_mid = (sfs_features.get('orb5_high', 0) + sfs_features.get('orb5_low', 0)) / 2
                section += f"  ORB5 Range: ${sfs_features.get('orb5_low', 0):.2f} - ${sfs_features.get('orb5_high', 0):.2f} (width: ${orb5_range:.2f})\n"
                section += f"  ORB5 Mid: ${orb5_mid:.2f} | Current vs Mid: ${current_price - orb5_mid:.2f}\n"

            # Momentum indicators
            if sfs_features.get('p_up_15') is not None:
                section += f"  P_UP_15: {sfs_features.get('p_up_15', 0):.3f} (15-bar up probability)\n"

            if sfs_features.get('trend_push_15') is not None:
                section += f"  Trend Push 15: {sfs_features.get('trend_push_15', 0):.4f}\n"

            # EWMA indicators
            if sfs_features.get('q80_ewma') is not None:
                section += f"  Q80 EWMA: ${sfs_features.get('q80_ewma', 0):.2f}\n"

            if sfs_features.get('k_ewma') is not None:
                section += f"  K EWMA: {sfs_features.get('k_ewma', 0):.4f}\n"

            # Price levels from sfs_features
            if sfs_features.get('high') and sfs_features.get('low'):
                range_pct = ((sfs_features.get('high', 0) - sfs_features.get('low', 0)) / sfs_features.get('low', 1)) * 100
                section += f"  Session High/Low: ${sfs_features.get('high', 0):.2f} / ${sfs_features.get('low', 0):.2f} (range: {range_pct:.2f}%)\n"

        section += "\n"
        return section

    def _build_position_context(self, symbol: str, portfolio_state: Dict[str, Any]) -> str:
        """Build position context section for the specific symbol"""
        current_positions = portfolio_state.get("positions", {})
        positions_detail = portfolio_state.get("positions_detail", {})
        position_size = current_positions.get(symbol, 0)
        cash = portfolio_state.get("cash", 0)
        max_position = portfolio_state.get("max_position_size", 0)

        section = "POSITION CONTEXT FOR " + symbol + "\n"
        section += "=" * 80 + "\n"

        if position_size == 0:
            section += f"⚠️ CURRENT STATUS: EMPTY POSITION (0 shares)\n\n"
            section += f"IMPORTANT: You have NO POSITION in {symbol}.\n"
            section += f"If you choose HOLD, you are PASSING on a potential entry opportunity.\n"
            section += f"Consider taking exploratory positions when confidence ≥ 0.55.\n\n"
            section += f"Available Cash: ${cash:,.2f}\n"
            section += f"Max Position Size: ${max_position:,.2f}\n"
        else:
            # Get detailed position info with risk metrics
            pos_detail = positions_detail.get(symbol, {})
            avg_cost = pos_detail.get("avg_cost", 0)
            current_price = pos_detail.get("current_price", 0)
            unrealized_pnl = pos_detail.get("unrealized_pnl", 0)
            unrealized_pnl_pct = pos_detail.get("unrealized_pnl_pct", 0)
            peak_pnl_pct = pos_detail.get("peak_pnl_pct", 0)
            drawdown_from_peak = pos_detail.get("drawdown_from_peak", 0)
            hold_duration = pos_detail.get("hold_duration_minutes", 0)

            section += f"✓ CURRENT STATUS: ACTIVE POSITION ({position_size} shares)\n\n"
            section += f"Entry Price: ${avg_cost:.2f}\n"
            section += f"Current Price: ${current_price:.2f}\n"
            section += f"Unrealized P&L: ${unrealized_pnl:,.2f} ({unrealized_pnl_pct:+.2f}%)\n"
            section += f"Peak P&L: {peak_pnl_pct:+.2f}%\n"
            section += f"Drawdown from Peak: {drawdown_from_peak:.2f}%\n"
            section += f"Hold Duration: {hold_duration} minutes\n\n"

            section += f"⚠️ RISK CHECK:\n"
            section += f"- If P&L ≤ -2.0%: STRONGLY consider SELLING\n"
            section += f"- If P&L ≤ -3.0%: EXIT immediately\n"
            section += f"- If P&L ≥ +5.0%: Consider profit-taking\n"
            section += f"- If Drawdown from Peak ≥ 3.0%: Trailing stop exit\n"
            section += f"- If Hold Duration >90min with flat P&L: Review exit\n\n"

            section += f"You currently HOLD {position_size} shares in {symbol}.\n"
            section += f"HOLD = Continue holding | SELL = Exit or reduce position\n\n"
            section += f"Available Cash: ${cash:,.2f}\n"
            section += f"Max Position Size: ${max_position:,.2f}\n"

        section += "\n"
        return section

    def _build_portfolio_section(self, portfolio_state: Dict[str, Any]) -> str:
        """Build portfolio section for prompt"""
        section = "PORTFOLIO STATE\n"
        section += "=" * 80 + "\n"
        section += f"Cash: ${portfolio_state.get('cash', 0):,.2f}\n"
        section += f"Total Equity: ${portfolio_state.get('total_equity', 0):,.2f}\n"
        section += f"Total P&L: ${portfolio_state.get('total_pnl', 0):,.2f} "
        section += f"({portfolio_state.get('total_pnl_pct', 0):.2f}%)\n"
        section += f"Positions: {portfolio_state.get('num_positions', 0)}/{portfolio_state.get('max_positions', 10)}\n"
        section += f"Max Position Size: ${portfolio_state.get('max_position_size', 0):,.2f}\n"
        section += f"Available Buying Power: ${portfolio_state.get('available_buying_power', 0):,.2f}\n\n"

        # Add global risk metrics
        consecutive_wins = portfolio_state.get("consecutive_wins", 0)
        consecutive_losses = portfolio_state.get("consecutive_losses", 0)
        daily_pnl = portfolio_state.get("daily_pnl", 0)
        daily_pnl_pct = portfolio_state.get("daily_pnl_pct", 0)

        section += f"RISK METRICS:\n"
        section += f"Today's Realized P&L: ${daily_pnl:,.2f} ({daily_pnl_pct:+.2f}%)\n"
        section += f"Consecutive Wins: {consecutive_wins}\n"
        section += f"Consecutive Losses: {consecutive_losses}\n"

        # Risk warnings
        if daily_pnl_pct <= -5.0:
            section += f"🚨 ALERT: Daily loss limit reached (-5.0%) - STOP TRADING!\n"
        if consecutive_losses >= 3:
            section += f"⚠️ WARNING: {consecutive_losses} consecutive losses - Reduce next position by 50%\n"

        section += "\n"

        # Show current positions
        positions = portfolio_state.get('positions', {})
        if positions:
            section += f"\nCurrent Positions:\n"
            for symbol, qty in positions.items():
                section += f"  {symbol}: {qty} shares\n"

        section += "\n"
        return section

    def _build_decision_request(self, symbol: str, portfolio_state: Dict[str, Any]) -> str:
        """Build decision request section"""
        current_positions = portfolio_state.get("positions", {})
        position_size = current_positions.get(symbol, 0)
        is_empty = position_size == 0

        request = "DECISION REQUEST\n"
        request += "=" * 80 + "\n\n"
        request += "Based on the above context (time, memory, market data, position status, portfolio), provide your trading decision.\n\n"

        if is_empty:
            request += "⚠️ EMPTY POSITION REMINDER:\n"
            request += f"You currently have NO POSITION in {symbol}.\n"
            request += "- If you choose HOLD, you are PASSING on an entry opportunity.\n"
            request += "- Only choose HOLD if confidence < 0.50 or signals are unclear.\n"
            request += "- When confidence ≥ 0.55, consider exploratory positions.\n\n"

        # Add risk management rules
        request += "⚠️ RISK MANAGEMENT RULES (CRITICAL):\n"
        request += "When you hold an active position, monitor these thresholds:\n\n"
        request += "Stop Loss (Protect Capital):\n"
        request += "- If unrealized P&L falls to -2.0% or worse → STRONGLY consider SELLING\n"
        request += "- If unrealized P&L falls to -3.0% or worse → EXIT immediately\n"
        request += "- ATR-adjusted: High volatility (ATR >3%) → widen stop to -3.5%\n"
        request += "- Near market close → tighten stop to -1.5%\n\n"
        request += "Take Profit (Lock in Gains):\n"
        request += "- If unrealized P&L reaches +5.0% → Consider profit-taking\n"
        request += "- If unrealized P&L reaches +8.0% → Strongly consider exiting\n"
        request += "- Partial exit: At +5%, sell 50% and trail remaining with 3% stop\n\n"
        request += "Trailing Stop:\n"
        request += "- If position reaches +3% or higher, track peak P&L\n"
        request += "- If price falls 3% from peak → Consider exit\n\n"
        request += "Position Duration:\n"
        request += "- Holding >90min with flat P&L (-0.5% to +0.5%) → Review exit\n"
        request += "- Holding >120min → Strongly review rationale\n"
        request += "- Near market close: Exit by 15:55 ET\n\n"
        request += "Daily Loss Protection:\n"
        request += "- Total daily P&L reaches -5% → STOP trading\n"
        request += "- After 3 consecutive losses → Reduce next position by 50%\n\n"
        request += "NOTE: These are guidelines. You can override with HIGH CONFIDENCE (≥0.75)\n"
        request += "but MUST justify your reasoning explicitly.\n\n"

        request += """Respond in JSON format:
{
  "decision": "BUY" | "SELL" | "HOLD",
  "quantity": <integer, number of shares>,
  "confidence": <float 0-1, your confidence level>,
  "reasoning": "<brief explanation max 200 chars>"
}

IMPORTANT:
- Provide honest confidence (0.0-1.0). Only trades with confidence ≥ 0.6 will execute
- When you have strong conviction based on clear signals, use confidence 0.7-0.9
- Consider time context (e.g., don't open new positions near close)
- Learn from your memory (what worked, what didn't)
- Respect position limits
"""
        if is_empty:
            request += "- Be proactive when empty: don't stay in HOLD forever\n"
        else:
            request += "- When holding: HOLD is valid, SELL to exit/reduce\n"
        request += "- Provide clear, concise reasoning\n"

        return request
