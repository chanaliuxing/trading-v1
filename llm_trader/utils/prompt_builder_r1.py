"""DeepSeek-R1 Optimized Prompt Builder

Specialized prompt templates for DeepSeek-R1 reasoning model.
Designed to leverage R1's thinking capabilities while encouraging action.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime


class R1PromptBuilder:
    """Prompt builder optimized for DeepSeek-R1 reasoning style"""

    def __init__(self, time_awareness, memory_system):
        """Initialize R1 prompt builder

        Args:
            time_awareness: TimeAwareness instance
            memory_system: MemorySystem instance
        """
        self.time_awareness = time_awareness
        self.memory_system = memory_system

    def _build_system_role(self) -> str:
        """Build system role definition for R1"""
        return """You are DeepTrader-R1, an autonomous short-term trading agent.

Your objective is to generate stable cash flow through intraday price movements, not long-term holdings.
You MUST base your reasoning on provided data, historical behavior, and time context.
DO NOT rely on external knowledge or news.

Key principles:
- You can think carefully, but don't stay in HOLD forever
- At medium confidence (≥0.55), consider light exploratory positions
- Balance speed of action with confidence threshold
- Time is a critical factor: opportunity cost increases as market close approaches
- Learn from your recent memory to avoid repeating mistakes"""

    def _build_time_context(self, dt: Optional[datetime] = None) -> str:
        """Build time context section"""
        time_ctx = self.time_awareness.get_time_context(dt)
        phase = time_ctx["market_phase"]

        return f"""TIME CONTEXT:
- Current Time (UTC): {time_ctx['utc_time']}
- Current Time (ET): {time_ctx['local_time']}
- Market Phase: {phase}
- Minutes Since Open: {time_ctx.get('minutes_since_open', 'N/A')}
- Minutes To Close: {time_ctx.get('minutes_to_close', 'N/A')}

Market Phase Description: {time_ctx['phase_description']}"""

    def _compress_market_snapshot(self, market_data: Dict[str, Any]) -> str:
        """Compress market data with all available features"""
        snapshot = ""
        current_price = market_data.get("current_price", 0)
        features_5s = market_data.get("features_5s", [])
        sfs_features = market_data.get("sfs_features", {})
        bars_1s = market_data.get("bars_1s", [])

        snapshot += f"Current Price: ${current_price:.2f}\n"

        # Latest 5s bar
        if features_5s:
            latest = features_5s[0] if isinstance(features_5s, list) else features_5s
            ret_5s = latest.get("ret_5s", 0) * 100
            snapshot += f"\nLatest 5s Bar:\n"
            snapshot += f"- Close: ${latest.get('close', 0):.2f} | High/Low: ${latest.get('high', 0):.2f} / ${latest.get('low', 0):.2f}\n"
            snapshot += f"- Volume: {latest.get('volume', 0):,.0f} | Trades: {latest.get('trades', 0)}\n"
            snapshot += f"- 5s Return: {ret_5s:+.3f}% | VWAP: ${latest.get('vwap', 0):.2f}\n"

        # 5s momentum (last 1 minute = 12 bars)
        if features_5s and len(features_5s) > 1:
            returns = [f.get('ret_5s', 0) for f in features_5s[:12]]
            volumes = [f.get('volume', 0) for f in features_5s[:12]]
            avg_ret = sum(returns) / len(returns) if returns else 0
            pos_bars = sum(1 for r in returns if r > 0)
            momentum = pos_bars / len(returns) if returns else 0

            snapshot += f"\n5s Momentum (1min window):\n"
            snapshot += f"- Avg Return: {avg_ret*100:+.3f}% | Positive: {pos_bars}/12 ({momentum*100:.0f}%)\n"
            snapshot += f"- Avg Volume: {sum(volumes)/len(volumes):,.0f}\n"

        # 1s microstructure
        if bars_1s and len(bars_1s) > 1:
            upticks = sum(1 for i in range(1, len(bars_1s))
                         if bars_1s[i].get('close', 0) > bars_1s[i-1].get('close', 0))
            total = len(bars_1s) - 1
            uptick_ratio = upticks / total if total > 0 else 0

            snapshot += f"\n1s Microstructure (30s):\n"
            snapshot += f"- Uptick Ratio: {uptick_ratio:.2f} ({upticks}/{total})\n"

        # SFS features (ALL available indicators)
        if sfs_features:
            snapshot += f"\nTechnical Indicators (1m):\n"
            snapshot += f"- VWAP: ${sfs_features.get('vwap_1m_tumble', 0):.2f}\n"

            atr = sfs_features.get('atr_1m_tumble', 0)
            atr_pct = (atr / current_price * 100) if current_price > 0 else 0
            snapshot += f"- ATR: ${atr:.4f} ({atr_pct:.2f}% of price)\n"

            snapshot += f"- Regime: {sfs_features.get('regime', 'UNKNOWN')}\n"

            # ORB5 with position relative to range
            orb5_high = sfs_features.get('orb5_high')
            orb5_low = sfs_features.get('orb5_low')
            if orb5_high and orb5_low:
                orb5_mid = (orb5_high + orb5_low) / 2
                orb5_width = orb5_high - orb5_low
                price_vs_mid = current_price - orb5_mid
                snapshot += f"- ORB5: ${orb5_low:.2f}-${orb5_high:.2f} (width: ${orb5_width:.2f})\n"
                snapshot += f"  Price vs ORB5 Mid: ${price_vs_mid:+.2f}\n"

            # Momentum indicators
            if sfs_features.get('p_up_15') is not None:
                snapshot += f"- P_UP_15: {sfs_features.get('p_up_15', 0):.3f} (15-bar up prob)\n"

            if sfs_features.get('trend_push_15') is not None:
                snapshot += f"- Trend Push 15: {sfs_features.get('trend_push_15', 0):+.4f}\n"

            # EWMA indicators
            if sfs_features.get('q80_ewma') is not None:
                snapshot += f"- Q80 EWMA: ${sfs_features.get('q80_ewma', 0):.2f}\n"

            if sfs_features.get('k_ewma') is not None:
                snapshot += f"- K EWMA: {sfs_features.get('k_ewma', 0):.4f}\n"

            # Session levels
            if sfs_features.get('high') and sfs_features.get('low'):
                sess_high = sfs_features.get('high', 0)
                sess_low = sfs_features.get('low', 0)
                sess_range = ((sess_high - sess_low) / sess_low * 100) if sess_low > 0 else 0
                snapshot += f"- Session Range: ${sess_low:.2f}-${sess_high:.2f} ({sess_range:.2f}%)\n"

        return snapshot

    def _build_memory_summary(self, symbol: str, max_episodes: int = 15) -> str:
        """Build compressed recent memory summary"""
        # Get all recent episodes, then filter by symbol
        all_episodes = self.memory_system.get_recent_episodes(n=max_episodes * 3)
        episodes = [e for e in all_episodes if e.symbol == symbol][:max_episodes]

        if not episodes:
            return "No recent trading history for this symbol."

        summary = f"Recent Trading History (last {len(episodes)} decisions):\n\n"

        # Statistics
        verified = [e for e in episodes if e.outcome_verified]
        if verified:
            total_pnl = sum(e.realized_pnl for e in verified)
            wins = sum(1 for e in verified if e.realized_pnl > 0)
            losses = sum(1 for e in verified if e.realized_pnl <= 0)
            win_rate = wins / len(verified) if verified else 0

            summary += f"Performance Summary:\n"
            summary += f"- Total P&L: ${total_pnl:.2f}\n"
            summary += f"- Win Rate: {win_rate:.1%} ({wins}W / {losses}L)\n"
            summary += f"- Verified Trades: {len(verified)}\n\n"

        # Recent decisions (compressed)
        summary += "Recent Decisions:\n"
        for i, ep in enumerate(episodes[:10], 1):  # Last 10 only
            status = "✓" if ep.outcome_verified else "⏳"
            pnl_str = f"${ep.realized_pnl:+.2f}" if ep.outcome_verified else "pending"
            summary += f"{i}. {status} {ep.action} x{ep.quantity} @ ${ep.price:.2f} "
            summary += f"(conf:{ep.confidence:.2f}, phase:{ep.market_phase}) → {pnl_str}\n"

        # Recent reflections
        reflections = self.memory_system.get_recent_reflections(n=1)
        if reflections:
            latest_ref = reflections[0]
            summary += f"\nLatest Self-Reflection ({latest_ref.period}):\n"
            summary += f"{latest_ref.insights[:200]}...\n"

        return summary

    def _build_goal_instruction(self, portfolio_state: Dict[str, Any], symbol: str) -> str:
        """Build goal and instruction section"""
        cash = portfolio_state.get("cash", 0)
        max_position = portfolio_state.get("max_position_size", 0)
        current_positions = portfolio_state.get("positions", {})
        positions_detail = portfolio_state.get("positions_detail", {})

        # Get position for this specific symbol
        position_size = current_positions.get(symbol, 0)
        is_empty = position_size == 0

        position_context = f"""POSITION CONTEXT:
- Current Position in {symbol}: {position_size} shares"""

        if is_empty:
            position_context += f"""
- Status: ⚠️ YOU ARE CURRENTLY EMPTY (NO POSITION)
- Portfolio Cash: ${cash:,.2f}
- Max Position Size: ${max_position:,.2f}

IMPORTANT: You are currently holding NO POSITION in {symbol}.
If you choose HOLD now, you are PASSING on a potential entry opportunity.
Repeated empty-position HOLD decisions indicate overcautious behavior.
Consider taking small exploratory positions when confidence ≥ 0.55."""
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

            position_context += f"""
- Status: ✓ ACTIVE POSITION
- Entry Price: ${avg_cost:.2f}
- Current Price: ${current_price:.2f}
- Unrealized P&L: ${unrealized_pnl:,.2f} ({unrealized_pnl_pct:+.2f}%)
- Peak P&L: {peak_pnl_pct:+.2f}%
- Drawdown from Peak: {drawdown_from_peak:.2f}%
- Hold Duration: {hold_duration} minutes
- Available Cash: ${cash:,.2f}
- Max Position Size: ${max_position:,.2f}

⚠️ RISK CHECK:
- If P&L ≤ -2.0%: STRONGLY consider SELLING
- If P&L ≤ -3.0%: EXIT immediately
- If P&L ≥ +5.0%: Consider profit-taking
- If Drawdown from Peak ≥ 3.0%: Consider trailing stop exit
- If Hold Duration >90min with flat P&L: Review exit
- If Hold Duration >120min: Strongly review

You currently HOLD {position_size} shares.
HOLD means continue holding; SELL means exit or reduce position."""

        # Get global risk metrics
        consecutive_wins = portfolio_state.get("consecutive_wins", 0)
        consecutive_losses = portfolio_state.get("consecutive_losses", 0)
        daily_pnl = portfolio_state.get("daily_pnl", 0)
        daily_pnl_pct = portfolio_state.get("daily_pnl_pct", 0)

        return f"""{position_context}

TOTAL PORTFOLIO:
- Total Positions: {len(current_positions)}
- Available Cash: ${cash:,.2f}
- Today's Realized P&L: ${daily_pnl:,.2f} ({daily_pnl_pct:+.2f}%)
- Consecutive Wins: {consecutive_wins}
- Consecutive Losses: {consecutive_losses}

⚠️ PORTFOLIO RISK STATUS:
- Daily Loss Limit: -5.0% (${portfolio_state.get('total_equity', 50000) * 0.05:,.2f})
- Current Daily P&L: {daily_pnl_pct:+.2f}%
- {'🚨 STOP TRADING - Daily loss limit reached!' if daily_pnl_pct <= -5.0 else '✓ Within daily loss limits'}
- {'⚠️ 3 consecutive losses - Reduce next position by 50%' if consecutive_losses >= 3 else ''}

GOAL INSTRUCTION:
Based on market snapshot, time context, your position status, and recent performance, decide your next action.

Action Definitions:
- BUY: Enter or add to position when you believe price will rise
- SELL: Exit or reduce position when you believe price will fall or protect profits
- HOLD:
  * If you HAVE a position: Continue holding (wait for better exit)
  * If you are EMPTY: ⚠️ You are PASSING on an entry opportunity

⚠️ EMPTY POSITION WARNING:
If you have NO POSITION and choose HOLD repeatedly, you are being overcautious.
In empty state, only choose HOLD if confidence < 0.50 or signals are truly unclear.
When confidence ≥ 0.55, consider taking exploratory positions.

Position Sizing Guidelines:
- Confidence 0.55-0.65: Light exploratory (25-50% of max position)
- Confidence 0.65-0.75: Standard position (50-75% of max)
- Confidence 0.75+: Full position (up to max)

Time-Based Adjustments:
- PREMARKET/OPEN: Higher volatility → reduce position size by 30%
- MIDDAY: Normal market → standard sizing
- CLOSE: Time pressure → avoid new entries if confidence < 0.65
- Factor in opportunity cost as market close approaches

⚠️ RISK MANAGEMENT RULES (CRITICAL):
When you HOLD an ACTIVE POSITION, monitor these thresholds carefully:

Stop Loss (Protect Capital):
- If unrealized P&L falls to -2.0% or worse → STRONGLY consider SELLING
- If unrealized P&L falls to -3.0% or worse → EXIT immediately (hard stop)
- ATR-adjusted: High volatility (ATR >3%) → widen stop to -3.5%
- Near market close (CLOSE phase) → tighten stop to -1.5%

Take Profit (Lock in Gains):
- If unrealized P&L reaches +5.0% → Consider partial or full profit-taking
- If unrealized P&L reaches +8.0% → Strongly consider taking profits
- Partial exit strategy: At +5%, sell 50% and trail remaining with 3% stop

Trailing Stop (Protect Profits):
- If position reaches +3% or higher, track the peak unrealized P&L
- If price falls 3% from peak → Consider exiting to protect gains
- Example: Reached +7% peak, now at +4% → falling from peak, consider exit

Position Duration Limits:
- If holding >90 minutes with P&L between -0.5% to +0.5% → Review and consider exit
- If holding >120 minutes (2 hours) → Strongly review rationale, consider exit
- Near market close: Exit all positions by 15:55 ET to avoid overnight risk

Daily Loss Protection:
- If your total daily P&L (across all positions) reaches -5% → STOP trading
- After 3 consecutive losing trades → Reduce position size by 50% for next trade
- If single position loses -2%, be more cautious with next entry

⚠️ THESE ARE GUIDELINES, NOT HARD RULES:
You can override these thresholds if you have HIGH CONFIDENCE (≥0.75) reasoning.
But you MUST explicitly justify why you're overriding risk management in your reasoning.

Learn from Memory:
- If consecutive empty HOLD decisions → you're being too conservative
- If recent losses → tighten risk management temporarily
- If missed rallies → consider lowering entry threshold
- Update your policy_delta based on lessons learned"""

    def build_decision_prompt(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        portfolio_state: Dict[str, Any],
        dt: Optional[datetime] = None,
    ) -> str:
        """Build complete decision prompt for R1

        Args:
            symbol: Stock symbol
            market_data: Market data dict
            portfolio_state: Portfolio state dict
            dt: Optional datetime for time context

        Returns:
            Complete prompt string
        """
        prompt = f"{self._build_system_role()}\n\n"
        prompt += f"SYMBOL: {symbol}\n\n"
        prompt += f"{self._build_time_context(dt)}\n\n"
        prompt += f"MARKET SNAPSHOT SUMMARY:\n{self._compress_market_snapshot(market_data)}\n\n"
        prompt += f"RECENT MEMORY SUMMARY:\n{self._build_memory_summary(symbol)}\n\n"
        prompt += f"{self._build_goal_instruction(portfolio_state, symbol)}\n\n"
        prompt += """OUTPUT FORMAT:
Provide your decision as a JSON object:
{
  "decision": "BUY" | "SELL" | "HOLD",
  "confidence": <float 0-1>,
  "quantity": <int, number of shares>,
  "reasoning": "<your main rationale>",
  "policy_delta": "<optional: any strategy adjustments you're making>"
}

Important:
- Be decisive when confidence ≥ 0.55
- Explain your thinking in reasoning
- Consider time pressure and recent performance
- Don't stay in HOLD forever when opportunities exist"""

        return prompt

    def build_feedback_prompt(
        self,
        symbol: str,
        action: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        realized_pnl: float,
        hold_duration_minutes: int,
        expected_reasoning: str,
    ) -> str:
        """Build feedback prompt after trade completion

        Args:
            symbol: Stock symbol
            action: Original action (BUY/SELL)
            entry_price: Entry price
            exit_price: Exit price
            quantity: Shares traded
            realized_pnl: Realized profit/loss
            hold_duration_minutes: How long position was held
            expected_reasoning: Original reasoning for the trade

        Returns:
            Feedback prompt string
        """
        pnl_pct = (realized_pnl / (entry_price * quantity)) * 100 if quantity > 0 else 0
        outcome = "PROFIT" if realized_pnl > 0 else "LOSS"

        return f"""TRADE OUTCOME FEEDBACK

Symbol: {symbol}
Action: {action}
Entry Price: ${entry_price:.2f}
Exit Price: ${exit_price:.2f}
Quantity: {quantity}
Hold Duration: {hold_duration_minutes} minutes
Realized P&L: ${realized_pnl:+.2f} ({pnl_pct:+.2f}%)
Outcome: {outcome}

Your Original Reasoning:
{expected_reasoning}

REFLECTION QUESTIONS:
1. Did the market behave as you expected?
2. Was your confidence level appropriate for the outcome?
3. Was your position size reasonable given the risk?
4. Would you make the same decision again in similar conditions?
5. What would you do differently next time?

Please provide a brief analysis (2-3 sentences) of this trade outcome.
Focus on what you learned and how it might influence future decisions.

OUTPUT FORMAT:
{{
  "outcome_analysis": "<your analysis>",
  "confidence_appropriate": <true/false>,
  "lessons_learned": "<key takeaway>",
  "future_adjustment": "<what you'll do differently>"
}}"""

    def build_reflection_prompt(
        self,
        period: str,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        total_pnl: float,
        recent_trades_summary: str,
    ) -> str:
        """Build reflection prompt for periodic self-assessment

        Args:
            period: Reflection period (MIDDAY, EOD)
            total_trades: Number of trades
            winning_trades: Number of winning trades
            losing_trades: Number of losing trades
            total_pnl: Total profit/loss
            recent_trades_summary: Summary of recent trades

        Returns:
            Reflection prompt string
        """
        win_rate = winning_trades / max(total_trades, 1)
        avg_pnl = total_pnl / max(total_trades, 1)

        return f"""TRADING SESSION REFLECTION - {period}

PERFORMANCE SUMMARY:
- Period: {period}
- Total Trades: {total_trades}
- Winning Trades: {winning_trades}
- Losing Trades: {losing_trades}
- Win Rate: {win_rate:.1%}
- Total P&L: ${total_pnl:+.2f}
- Average P&L per Trade: ${avg_pnl:+.2f}

RECENT TRADES:
{recent_trades_summary}

REFLECTION PROMPTS:
1. What patterns led to successful trades?
2. What mistakes were repeated?
3. Were you too aggressive or too conservative?
4. Which market phases (OPEN/MIDDAY/CLOSE) did you perform best/worst in?
5. How should you adjust your strategy for the next session?
6. What confidence threshold worked best today?

Please provide a comprehensive self-assessment.

OUTPUT FORMAT:
{{
  "key_lessons": "<main insights from this session>",
  "risk_adjustment": "<increase/decrease/maintain risk, and why>",
  "execution_feedback": "<what went well, what didn't>",
  "new_policy": "<specific strategy changes for next session>",
  "confidence_threshold_adjustment": "<should you be more/less aggressive?>",
  "market_phase_preference": "<which phases to focus on or avoid>"
}}"""
