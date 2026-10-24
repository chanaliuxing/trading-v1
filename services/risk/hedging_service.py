"""
Quantitative Dynamic Hedging Service - Minimal Viable Product (MVP)

This is a skeleton implementation that:
1. Runs periodic hedging checks (every 60s by default)
2. Calculates portfolio exposure from real positions
3. Computes market factors (VIX percentile, ATR-based trend, carry)
4. Determines target hedge ratio dynamically
5. Logs what WOULD be done (dry-run mode by default)

Next phases will add:
- Real option contract selection via OptionsSelector
- Order execution integration
- Theta budget enforcement
- Advanced smoothing and hysteresis
"""

import asyncio
import time
from typing import Dict, Optional, Any, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from libs.db.ch_pool import threadsafe_query
from libs.logger import get_logger
from libs.config import config
from libs.db.clickhouse import ClickHouseDB

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")


class HedgingService:
    """
    Minimal viable hedging service.

    Core flow:
    1. Every check_interval_sec, evaluate portfolio exposure
    2. Calculate market state (VIX, trend, carry)
    3. Compute target hedge ratio = exposure_ratio * market_multiplier
    4. Log decision (DRY RUN mode by default)
    5. Future: Execute via OptionsSelector + order submission
    """

    def __init__(self, hedge_executor=None, order_router=None):
        """Initialize hedging service with config.

        Args:
            hedge_executor: Optional HedgeExecutor instance (injected for testing/prod)
            order_router: Optional order router (passed to executor for real execution)
        """
        self.running = False
        self.ch = ClickHouseDB()

        # Load config
        cfg = config.get("risk_management.hedge", {}) or {}
        self.enabled = cfg.get("enabled", False)
        self.dry_run = cfg.get("dry_run", True)
        self.check_interval_sec = cfg.get("check_interval_sec", 60)

        # Thresholds
        thresholds = cfg.get("thresholds", {})
        self.max_net_delta = float(thresholds.get("max_net_delta_usd", 50000.0))

        # Dynamic parameters
        self.params = {
            "smooth_alpha": 0.3,
            "hysteresis": 0.05,
            "theta_budget_pct": 0.0005,
            "mult_min": 0.5,
            "mult_max": 1.5,
            "trend_weight": 0.4,
            "vix_weight": 0.4,
            "carry_weight": 0.2,
            "min_gap_usd": 10000.0,
        }
        if "dynamic_params" in cfg:
            self.params.update(cfg["dynamic_params"])

        # State
        self.cooldown_sec = float(cfg.get("cooldown_sec", 300))
        self.last_action_ts = 0.0
        self.exposure_ratio = 0.0
        self.target_hedge_ratio_raw = 0.0
        self.target_hedge_ratio_ema = 0.0
        self.last_committed_ratio = 0.0

        # Hedge executor (injected or created)
        if hedge_executor is None:
            from services.risk.hedge_executor import HedgeExecutor
            self.hedge_executor = HedgeExecutor(order_router=order_router)
        else:
            self.hedge_executor = hedge_executor

        # Keep executor aligned with service switches
        self.hedge_executor.dry_run = self.dry_run
        self.hedge_executor.enabled = self.enabled

        logger.info(
            "HedgingService initialized",
            enabled=self.enabled,
            dry_run=self.dry_run,
            check_interval=self.check_interval_sec,
            max_net_delta=self.max_net_delta,
            params=self.params,
            executor_enabled=self.hedge_executor.enabled,
        )

    # ------------------------------------------------------------------
    # Main Loop
    # ------------------------------------------------------------------
    async def start(self):
        """Start the hedging service loop."""
        if not self.enabled:
            logger.info("HedgingService disabled in config (risk_management.hedge.enabled=false)")
            return

        self.running = True
        logger.info("🛡️  HedgingService STARTED (dry_run=%s)", self.dry_run)

        while self.running:
            try:
                await self._run_cycle()
            except Exception as e:
                logger.error("Hedging cycle error", error=str(e), exc_info=True)

            await asyncio.sleep(self.check_interval_sec)

    async def stop(self):
        """Stop the hedging service."""
        self.running = False
        logger.info("HedgingService stopped")

    async def _run_cycle(self):
        """Single hedging evaluation cycle."""
        # 0. Cooldown check
        now = time.time()
        if now - self.last_action_ts < self.cooldown_sec:
            return

        # 1. Get portfolio exposure
        exposure = await self._get_portfolio_exposure()
        net_delta = exposure.get("net", 0.0)
        gross_delta = exposure.get("gross", 0.0)

        if gross_delta <= 0:
            logger.debug("No exposure, skipping hedge cycle")
            return

        # Calculate exposure ratio (0~1)
        self.exposure_ratio = min(1.0, abs(net_delta) / self.max_net_delta)

        # 2. Market state factors
        trend_score = await self._get_spy_trend_score_atr()
        vix_pct, carry = await self._get_vix_and_carry_factors()

        # 3. Calculate target hedge ratio (raw)
        self.target_hedge_ratio_raw = self._calc_target_ratio(
            exposure_ratio=self.exposure_ratio,
            trend_score=trend_score,
            vix_pct=vix_pct,
            carry=carry,
        )

        # 4. EMA smoothing
        alpha = self.params["smooth_alpha"]
        self.target_hedge_ratio_ema = (
            alpha * self.target_hedge_ratio_raw
            + (1 - alpha) * self.target_hedge_ratio_ema
        )

        # 5. Check if action needed (hysteresis)
        if not self._should_act(self.target_hedge_ratio_ema, self.last_committed_ratio):
            return

        # 6. Calculate delta gap
        target_hedge_delta = -1.0 * net_delta * self.target_hedge_ratio_ema
        current_hedge_delta = await self._get_current_hedge_delta()
        delta_gap = target_hedge_delta - current_hedge_delta

        if abs(delta_gap) < self.params["min_gap_usd"]:
            logger.debug("Delta gap below threshold", gap=delta_gap)
            return

        # 7. Log decision
        logger.info(
            "🛡️  HEDGE DECISION",
            net_delta=f"{net_delta:.0f}",
            gross_delta=f"{gross_delta:.0f}",
            exposure_ratio=f"{self.exposure_ratio:.2f}",
            trend_score=f"{trend_score:.2f}",
            vix_pct=f"{vix_pct:.2f}",
            carry=f"{carry:.2f}",
            target_ratio_raw=f"{self.target_hedge_ratio_raw:.2f}",
            target_ratio_ema=f"{self.target_hedge_ratio_ema:.2f}",
            target_hedge_delta=f"{target_hedge_delta:.0f}",
            current_hedge_delta=f"{current_hedge_delta:.0f}",
            delta_gap=f"{delta_gap:.0f}",
            dry_run=self.dry_run,
        )

        # 8. Execute via HedgeExecutor
        exposure_dict = {"net": net_delta, "gross": gross_delta}

        # Execute hedge (handles both dry_run and real execution)
        result = await self.hedge_executor.execute(
            delta_gap_usd=delta_gap,
            exposure=exposure_dict,
            intent=None,  # Could pass HedgeIntent from AI brain here
        )

        # Log result
        logger.info(
            "hedge_execution_result",
            success=result.success,
            action=result.action,
            reason=result.reason,
            qty=result.qty,
            notional=result.notional_usd,
            error=result.error,
        )

        # Handle degradation: reduce target ratio on failure
        if not result.success and result.action == "reject":
            # Reduce target hedge ratio by 50% on failure (safe mode)
            self.target_hedge_ratio_ema *= 0.5
            logger.warning(
                "hedge_safe_mode",
                new_target_ratio=self.target_hedge_ratio_ema,
                msg="Reduced target hedge ratio due to execution failure",
            )

        # 9. Update state (only if successful action)
        if result.success and result.action != "hold":
            self.last_committed_ratio = self.target_hedge_ratio_ema
            self.last_action_ts = now

    # ------------------------------------------------------------------
    # Portfolio Exposure
    # ------------------------------------------------------------------
    async def _get_portfolio_exposure(self) -> Dict[str, float]:
        """
        Get current portfolio exposure (net and gross delta in USD).

        Returns dict with:
        - net: Net delta exposure (sum of all positions)
        - gross: Gross delta exposure (sum of abs values)
        """
        try:
            # Query recent positions from ClickHouse
            # Exclude sys_hedger positions to avoid feedback loop
            sql = """
            SELECT symbol, SUM(pnl_pos_5s) as position_usd
            FROM trading.signals_realtime
            WHERE ts >= now() - INTERVAL 60 SECOND
              AND strategy_id != 'sys_hedger'
            GROUP BY symbol
            """
            rows = await asyncio.to_thread(threadsafe_query, self.ch, sql)

            if not rows:
                return {"net": 0.0, "gross": 0.0}

            net_delta = 0.0
            gross_delta = 0.0

            for row in rows:
                pos = float(row.get("position_usd", 0.0))
                net_delta += pos
                gross_delta += abs(pos)

            return {"net": net_delta, "gross": gross_delta}

        except Exception as e:
            logger.error("Failed to get portfolio exposure", error=str(e), exc_info=True)
            return {"net": 0.0, "gross": 0.0}

    async def _get_current_hedge_delta(self) -> float:
        """
        Get current hedge position delta.

        Returns negative value for Put positions (protective hedge).
        """
        try:
            # Query sys_hedger positions
            sql = """
            SELECT SUM(pnl_pos_5s) as hedge_delta
            FROM trading.signals_realtime
            WHERE ts >= now() - INTERVAL 60 SECOND
              AND strategy_id = 'sys_hedger'
            """
            rows = await asyncio.to_thread(threadsafe_query, self.ch, sql)

            if not rows or not rows[0]:
                return 0.0

            return float(rows[0].get("hedge_delta", 0.0))

        except Exception as e:
            logger.error("Failed to get hedge delta", error=str(e), exc_info=True)
            return 0.0

    # ------------------------------------------------------------------
    # Market Factors
    # ------------------------------------------------------------------
    async def _get_spy_trend_score_atr(self) -> float:
        """
        Calculate SPY trend score using Wilder ATR.

        Formula: (Close - EMA20) / ATR20
        Returns: Typically in range [-3, 3]
        """
        try:
            sql = """
            SELECT close, high, low
            FROM trading.bars_1d_ibkr
            WHERE symbol = 'SPY'
            ORDER BY ts_end DESC
            LIMIT 80
            """
            rows = await asyncio.to_thread(threadsafe_query, self.ch, sql)

            if not rows or len(rows) < 25:
                logger.warning("Insufficient SPY data for trend calculation")
                return 0.0

            # Reverse to chronological order
            rows = list(reversed(rows))

            closes = [float(r["close"]) for r in rows]
            highs = [float(r["high"]) for r in rows]
            lows = [float(r["low"]) for r in rows]

            # Calculate EMA20
            ema = closes[0]
            k_ema = 2.0 / (20 + 1)
            for c in closes:
                ema = c * k_ema + ema * (1 - k_ema)

            # Calculate ATR20 (Wilder method)
            trs: List[float] = []
            prev_close = closes[0]
            for i in range(1, len(closes)):
                high = highs[i]
                low = lows[i]
                tr = max(
                    high - low,
                    abs(high - prev_close),
                    abs(low - prev_close),
                )
                trs.append(tr)
                prev_close = closes[i]

            if len(trs) < 20:
                return 0.0

            # Initial ATR = average of first 20 TR values
            atr = sum(trs[:20]) / 20.0

            # Wilder smoothing for remaining values
            for tr in trs[20:]:
                atr = (atr * 19 + tr) / 20.0

            if atr <= 0:
                return 0.0

            current_price = closes[-1]
            score = (current_price - ema) / atr

            # Clamp to [-3, 3]
            return max(-3.0, min(3.0, score))

        except Exception as e:
            logger.error("Failed to calculate trend score", error=str(e), exc_info=True)
            return 0.0

    async def _get_vix_and_carry_factors(self) -> tuple[float, float]:
        """
        Calculate VIX percentile and carry factor.

        Returns:
            (vix_pct, carry) where:
            - vix_pct: VIX percentile over 252 days (0~1)
            - carry: Normalized carry signal (vol slope)
        """
        try:
            sql = """
            SELECT close as vix_close
            FROM trading.bars_1d_ibkr
            WHERE symbol = 'VIX'
            ORDER BY ts_end DESC
            LIMIT 252
            """
            rows = await asyncio.to_thread(threadsafe_query, self.ch, sql)

            if not rows or len(rows) < 30:
                logger.warning("Insufficient VIX data")
                return 0.5, 0.0

            vix_values = [float(r["vix_close"]) for r in rows]
            current_vix = vix_values[0]

            # Calculate percentile
            sorted_vix = sorted(vix_values)
            rank = sorted_vix.index(min(sorted_vix, key=lambda x: abs(x - current_vix)))
            vix_pct = rank / len(sorted_vix)

            # Calculate carry (VIX slope)
            if len(vix_values) >= 5:
                recent_avg = sum(vix_values[:5]) / 5.0
                carry = (current_vix - recent_avg) / recent_avg
                carry = max(-1.0, min(1.0, carry))
            else:
                carry = 0.0

            return vix_pct, carry

        except Exception as e:
            logger.error("Failed to calculate VIX factors", error=str(e), exc_info=True)
            return 0.5, 0.0

    # ------------------------------------------------------------------
    # Decision Logic
    # ------------------------------------------------------------------
    def _calc_target_ratio(
        self,
        exposure_ratio: float,
        trend_score: float,
        vix_pct: float,
        carry: float,
    ) -> float:
        """
        Calculate target hedge ratio.

        Formula:
        hedge_ratio = exposure_ratio * market_mult

        market_mult = 1.0 + w_trend * trend_norm + w_vix * vix_norm + w_carry * carry_pos

        Where:
        - trend_norm: trend_score / 2, clamped to [-1, 1]
        - vix_norm: (vix_pct - 0.5) * 2, maps [0,1] to [-1,1]
        - carry_pos: max(0, carry), only positive carry increases hedging
        """
        p = self.params

        # Normalize factors
        trend_norm = max(-1.0, min(1.0, trend_score / 2.0))
        vix_norm = (vix_pct - 0.5) * 2.0
        carry_pos = max(0.0, carry)

        # Calculate market multiplier
        # Strong trend -> less hedge; High VIX -> more hedge; Positive carry -> more hedge
        market_mult = (
            1.0
            + p["trend_weight"] * (-trend_norm)  # Invert: strong trend reduces hedge
            + p["vix_weight"] * vix_norm         # High VIX increases hedge
            + p["carry_weight"] * carry_pos      # Cheap options increase hedge
        )

        # Clamp multiplier
        market_mult = max(p["mult_min"], min(p["mult_max"], market_mult))

        # Final ratio
        ratio = exposure_ratio * market_mult
        return max(0.0, min(1.0, ratio))

    def _should_act(self, new_ratio: float, last_ratio: float) -> bool:
        """Check if ratio change exceeds hysteresis threshold."""
        hysteresis = self.params["hysteresis"]
        return abs(new_ratio - last_ratio) >= hysteresis
