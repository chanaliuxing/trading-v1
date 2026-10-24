"""
Cold Startup Reconciliation
Based on doc/39.md specification

Responsibilities:
- Adopt open orders from broker (by orderRef or fuzzy matching)
- Clean up ghost orders (DB has, broker doesn't)
- Rebuild order_map and OCO bindings
- Ensure OCO protection for unprotected positions
- Run within 5 seconds on startup
"""
import asyncio
import time
from typing import Dict, List, Optional, Set, Any
from dataclasses import dataclass

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ReconciliationResult:
    """Reconciliation run result"""
    adopted_count: int
    orphaned_count: int
    ghost_cleaned: int
    positions_protected: int
    ib_open_orders: int
    db_open_orders: int
    duration_ms: float


class Reconciler:
    """
    Cold startup reconciliation

    On system restart:
    1. Fetch IB open orders + positions
    2. Adopt orders by orderRef (direct match)
    3. Fuzzy match orders without orderRef
    4. Clean ghost orders (DB has, IB doesn't)
    5. Ensure OCO protection for all positions
    """

    def __init__(
        self,
        adapter: Any,  # IBInsyncAdapter
        kernel: Any,  # SafetyKernel
        pg_pool: Any,  # asyncpg.Pool
        price_tol_bps: int = 15,
        time_window_s: int = 6 * 3600
    ):
        """
        Initialize reconciler

        Args:
            adapter: IB adapter (provides open_orders, portfolio)
            kernel: Safety kernel (provides adopt_*, ensure_oco)
            pg_pool: PostgreSQL connection pool
            price_tol_bps: Price tolerance for fuzzy matching (basis points)
            time_window_s: Time window for recent plan matching (seconds)
        """
        self.adapter = adapter
        self.kernel = kernel
        self.pg = pg_pool
        self.price_tol_bps = price_tol_bps
        self.time_window_s = time_window_s

        logger.info(
            "Reconciler initialized",
            price_tol_bps=price_tol_bps,
            time_window_s=time_window_s
        )

    def _price_close(self, px1: Optional[float], px2: Optional[float], bps: int) -> bool:
        """
        Check if two prices are within tolerance

        Args:
            px1: First price
            px2: Second price
            bps: Tolerance in basis points

        Returns:
            True if within tolerance
        """
        if not px1 or not px2:
            return False

        diff_bps = abs(px1 - px2) / max(px1, px2) * 1e4
        return diff_bps <= bps

    async def _fetch_ib_state(self) -> tuple:
        """
        Fetch IB current state

        Returns:
            (open_orders, portfolio_items)
        """
        # Fetch open orders
        ib_orders = await self.adapter.open_orders()

        # Fetch portfolio (positions)
        ib_portfolio = await asyncio.to_thread(
            lambda: list(self.adapter.ib.portfolio())
        )

        logger.info(
            "IB state fetched",
            open_orders=len(ib_orders),
            positions=len(ib_portfolio)
        )

        return ib_orders, ib_portfolio

    async def _fetch_db_open_orders(self) -> Dict[str, Dict]:
        """
        Fetch open orders from database

        Returns:
            Dict[order_id, order_record]
        """
        async with self.pg.acquire() as con:
            rows = await con.fetch("""
                SELECT order_id, symbol, side, limit_px, stop_px,
                       tif, oca_group, idempotency_key, state, created_at
                FROM core.core_order
                WHERE state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')
            """)

        db_open = {str(r["order_id"]): dict(r) for r in rows}

        logger.info("DB open orders fetched", count=len(db_open))

        return db_open

    async def _adopt_by_ref(
        self,
        ib_orders: List,
        adopted: Set[str]
    ) -> None:
        """
        Adopt orders by orderRef (direct match)

        Args:
            ib_orders: List of IB orders
            adopted: Set to track adopted order IDs
        """
        # Build orderRef index
        ref_index: Dict[str, List] = {}
        for o in ib_orders:
            if o.order_ref:
                ref_index.setdefault(o.order_ref, []).append(o)

        # Get live plans from kernel
        live_plans = getattr(self.kernel, 'live_plans', lambda: [])()

        adopted_count = 0

        for plan in live_plans:
            idemp = plan.idempotency_key

            # Check for entry, stop, and take-profit orders
            for suffix in ['ENT', 'STP', 'TP']:
                ref = f"{idemp}:{suffix}"

                for o in ref_index.get(ref, []):
                    # Adopt order into kernel
                    await self.kernel.adopt_external_order(plan, o)
                    adopted.add(o.order_id)
                    adopted_count += 1

                    logger.debug(
                        "Order adopted by ref",
                        order_id=o.order_id,
                        ref=ref,
                        symbol=o.symbol
                    )

        logger.info("Orders adopted by ref", count=adopted_count)

    async def _fuzzy_match(
        self,
        ib_orders: List,
        adopted: Set[str]
    ) -> int:
        """
        Fuzzy match orders without orderRef

        Args:
            ib_orders: List of IB orders
            adopted: Set of already adopted order IDs

        Returns:
            Number of orphaned orders
        """
        orphaned = 0

        for o in ib_orders:
            if o.order_id in adopted:
                continue

            # Find recent plan by symbol + side
            cand = getattr(
                self.kernel,
                'find_recent_plan',
                lambda **kw: None
            )(
                symbol=o.symbol,
                side=o.side,
                within_s=self.time_window_s
            )

            if not cand:
                # No matching plan - orphan
                await self._handle_orphan(o)
                orphaned += 1
                continue

            # Check price tolerance
            price_ok = True

            if o.order_type in ("LMT", "STP_LMT"):
                last_px = getattr(cand, 'last_intent_px', None)
                if not self._price_close(o.limit_px, last_px, self.price_tol_bps):
                    price_ok = False

            if o.order_type in ("STP", "STP_LMT"):
                last_stop = getattr(cand, 'last_stop_px', None)
                if not self._price_close(o.stop_px, last_stop, self.price_tol_bps):
                    price_ok = False

            if not price_ok:
                # Price mismatch - orphan
                await self._handle_orphan(o)
                orphaned += 1
                continue

            # Adopt successfully
            await self.kernel.adopt_external_order(cand, o)
            adopted.add(o.order_id)

            logger.debug(
                "Order fuzzy matched",
                order_id=o.order_id,
                symbol=o.symbol,
                side=o.side
            )

        logger.info("Fuzzy matching complete", orphaned=orphaned)

        return orphaned

    async def _handle_orphan(self, order: Any) -> None:
        """
        Handle orphan order (no matching plan)

        Args:
            order: IB order
        """
        # Cancel orphan or flag for review
        cancel_fn = getattr(self.kernel, 'cancel_or_flag_orphan', None)

        if cancel_fn:
            await cancel_fn(order)
        else:
            # Fallback: cancel directly
            try:
                await self.adapter.cancel_order(order.order_id)
                logger.warning(
                    "Orphan order cancelled",
                    order_id=order.order_id,
                    symbol=order.symbol
                )
            except Exception as e:
                logger.error(
                    "Failed to cancel orphan",
                    order_id=order.order_id,
                    error=str(e)
                )

    async def _clean_ghosts(
        self,
        db_open: Dict[str, Dict],
        ib_orders: List,
        adopted: Set[str]
    ) -> int:
        """
        Clean ghost orders (DB has, IB doesn't)

        Args:
            db_open: DB open orders
            ib_orders: IB open orders
            adopted: Set of adopted order IDs

        Returns:
            Number of ghosts cleaned
        """
        ib_order_ids = {o.order_id for o in ib_orders}
        cleaned = 0

        for oid, rec in db_open.items():
            if oid in adopted or oid in ib_order_ids:
                continue

            # Ghost: in DB but not in IB
            async with self.pg.acquire() as con:
                await con.execute("""
                    UPDATE core.core_order
                    SET state = 'CANCELLED', updated_at = now()
                    WHERE order_id = $1
                """, oid)

            cleaned += 1

            logger.warning(
                "Ghost order cleaned",
                order_id=oid,
                symbol=rec["symbol"],
                state=rec["state"]
            )

        logger.info("Ghost orders cleaned", count=cleaned)

        return cleaned

    async def _ensure_position_protection(
        self,
        ib_portfolio: List
    ) -> int:
        """
        Ensure OCO protection for all positions

        Args:
            ib_portfolio: IB portfolio items

        Returns:
            Number of positions protected
        """
        protected = 0

        for item in ib_portfolio:
            sym = item.contract.symbol
            qty = float(item.position)

            if abs(qty) < 1e-9:
                continue

            # Find or create plan for position
            find_fn = getattr(
                self.kernel,
                'find_or_create_plan_for_position',
                None
            )

            if not find_fn:
                logger.warning(
                    "Cannot protect position - method not found",
                    symbol=sym,
                    qty=qty
                )
                continue

            plan = find_fn(
                symbol=sym,
                qty=qty,
                avg_px=float(item.averageCost)
            )

            # Ensure OCO protection
            ensure_fn = getattr(self.kernel, 'ensure_oco', None)

            if ensure_fn:
                await ensure_fn(plan)
                protected += 1

                logger.info(
                    "Position protected",
                    symbol=sym,
                    qty=qty,
                    avg_px=item.averageCost
                )

        logger.info("Positions protected", count=protected)

        return protected

    async def run_once(self) -> ReconciliationResult:
        """
        Run reconciliation once

        Returns:
            ReconciliationResult
        """
        start = time.time()

        logger.info("Reconciliation started")

        try:
            # 1. Fetch IB state
            ib_orders, ib_portfolio = await self._fetch_ib_state()

            # 2. Fetch DB open orders
            db_open = await self._fetch_db_open_orders()

            # 3. Adopt by orderRef
            adopted: Set[str] = set()
            await self._adopt_by_ref(ib_orders, adopted)

            # 4. Fuzzy match
            orphaned = await self._fuzzy_match(ib_orders, adopted)

            # 5. Clean ghosts
            ghost_cleaned = await self._clean_ghosts(db_open, ib_orders, adopted)

            # 6. Ensure position protection
            protected = await self._ensure_position_protection(ib_portfolio)

            # Build result
            duration_ms = (time.time() - start) * 1000

            result = ReconciliationResult(
                adopted_count=len(adopted),
                orphaned_count=orphaned,
                ghost_cleaned=ghost_cleaned,
                positions_protected=protected,
                ib_open_orders=len(ib_orders),
                db_open_orders=len(db_open),
                duration_ms=duration_ms
            )

            logger.info(
                "Reconciliation complete",
                adopted=result.adopted_count,
                orphaned=result.orphaned_count,
                ghost_cleaned=result.ghost_cleaned,
                protected=result.positions_protected,
                duration_ms=round(duration_ms, 1)
            )

            # Emit metrics
            if hasattr(self.kernel, 'metrics'):
                self.kernel.metrics.reconcile_runs.inc()
                self.kernel.metrics.reconcile_duration.observe(duration_ms / 1000.0)

            return result

        except Exception as e:
            logger.error("Reconciliation failed", error=str(e), exc_info=True)
            raise
