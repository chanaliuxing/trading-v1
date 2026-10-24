"""
Portfolio Service
Provides low-latency position, PnL, and NAV queries for strategies and risk
Based on doc/75.md specification

Features:
- Position snapshots per strategy×symbol
- Strategy-level summaries (cash, NAV, UPL, RPL, exposures)
- Resource usage tracking
- P95 < 5ms query performance (using in-memory + Redis cache)
"""
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime
import asyncio

from libs.db.ch_pool import threadsafe_query
from libs.logger import get_logger
from libs.db.clickhouse import ClickHouseDB
from libs.db.postgres import PostgresDB

logger = get_logger(__name__)


@dataclass
class PositionSnapshot:
    """Position snapshot"""
    strategy_id: str
    symbol: str
    pos_qty: float
    avg_px: float
    mark_px: float
    upl: float
    rpl_cum: float
    notional: float
    ts: datetime


@dataclass
class StrategySummary:
    """Strategy-level portfolio summary"""
    strategy_id: str
    cash: float
    nav: float
    upl: float
    rpl: float
    gross_exposure: float
    net_exposure: float
    usage_ratio: float
    positions_count: int
    ts: datetime


class PortfolioService:
    """
    Portfolio service providing position and PnL queries

    Maintains cached state for low-latency access.
    """

    def __init__(
        self,
        ch: ClickHouseDB,
        pg: PostgresDB,
        redis_client: Optional[Any] = None
    ):
        """
        Initialize service

        Args:
            ch: ClickHouse client
            pg: PostgreSQL client
            redis_client: Optional Redis client for caching
        """
        self.ch = ch
        self.pg = pg
        self.redis = redis_client

        # In-memory cache
        self._positions: Dict[str, PositionSnapshot] = {}
        self._summaries: Dict[str, StrategySummary] = {}
        self._last_refresh: Optional[datetime] = None

        logger.info("PortfolioService initialized")

    async def position(
        self,
        strategy_id: str,
        symbol: str
    ) -> Optional[PositionSnapshot]:
        """
        Get position for strategy×symbol

        Args:
            strategy_id: Strategy ID
            symbol: Symbol

        Returns:
            Position snapshot or None if no position
        """
        key = f"{strategy_id}:{symbol}"

        # Try cache
        if key in self._positions:
            return self._positions[key]

        # Query ClickHouse
        sql = """
        SELECT
            strategy_id,
            symbol,
            anyLast(pos_qty) AS pos_qty,
            anyLast(avg_cost) AS avg_px,
            anyLast(mark_px) AS mark_px,
            anyLast(upl) AS upl,
            anyLast(ts_end) AS ts
        FROM pnl_pos_5s
        WHERE strategy_id = {strategy_id:String}
          AND symbol = {symbol:String}
          AND ts_end > now() - INTERVAL 20 MINUTE
        GROUP BY strategy_id, symbol
        """

        result = await asyncio.to_thread(
            threadsafe_query,
            self.ch,
            sql,
            {
                "strategy_id": strategy_id,
                "symbol": symbol,
            },
        )

        if not result:
            return None

        row = result[0]

        # Get cumulative RPL
        rpl_sql = """
        SELECT sum(realized_pnl) AS rpl
        FROM pnl_fills
        WHERE strategy_id = {strategy_id:String}
          AND symbol = {symbol:String}
          AND toDate(ts) = toDate(now())
        """

        rpl_result = await asyncio.to_thread(
            threadsafe_query,
            self.ch,
            rpl_sql,
            {
                "strategy_id": strategy_id,
                "symbol": symbol,
            },
        )

        rpl_cum = rpl_result[0]["rpl"] if rpl_result else 0.0

        pos = PositionSnapshot(
            strategy_id=row["strategy_id"],
            symbol=row["symbol"],
            pos_qty=float(row["pos_qty"]),
            avg_px=float(row["avg_px"]),
            mark_px=float(row["mark_px"]),
            upl=float(row["upl"]),
            rpl_cum=float(rpl_cum),
            notional=float(row["pos_qty"]) * float(row["mark_px"]),
            ts=row["ts"]
        )

        # Cache
        self._positions[key] = pos

        return pos

    async def summary(self, strategy_id: str) -> StrategySummary:
        """
        Get strategy-level portfolio summary

        Args:
            strategy_id: Strategy ID

        Returns:
            Strategy summary
        """
        # Try cache
        if strategy_id in self._summaries:
            return self._summaries[strategy_id]

        # Query positions
        sql = """
        WITH last_pos AS (
            SELECT
                strategy_id,
                symbol,
                anyLast(pos_qty) AS pos_qty,
                anyLast(avg_cost) AS avg_cost,
                anyLast(mark_px) AS mark_px,
                anyLast(upl) AS upl,
                anyLast(ts_end) AS ts_end
            FROM pnl_pos_5s
            WHERE strategy_id = {strategy_id:String}
              AND ts_end > now() - INTERVAL 20 MINUTE
            GROUP BY strategy_id, symbol
        ),
        rpl_today AS (
            SELECT
                strategy_id,
                sum(realized_pnl) AS rpl,
                sum(fee) AS fees
            FROM pnl_fills
            WHERE strategy_id = {strategy_id:String}
              AND toDate(ts) = toDate(now())
            GROUP BY strategy_id
        ),
        cap_cfg AS (
            SELECT strategy_id, base_cap
            FROM strategy_pools
            WHERE strategy_id = {strategy_id:String}
        )
        SELECT
            p.strategy_id,
            sum(abs(p.pos_qty * p.mark_px)) AS gross_exposure,
            sum(p.pos_qty * p.mark_px) AS net_exposure,
            sum(p.upl) AS upl_total,
            coalesce(r.rpl, 0) AS rpl_total,
            coalesce(r.fees, 0) AS fees_total,
            count(*) AS positions_count,
            any(c.base_cap) AS base_cap,
            any(p.ts_end) AS ts
        FROM last_pos p
        LEFT JOIN rpl_today r USING (strategy_id)
        LEFT JOIN cap_cfg c USING (strategy_id)
        GROUP BY p.strategy_id
        """

        result = await asyncio.to_thread(
            threadsafe_query,
            self.ch,
            sql,
            {"strategy_id": strategy_id},
        )

        if not result:
            # No positions
            return StrategySummary(
                strategy_id=strategy_id,
                cash=0.0,
                nav=0.0,
                upl=0.0,
                rpl=0.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                usage_ratio=0.0,
                positions_count=0,
                ts=datetime.now()
            )

        row = result[0]

        # Calculate cash (simplified - should track actual cash flows)
        # cash = initial_cash - fees + rpl
        cash = -float(row.get("fees_total", 0)) + float(row.get("rpl_total", 0))

        # NAV = cash + net_exposure
        nav = cash + float(row["net_exposure"])

        # Usage ratio
        base_cap = float(row.get("base_cap", 0) or 100000)
        usage_ratio = float(row["gross_exposure"]) / base_cap if base_cap > 0 else 0.0

        summary = StrategySummary(
            strategy_id=strategy_id,
            cash=cash,
            nav=nav,
            upl=float(row["upl_total"]),
            rpl=float(row["rpl_total"]),
            gross_exposure=float(row["gross_exposure"]),
            net_exposure=float(row["net_exposure"]),
            usage_ratio=usage_ratio,
            positions_count=int(row["positions_count"]),
            ts=row.get("ts", datetime.now())
        )

        # Cache
        self._summaries[strategy_id] = summary

        return summary

    async def positions_all(
        self,
        strategy_id: str
    ) -> List[PositionSnapshot]:
        """
        Get all positions for strategy

        Args:
            strategy_id: Strategy ID

        Returns:
            List of position snapshots
        """
        sql = """
        WITH last_pos AS (
            SELECT
                strategy_id,
                symbol,
                anyLast(pos_qty) AS pos_qty,
                anyLast(avg_cost) AS avg_px,
                anyLast(mark_px) AS mark_px,
                anyLast(upl) AS upl,
                anyLast(ts_end) AS ts
            FROM pnl_pos_5s
            WHERE strategy_id = {strategy_id:String}
              AND ts_end > now() - INTERVAL 20 MINUTE
            GROUP BY strategy_id, symbol
        ),
        rpl_today AS (
            SELECT
                strategy_id,
                symbol,
                sum(realized_pnl) AS rpl_cum
            FROM pnl_fills
            WHERE strategy_id = {strategy_id:String}
              AND toDate(ts) = toDate(now())
            GROUP BY strategy_id, symbol
        )
        SELECT
            p.strategy_id,
            p.symbol,
            p.pos_qty,
            p.avg_px,
            p.mark_px,
            p.upl,
            coalesce(r.rpl_cum, 0) AS rpl_cum,
            (p.pos_qty * p.mark_px) AS notional,
            p.ts
        FROM last_pos p
        LEFT JOIN rpl_today r USING (strategy_id, symbol)
        ORDER BY abs(notional) DESC
        """

        result = await asyncio.to_thread(
            threadsafe_query,
            self.ch,
            sql,
            {"strategy_id": strategy_id},
        )

        positions = []
        for row in result:
            pos = PositionSnapshot(
                strategy_id=row["strategy_id"],
                symbol=row["symbol"],
                pos_qty=float(row["pos_qty"]),
                avg_px=float(row["avg_px"]),
                mark_px=float(row["mark_px"]),
                upl=float(row["upl"]),
                rpl_cum=float(row["rpl_cum"]),
                notional=float(row["notional"]),
                ts=row["ts"]
            )
            positions.append(pos)

        return positions

    async def metrics(self) -> Dict[str, Any]:
        """
        Get service health metrics

        Returns:
            Metrics dict
        """
        # Calculate lag from latest pnl_pos_5s
        lag_sql = """
        SELECT
            max(ts_end) AS latest_ts,
            now() - max(ts_end) AS lag_seconds
        FROM pnl_pos_5s
        """

        result = await asyncio.to_thread(threadsafe_query, self.ch, lag_sql)
        lag = result[0]["lag_seconds"] if result else 0

        return {
            "cached_positions": len(self._positions),
            "cached_summaries": len(self._summaries),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "ledger_lag_s": lag,
            "status": "healthy" if lag < 10 else "degraded"
        }

    async def refresh_cache(self) -> None:
        """Refresh in-memory cache"""
        self._positions.clear()
        self._summaries.clear()
        self._last_refresh = datetime.now()
        logger.debug("Cache refreshed")

    async def reconcile_with_broker(
        self,
        broker_positions: List[Dict]
    ) -> List[Dict]:
        """
        Reconcile internal positions with broker

        Args:
            broker_positions: List of broker position dicts

        Returns:
            List of discrepancies
        """
        discrepancies = []

        for bp in broker_positions:
            strategy_id = bp.get("strategy_id", "unknown")
            symbol = bp["symbol"]
            broker_qty = float(bp["quantity"])

            # Get internal position
            internal = await self.position(strategy_id, symbol)
            internal_qty = internal.pos_qty if internal else 0.0

            diff = broker_qty - internal_qty

            if abs(diff) > 0.01:  # Tolerance
                discrepancies.append({
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "broker_qty": broker_qty,
                    "internal_qty": internal_qty,
                    "difference": diff,
                    "severity": "critical" if abs(diff) > 10 else "warning"
                })

                logger.warning(
                    "Position mismatch",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    broker_qty=broker_qty,
                    internal_qty=internal_qty,
                    diff=diff
                )

        return discrepancies
