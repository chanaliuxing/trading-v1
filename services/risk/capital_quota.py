"""
Dynamic Capital Quota Orchestrator
Based on doc/64.md specification

Automatically adjusts strategy and per-symbol capital limits based on:
- Volatility (inverse volatility weighting - risk parity)
- Performance (half-Kelly sizing)
- Drawdown (tier-based scaling with cooldown)

Features:
- Rate limiting (max 10-20% change per 5min)
- Cooldown periods after drawdown events
- Bleed-down protection (never force liquidation)
- Multi-dimensional caps (strategy-level + per-symbol)
"""
import asyncio
import time
import math
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from libs.db.ch_pool import threadsafe_query
from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CapsPolicy:
    """Capital quota policy configuration"""
    step_pct_limit: float = 0.10  # Max change per 5min (10%)
    cooldown_min: int = 30  # Cooldown period after drawdown (minutes)
    floor_usd: float = 2000  # Minimum per-symbol cap
    sigma_target: float = 0.02  # Target volatility (30m)
    g_edge_k: float = 0.2  # Half-Kelly scaling coefficient
    m_min: float = 0.2  # Minimum multiplier
    m_max: float = 1.2  # Maximum multiplier


@dataclass
class StrategyMetrics:
    """Strategy performance metrics"""
    sigma_30m: float  # 30-min volatility
    dd_60m: float  # 60-min drawdown
    dd_day: float  # Daily drawdown
    E_pnl: float  # Expected PnL
    Var_pnl: float  # Variance of PnL
    n_trades: int  # Number of trades


class CapOrchestrator:
    """
    Dynamic capital quota orchestrator

    Adjusts strategy and per-symbol caps based on:
    - Volatility (risk parity)
    - Performance (half-Kelly)
    - Drawdown (tiered reduction + cooldown)
    """

    def __init__(
        self,
        ch_client: any,
        risk_service: any,
        arbiter: any,
        ledger: any,
        audit_writer: any,
        policy: Optional[CapsPolicy] = None
    ):
        """
        Initialize orchestrator

        Args:
            ch_client: ClickHouse client
            risk_service: Risk service for setting caps
            arbiter: Arbiter for symbol-level caps
            ledger: Reservation ledger
            audit_writer: Audit log writer
            policy: Caps policy configuration
        """
        self.ch = ch_client
        self.risk = risk_service
        self.arb = arbiter
        self.ledger = ledger
        self.audit = audit_writer
        self.policy = policy or CapsPolicy()

        # State
        self.cooldown_until: Dict[str, int] = {}  # strategy_id -> epoch_seconds
        self.last_caps: Dict[str, Dict[str, float]] = {}  # strategy_id -> {symbol: cap}

        logger.info(
            "CapOrchestrator initialized",
            step_limit=self.policy.step_pct_limit,
            cooldown_min=self.policy.cooldown_min,
            floor_usd=self.policy.floor_usd
        )

    async def fetch_sigma_30m(self, symbols: List[str]) -> Dict[str, float]:
        """
        Fetch 30-minute volatility (EWMA) for symbols

        Args:
            symbols: List of symbols

        Returns:
            Dict[symbol, sigma]
        """
        sql = """
        WITH ret AS (
            SELECT symbol, ts_end,
                   log(close / lagInFrame(close, 1) OVER (PARTITION BY symbol ORDER BY ts_end)) AS r
            FROM bars_5s
            WHERE ts_end > now() - INTERVAL 40 MINUTE
              AND symbol IN {symbols:Array(String)}
        ),
        ewma AS (
            SELECT symbol, ts_end,
                   expWeightedMovingAverage(r*r, 0.94) OVER (
                       PARTITION BY symbol ORDER BY ts_end
                       ROWS BETWEEN 120 PRECEDING AND CURRENT ROW
                   ) AS v_ewma
            FROM ret
        )
        SELECT symbol,
               sqrt(anyLast(v_ewma)) AS sigma_30m
        FROM ewma
        GROUP BY symbol
        """

        result = await asyncio.to_thread(
            threadsafe_query,
            self.ch,
            sql,
            {"symbols": symbols},
        )

        sigma_dict = {row["symbol"]: float(row["sigma_30m"]) for row in result}

        # Fill missing with default
        for sym in symbols:
            if sym not in sigma_dict:
                sigma_dict[sym] = 0.02  # Default 2%

        return sigma_dict

    async def fetch_drawdown(self, strategy_id: str) -> Dict[str, float]:
        """
        Fetch drawdown metrics for strategy

        Args:
            strategy_id: Strategy ID

        Returns:
            Dict with dd_60m, dd_day
        """
        sql = """
        WITH pnl AS (
            SELECT ts_end, strategy_id,
                   sum(upl) OVER (
                       PARTITION BY strategy_id ORDER BY ts_end
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS nav
            FROM pnl_pos_5s
            WHERE strategy_id = {strategy_id:String}
              AND ts_end > now() - INTERVAL 1 DAY
        ),
        dd AS (
            SELECT strategy_id, ts_end, nav,
                   max(nav) OVER (
                       PARTITION BY strategy_id ORDER BY ts_end
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS peak
            FROM pnl
        )
        SELECT strategy_id,
               max((peak - nav) / nullIf(peak, 1)) FILTER (
                   WHERE ts_end > now() - INTERVAL 60 MINUTE
               ) AS dd_60m,
               max((peak - nav) / nullIf(peak, 1)) FILTER (
                   WHERE toDate(ts_end) = toDate(now())
               ) AS dd_day
        FROM dd
        GROUP BY strategy_id
        """

        result = await asyncio.to_thread(
            threadsafe_query,
            self.ch,
            sql,
            {"strategy_id": strategy_id},
        )

        if not result:
            return {"dd_60m": 0.0, "dd_day": 0.0}

        row = result[0]
        return {
            "dd_60m": float(row.get("dd_60m") or 0.0),
            "dd_day": float(row.get("dd_day") or 0.0)
        }

    async def fetch_performance(
        self,
        strategy_id: str,
        symbols: List[str]
    ) -> Dict[str, Dict]:
        """
        Fetch performance metrics (E, Var, n) from last 100 trades

        Args:
            strategy_id: Strategy ID
            symbols: List of symbols

        Returns:
            Dict[symbol, {E, Var, n}]
        """
        sql = """
        WITH last100 AS (
            SELECT strategy_id, symbol, ts, realized_pnl
            FROM pnl_fills
            WHERE strategy_id = {strategy_id:String}
              AND symbol IN {symbols:Array(String)}
              AND ts > now() - INTERVAL 5 DAY
            ORDER BY ts DESC
            LIMIT 100 BY strategy_id, symbol
        )
        SELECT strategy_id, symbol,
               avg(realized_pnl) AS E_pnl,
               varSamp(realized_pnl) AS Var_pnl,
               count() AS n
        FROM last100
        GROUP BY strategy_id, symbol
        """

        result = await asyncio.to_thread(
            threadsafe_query,
            self.ch,
            sql,
            {
                "strategy_id": strategy_id,
                "symbols": symbols,
            },
        )

        perf_dict = {}
        for row in result:
            perf_dict[row["symbol"]] = {
                "E": float(row.get("E_pnl") or 0.0),
                "Var": float(row.get("Var_pnl") or 1.0),
                "n": int(row.get("n") or 0)
            }

        # Fill missing
        for sym in symbols:
            if sym not in perf_dict:
                perf_dict[sym] = {"E": 0.0, "Var": 1.0, "n": 0}

        return perf_dict

    def calculate_scaling_multiplier(
        self,
        dd_day: float,
        sigma_port: float,
        E_total: float,
        Var_total: float,
        n_total: int,
        now_s: int,
        strategy_id: str
    ) -> float:
        """
        Calculate dynamic scaling multiplier M

        Args:
            dd_day: Daily drawdown
            sigma_port: Portfolio volatility
            E_total: Total expected PnL
            Var_total: Total PnL variance
            n_total: Total number of trades
            now_s: Current timestamp (seconds)
            strategy_id: Strategy ID

        Returns:
            Multiplier M in [m_min, m_max]
        """
        # Volatility scaling (inverse)
        g_vol = min(1.0, max(0.5, self.policy.sigma_target / max(1e-9, sigma_port)))

        # Drawdown scaling (tiered)
        if dd_day < 0.005:  # < 0.5%
            g_dd = 1.0
        elif dd_day < 0.01:  # 0.5-1.0%
            g_dd = 0.7
        else:  # > 1.0%
            g_dd = 0.4
            # Enter cooldown
            cooldown_end = now_s + self.policy.cooldown_min * 60
            self.cooldown_until[strategy_id] = max(
                self.cooldown_until.get(strategy_id, 0),
                cooldown_end
            )

            logger.warning(
                "Drawdown triggered cooldown",
                strategy_id=strategy_id,
                dd_day=dd_day,
                g_dd=g_dd,
                cooldown_until=cooldown_end
            )

        # Performance scaling (half-Kelly)
        g_edge = 1.0
        if Var_total > 1e-9 and n_total >= 30:
            # Half-Kelly: f* = (E/Var) / 2
            f_star = (E_total / Var_total) / 2.0
            g_edge = min(1.2, max(0.8, 1.0 + self.policy.g_edge_k * f_star))

        # Combine
        M = min(g_vol, g_dd) * g_edge

        # Cooldown constraint: no upscaling during cooldown
        if now_s < self.cooldown_until.get(strategy_id, 0):
            M = min(M, 1.0)

        # Global bounds
        M = max(self.policy.m_min, min(self.policy.m_max, M))

        logger.debug(
            "Scaling multiplier calculated",
            strategy_id=strategy_id,
            g_vol=round(g_vol, 3),
            g_dd=round(g_dd, 3),
            g_edge=round(g_edge, 3),
            M=round(M, 3)
        )

        return M

    def apply_rate_limit(self, old: float, new: float) -> float:
        """
        Apply rate limit to cap change

        Args:
            old: Old cap value
            new: New cap value

        Returns:
            Rate-limited new cap
        """
        limit = self.policy.step_pct_limit
        max_up = old * (1.0 + limit)
        max_down = old * (1.0 - limit)

        return min(max(new, max_down), max_up)

    async def propose_caps(
        self,
        strategy_id: str,
        symbols: List[str],
        static_cap: float,
        sym_static_caps: Dict[str, float]
    ) -> Tuple[float, Dict[str, float]]:
        """
        Propose new caps for strategy and symbols

        Args:
            strategy_id: Strategy ID
            symbols: List of symbols
            static_cap: Static strategy cap (hard limit)
            sym_static_caps: Per-symbol static caps

        Returns:
            (strategy_cap_eff, per_symbol_caps)
        """
        now_s = int(time.time())

        # Fetch metrics
        sigma = await self.fetch_sigma_30m(symbols)
        dd = await self.fetch_drawdown(strategy_id)
        perf = await self.fetch_performance(strategy_id, symbols)

        # Portfolio volatility (median of symbol volatilities)
        sigma_values = [sigma.get(s, 0.02) for s in symbols]
        sigma_port = sorted(sigma_values)[len(sigma_values) // 2] if sigma_values else 0.02

        # Aggregate performance
        E_total = sum(perf[s]["E"] for s in symbols if s in perf)
        Var_total = sum(perf[s]["Var"] for s in symbols if s in perf)
        n_total = sum(perf[s]["n"] for s in symbols if s in perf)

        # Calculate scaling multiplier
        M = self.calculate_scaling_multiplier(
            dd_day=dd["dd_day"],
            sigma_port=sigma_port,
            E_total=E_total,
            Var_total=Var_total,
            n_total=n_total,
            now_s=now_s,
            strategy_id=strategy_id
        )

        # Effective strategy cap
        cap_strategy_eff = static_cap * M

        # Risk parity weights (inverse volatility)
        inv_sigma = {s: 1.0 / max(1e-6, sigma.get(s, 0.02)) for s in symbols}
        sum_inv = sum(inv_sigma.values())
        weights = {s: inv_sigma[s] / sum_inv for s in symbols}

        # Propose per-symbol caps
        proposed_caps = {}
        for s in symbols:
            raw_cap = weights[s] * cap_strategy_eff
            floor = self.policy.floor_usd
            hard_cap = sym_static_caps.get(s, cap_strategy_eff)

            proposed_caps[s] = min(max(raw_cap, floor), hard_cap)

        # Apply rate limiting & bleed-down protection
        last_caps = self.last_caps.get(strategy_id, {})
        final_caps = {}

        for s in symbols:
            if s in last_caps:
                # Rate limit
                rate_limited = self.apply_rate_limit(last_caps[s], proposed_caps[s])

                # Bleed-down: if current usage > new cap, don't force liquidation
                if hasattr(self.ledger, 'get_used'):
                    try:
                        used = await self.ledger.get_used(strategy_id, s)
                        if used > rate_limited:
                            logger.warning(
                                "Bleed-down protection activated",
                                strategy_id=strategy_id,
                                symbol=s,
                                used=round(used, 0),
                                new_cap=round(rate_limited, 0)
                            )
                            rate_limited = max(rate_limited, used)
                    except Exception as e:
                        logger.error("Failed to check usage for bleed-down", error=str(e))

                final_caps[s] = rate_limited
            else:
                final_caps[s] = proposed_caps[s]

        logger.info(
            "Caps proposed",
            strategy_id=strategy_id,
            M=round(M, 3),
            cap_strategy_eff=round(cap_strategy_eff, 0),
            per_symbol_caps={k: round(v, 0) for k, v in final_caps.items()}
        )

        return cap_strategy_eff, final_caps

    async def apply_caps(
        self,
        strategy_id: str,
        cap_strategy_eff: float,
        per_symbol_caps: Dict[str, float]
    ) -> None:
        """
        Apply caps to risk service and arbiter

        Args:
            strategy_id: Strategy ID
            cap_strategy_eff: Effective strategy cap
            per_symbol_caps: Per-symbol caps
        """
        # Update risk service
        if hasattr(self.risk, 'set_strategy_caps'):
            await self.risk.set_strategy_caps(strategy_id, cap_strategy_eff, per_symbol_caps)

        # Update arbiter
        if hasattr(self.arb, 'set_symbol_caps'):
            await self.arb.set_symbol_caps(strategy_id, per_symbol_caps)

        # Audit log
        if hasattr(self.audit, 'record'):
            await self.audit.record(strategy_id, cap_strategy_eff, per_symbol_caps, reason="auto")

        # Update last caps
        self.last_caps[strategy_id] = per_symbol_caps

        logger.info(
            "Caps applied",
            strategy_id=strategy_id,
            cap_strategy_eff=round(cap_strategy_eff, 0),
            per_symbol_caps={k: round(v, 0) for k, v in per_symbol_caps.items()}
        )

    async def run_loop(
        self,
        strategies_config: List[Dict],
        interval_seconds: int = 300
    ) -> None:
        """
        Run orchestrator loop

        Args:
            strategies_config: List of strategy configs
            interval_seconds: Evaluation interval (default 5 minutes)
        """
        logger.info("CapOrchestrator loop started", interval_s=interval_seconds)

        while True:
            try:
                for strat_cfg in strategies_config:
                    strategy_id = strat_cfg["id"]
                    symbols = strat_cfg["symbols"]
                    static_cap = strat_cfg["static_cap_usd"]
                    sym_static_caps = strat_cfg.get("per_symbol_static_caps", {})

                    # Propose new caps
                    cap_eff, per_sym_caps = await self.propose_caps(
                        strategy_id=strategy_id,
                        symbols=symbols,
                        static_cap=static_cap,
                        sym_static_caps=sym_static_caps
                    )

                    # Apply caps
                    await self.apply_caps(strategy_id, cap_eff, per_sym_caps)

            except Exception as e:
                logger.error("Cap orchestrator error", error=str(e), exc_info=True)

            await asyncio.sleep(interval_seconds)
