"""
Pre-Decision Validator
Validates decision envelopes before execution
Based on doc/50.md specification

Validates:
- Freshness (decision latency budget)
- Data quality (sufficient history, no gaps)
- Market state (NBBO, halts, SSR, LULD)
- Cost coverage (edge > cost + buffer)
"""
import time
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from prometheus_client import Counter

from libs.strategy.base import DecisionEnvelope
from libs.logger import get_logger

logger = get_logger(__name__)

validator_history_bypass_total = Counter(
    "validator_history_bypass_total",
    "History gate bypass hint usage in validator",
    ["strategy_id", "symbol", "outcome"],
)


@dataclass
class ValidateConfig:
    """Validation configuration"""
    min_hist_bars: int = 36  # Minimum history window (5s bars)
    bar_gap_ms_max: int = 1200  # Max aggregator tail latency
    require_nbbo: bool = True  # Require NBBO for market orders
    min_volume: float = 1.0  # Minimum bar volume
    min_atr_bp: float = 1.0  # Minimum volatility (filter noise)
    edge_buffer_bp: float = 2.0  # Edge must exceed cost + buffer
    allow_strategy_history_ready_bypass: bool = True
    enforce_quote_age: bool = False
    max_quote_age_sec: float = 10.0
    min_dlb_ms: int = 2000
    history_ready_allowed_strategy_ids: tuple[str, ...] = (
        "qqq_intraday_plugin_001",
        "spy_1dte_ma10_reject_0324_001",
    )


@dataclass
class ValidateResult:
    """Validation result"""
    ok: bool
    skip_reasons: List[str]
    now_us: int
    effective_ttl_ms: int


class PreDecisionValidator:
    """
    Pre-decision validator

    Validates decision envelopes on four dimensions:
    1. Freshness (DLB - decision latency budget)
    2. Data quality (history window, gaps, quality flags)
    3. Market state (NBBO, halts, circuit breakers)
    4. Cost coverage (edge must exceed costs)
    """

    def __init__(
        self,
        cfg: ValidateConfig,
        market_state_provider: Any,
        bar_store: Any,
        feature_store: Any,
        cost_model: Any
    ):
        """
        Initialize validator

        Args:
            cfg: Validation configuration
            market_state_provider: Market state (bid/ask/halt/SSR/LULD)
            bar_store: Historical bar access
            feature_store: Feature access
            cost_model: Cost estimation model
        """
        self.cfg = cfg
        self.ms = market_state_provider
        self.bars = bar_store
        self.feats = feature_store
        self.cost = cost_model

        logger.info("PreDecisionValidator initialized", config=cfg)

    def _is_safety_intent(self, env: DecisionEnvelope) -> bool:
        """
        Safety intents must be fail-open.

        These are position-risk reducing actions (flatten / close-only / protective-stop)
        and must not be blocked by entry-quality gates like:
        - insufficient_history
        - no_nbbo
        - edge/cost coverage
        - DLB freshness
        """
        extras = env.extras or {}
        return bool(
            extras.get("flatten_action")
            or extras.get("protective_stop")
            or extras.get("close_only")
        )

    @staticmethod
    def _strategy_history_ready(env: DecisionEnvelope) -> bool:
        """
        Strategy-level history readiness hint.

        Some strategies (e.g. QQQ family) run on Redis/SFS warmup windows and can
        be ready even when ClickHouse bars_5s is temporarily sparse right after
        restart. In that case we only bypass the `insufficient_history` gate.
        """
        extras = env.extras or {}
        return bool(extras.get("history_ready"))

    def _allow_history_ready_bypass(self, env: DecisionEnvelope) -> bool:
        if not bool(self.cfg.allow_strategy_history_ready_bypass):
            return False
        allowed_ids = tuple(self.cfg.history_ready_allowed_strategy_ids or ())
        if not allowed_ids:
            return True
        sid = str(env.strategy_id or "").strip()
        return sid in allowed_ids

    async def validate(self, env: DecisionEnvelope) -> ValidateResult:
        """
        Validate decision envelope

        Args:
            env: Decision envelope

        Returns:
            Validation result
        """
        reasons: List[str] = []
        now_us = int(time.time() * 1e6)
        is_safety = self._is_safety_intent(env)
        extras = env.extras or {}
        is_option = bool(
            str(extras.get("instrument_type") or "").strip().upper() == "OPTION"
            or str(env.symbol or "").strip().upper().startswith("OPT:")
        )
        bars_symbol = str(
            (extras.get("bars_symbol") or extras.get("underlying") or env.symbol) or ""
        ).strip().upper()
        history_ready_hint = self._strategy_history_ready(env)
        history_ready = history_ready_hint and self._allow_history_ready_bypass(env)
        if (not is_safety) and history_ready_hint:
            outcome = "allowed" if history_ready else "blocked_by_policy"
            validator_history_bypass_total.labels(
                strategy_id=str(env.strategy_id or "unknown"),
                symbol=str(env.symbol or "unknown"),
                outcome=outcome,
            ).inc()
            if not history_ready:
                logger.warning(
                    "validator_history_ready_hint_blocked",
                    decision_id=env.decision_id,
                    strategy_id=env.strategy_id,
                    symbol=env.symbol,
                )

        # 0) Freshness (DLB check)
        effective_dlb_ms = max(int(self.cfg.min_dlb_ms), int(env.freshness.dlb_ms))
        dlb_exceeded = now_us - env.ts_bar_end_us > effective_dlb_ms * 1000
        if dlb_exceeded and not is_safety:
            reasons.append("stale_decision:dlb_exceeded")
            logger.debug("DLB exceeded",
                        decision_id=env.decision_id,
                        latency_ms=(now_us - env.ts_bar_end_us) / 1000,
                        dlb_ms=effective_dlb_ms)

        # 1) History window sufficiency
        bars = None
        if not is_safety:
            bars = await self.bars.window(
                bars_symbol if is_option and bars_symbol else env.symbol,
                end_us=env.ts_bar_end_us,
                count=self.cfg.min_hist_bars
            )

        if (not is_safety) and (not history_ready) and (not bars or len(bars) < self.cfg.min_hist_bars):
            reasons.append("insufficient_history")
            logger.debug("Insufficient history",
                        decision_id=env.decision_id,
                        bars_available=len(bars) if bars else 0,
                        required=self.cfg.min_hist_bars)
        elif (not is_safety) and history_ready and (not bars or len(bars) < self.cfg.min_hist_bars):
            logger.info(
                "validator_history_gate_bypassed_by_strategy_ready",
                decision_id=env.decision_id,
                strategy_id=env.strategy_id,
                symbol=env.symbol,
                bars_available=len(bars) if bars else 0,
                required=self.cfg.min_hist_bars,
            )
        # NOTE: bar_lag check removed - BarStore uses 5s bars but strategies use 5m bars,
        # so lag comparison is not meaningful. DLB check above is sufficient for freshness.

        # 2) Data quality (ATR, volume, quality flags)
        if bars and not is_safety:
            atr_bp = max(1e-9, bars[-1].get("atr_bp", 0.0))
            vol = bars[-1].get("volume", 0.0)
            qflags = bars[-1].get("quality", "GOOD")

            if atr_bp < self.cfg.min_atr_bp:
                reasons.append("too_low_volatility")

            if vol < self.cfg.min_volume:
                reasons.append("too_low_volume")

            # NOTE: Accept UNKNOWN quality - it just means features aren't calculated yet
            # This is normal for recent bars due to feature calculator lag
            if qflags not in ("GOOD", "REPAIRED", "UNKNOWN"):
                reasons.append(f"bar_quality:{qflags}")

        # 3) Market state (NBBO, halt, SSR, LULD)
        # For options, we validate underlying halts using bars_symbol and validate option NBBO using extras.
        m = await self.ms.get(bars_symbol if is_option and bars_symbol else env.symbol) or {}

        if m.get('halted', False):
            reasons.append("halted")

        # Check NBBO for mid reference
        if (not is_safety) and env.price_intent and env.price_intent.ref == "mid":
            if m.get('bid_px') is None or m.get('ask_px') is None:
                reasons.append("no_nbbo_for_mid")

        if (not is_safety) and self.cfg.require_nbbo:
            if is_option:
                opt_bid = extras.get("opt_bid_px")
                opt_ask = extras.get("opt_ask_px")
                try:
                    if opt_bid is None or opt_ask is None or float(opt_bid) <= 0 or float(opt_ask) <= 0:
                        reasons.append("no_nbbo")
                except Exception:
                    reasons.append("no_nbbo")
            else:
                if m.get('bid_px') is None and m.get('ask_px') is None:
                    reasons.append("no_nbbo")

        if (not is_safety) and bool(self.cfg.enforce_quote_age):
            try:
                if is_option:
                    enforce_opt_quote_age = bool(extras.get("enforce_opt_quote_age", False))
                    if enforce_opt_quote_age:
                        qage = extras.get("opt_quote_age_sec")
                        if qage is not None and float(qage) > float(self.cfg.max_quote_age_sec):
                            reasons.append(f"stale_quote:{float(qage):.3f}s")
                else:
                    qage = m.get("quote_age_sec")
                    if qage is not None and float(qage) > float(self.cfg.max_quote_age_sec):
                        reasons.append(f"stale_quote:{float(qage):.3f}s")
            except Exception:
                pass

        # 4) Cost coverage (edge > cost + buffer)
        if not is_safety:
            market_for_cost = m
            if is_option:
                try:
                    opt_bid = float(extras.get("opt_bid_px") or 0.0)
                    opt_ask = float(extras.get("opt_ask_px") or 0.0)
                    if opt_bid > 0 and opt_ask > 0:
                        market_for_cost = dict(market_for_cost or {})
                        market_for_cost["bid_px"] = opt_bid
                        market_for_cost["ask_px"] = opt_ask
                        market_for_cost["mid_px"] = (opt_bid + opt_ask) / 2.0
                except Exception:
                    market_for_cost = m
            c = self.cost.estimate(env, market=market_for_cost, bars=bars)

            # Prefer explicit edge from envelope extras when available.
            # Fallback to signal_score mapping to preserve legacy behavior.
            edge_bp_raw = (env.extras or {}).get("edge_bp")
            try:
                if edge_bp_raw is not None:
                    edge_bp = max(0.0, float(edge_bp_raw))
                else:
                    edge_bp = max(0.0, env.signal_score * 100.0)
            except Exception:
                edge_bp = max(0.0, env.signal_score * 100.0)

            total_cost_bp = (
                c["slippage_bp"] +
                c["fees_bp"] +
                c["impact_bp"] +
                self.cfg.edge_buffer_bp
            )

            if edge_bp <= total_cost_bp:
                reasons.append(
                    f"edge_insufficient:{edge_bp:.2f}<=cost:{total_cost_bp:.2f}"
                )
                logger.debug("Edge insufficient",
                            decision_id=env.decision_id,
                            edge_bp=edge_bp,
                            cost_bp=total_cost_bp)

        # Result
        ok = len(reasons) == 0
        effective_ttl_ms = env.freshness.ttl_ms

        if not ok:
            logger.info("Decision validation failed",
                       decision_id=env.decision_id,
                       symbol=env.symbol,
                       reasons=reasons)

        return ValidateResult(
            ok=ok,
            skip_reasons=reasons,
            now_us=now_us,
            effective_ttl_ms=effective_ttl_ms
        )


class SimpleCostModel:
    """
    Simple cost estimation model

    Estimates:
    - Slippage: Based on spread and ATR
    - Fees: Fixed commission rate
    - Impact: Placeholder (can be enhanced with volume analysis)
    """

    def estimate(
        self,
        env: DecisionEnvelope,
        market: Any,
        bars: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """
        Estimate trading costs

        Args:
            env: Decision envelope
            market: Market state
            bars: Historical bars

        Returns:
            Cost estimates (bp)
        """
        # Calculate spread
        spread_bp = 0.0
        bid_px = market.get('bid_px')
        ask_px = market.get('ask_px')
        if bid_px and ask_px:
            mid = (ask_px + bid_px) / 2
            spread_bp = (ask_px - bid_px) / mid * 1e4

        # Get ATR from latest bar
        atr_bp = 5.0  # Default
        if bars:
            atr_bp = bars[-1].get("atr_bp", 5.0)

        # Slippage: max(half spread, 20% of ATR)
        slippage_bp = max(spread_bp / 2, 0.2 * atr_bp)

        # Fixed commission
        fees_bp = 0.3

        # Impact cost (placeholder - could enhance with volume/ADV analysis)
        impact_bp = 0.0

        return {
            "slippage_bp": slippage_bp,
            "fees_bp": fees_bp,
            "impact_bp": impact_bp,
            "confidence": 0.6
        }
