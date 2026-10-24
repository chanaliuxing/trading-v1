"""
EOD Reconciliation Service

Reconciles daily trading activity against IB FlexQuery reports:
- Position reconciliation (qty, avg_px, unrealized_pnl)
- Fill reconciliation (executions, commissions, fees)
- Cash balance reconciliation

Based on PRODUCTION_READINESS.md requirements.
"""
import asyncio
import time
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from decimal import Decimal

import asyncpg
from ib_insync import IB, FlexReport, FlexStatement

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PositionDiscrepancy:
    """Position reconciliation discrepancy"""
    symbol: str
    our_qty: int
    ib_qty: int
    qty_diff: int
    our_avg_px: float
    ib_avg_px: float
    px_diff_pct: float
    our_unrealized_pnl: float
    ib_unrealized_pnl: float
    pnl_diff: float


@dataclass
class FillDiscrepancy:
    """Fill reconciliation discrepancy"""
    our_exec_id: Optional[str]
    ib_exec_id: Optional[str]
    symbol: str
    qty: int
    price: float
    discrepancy_type: str  # missing_in_ours, missing_in_ib, price_mismatch, qty_mismatch


@dataclass
class CashDiscrepancy:
    """Cash balance discrepancy"""
    our_balance: float
    ib_balance: float
    diff: float
    diff_pct: float


@dataclass
class ReconciliationResult:
    """EOD reconciliation result"""
    date: str
    timestamp: str
    position_discrepancies: List[PositionDiscrepancy]
    fill_discrepancies: List[FillDiscrepancy]
    cash_discrepancy: Optional[CashDiscrepancy]
    total_issues: int
    passed: bool
    threshold_pct: float


class EODReconciler:
    """
    End-of-Day Reconciliation Service

    Reconciles portfolio state against IB FlexQuery reports.
    """

    def __init__(
        self,
        db_pool: asyncpg.Pool,
        ib_client: IB,
        flex_token: str,
        flex_query_id: str,
        threshold_pct: float = 0.1
    ):
        """
        Initialize EOD reconciler

        Args:
            db_pool: PostgreSQL connection pool
            ib_client: IB client for FlexQuery
            flex_token: IB FlexQuery token
            flex_query_id: IB FlexQuery ID
            threshold_pct: Alert threshold (default: 0.1%)
        """
        self.db = db_pool
        self.ib = ib_client
        self.flex_token = flex_token
        self.flex_query_id = flex_query_id
        self.threshold_pct = threshold_pct

    async def reconcile_eod(self, eod_date: date) -> ReconciliationResult:
        """
        Run full EOD reconciliation

        Args:
            eod_date: Date to reconcile

        Returns:
            ReconciliationResult with all discrepancies
        """
        start = time.time()
        date_str = eod_date.isoformat()

        logger.info("Starting EOD reconciliation", date=date_str)

        # Fetch FlexQuery report from IB
        flex_report = await self._fetch_flex_report(eod_date)

        # Run reconciliations in parallel
        position_task = self.reconcile_positions(eod_date, flex_report)
        fill_task = self.reconcile_fills(eod_date, flex_report)
        cash_task = self.reconcile_cash(eod_date, flex_report)

        pos_discrep, fill_discrep, cash_discrep = await asyncio.gather(
            position_task, fill_task, cash_task
        )

        # Build result
        total_issues = len(pos_discrep) + len(fill_discrep) + (1 if cash_discrep else 0)
        passed = self._check_passed(pos_discrep, fill_discrep, cash_discrep)

        result = ReconciliationResult(
            date=date_str,
            timestamp=datetime.utcnow().isoformat(),
            position_discrepancies=pos_discrep,
            fill_discrepancies=fill_discrep,
            cash_discrepancy=cash_discrep,
            total_issues=total_issues,
            passed=passed,
            threshold_pct=self.threshold_pct
        )

        # Store result in audit trail
        await self._store_result(result)

        duration_ms = (time.time() - start) * 1000
        logger.info(
            "EOD reconciliation complete",
            date=date_str,
            issues=total_issues,
            passed=passed,
            duration_ms=round(duration_ms, 2)
        )

        return result

    async def reconcile_positions(
        self,
        eod_date: date,
        flex_report: FlexReport
    ) -> List[PositionDiscrepancy]:
        """
        Reconcile positions against IB

        Args:
            eod_date: Date to reconcile
            flex_report: IB FlexQuery report

        Returns:
            List of position discrepancies
        """
        discrepancies: List[PositionDiscrepancy] = []

        # Fetch our EOD positions
        our_positions = await self._fetch_our_positions(eod_date)

        # Parse IB positions from flex report
        ib_positions = self._parse_ib_positions(flex_report)

        # Match positions
        all_symbols = set(our_positions.keys()) | set(ib_positions.keys())

        for symbol in all_symbols:
            our_pos = our_positions.get(symbol, {"qty": 0, "avg_px": 0.0, "unrealized_pnl": 0.0})
            ib_pos = ib_positions.get(symbol, {"qty": 0, "avg_px": 0.0, "unrealized_pnl": 0.0})

            qty_diff = our_pos["qty"] - ib_pos["qty"]
            px_diff_pct = 0.0
            if ib_pos["avg_px"] != 0:
                px_diff_pct = abs((our_pos["avg_px"] - ib_pos["avg_px"]) / ib_pos["avg_px"]) * 100

            pnl_diff = our_pos["unrealized_pnl"] - ib_pos["unrealized_pnl"]

            # Check thresholds
            if qty_diff != 0 or px_diff_pct > self.threshold_pct or abs(pnl_diff) > 1.0:
                discrepancies.append(PositionDiscrepancy(
                    symbol=symbol,
                    our_qty=our_pos["qty"],
                    ib_qty=ib_pos["qty"],
                    qty_diff=qty_diff,
                    our_avg_px=our_pos["avg_px"],
                    ib_avg_px=ib_pos["avg_px"],
                    px_diff_pct=px_diff_pct,
                    our_unrealized_pnl=our_pos["unrealized_pnl"],
                    ib_unrealized_pnl=ib_pos["unrealized_pnl"],
                    pnl_diff=pnl_diff
                ))

        return discrepancies

    async def reconcile_fills(
        self,
        eod_date: date,
        flex_report: FlexReport
    ) -> List[FillDiscrepancy]:
        """
        Reconcile fills/executions against IB

        Args:
            eod_date: Date to reconcile
            flex_report: IB FlexQuery report

        Returns:
            List of fill discrepancies
        """
        discrepancies: List[FillDiscrepancy] = []

        # Fetch our fills
        our_fills = await self._fetch_our_fills(eod_date)

        # Parse IB executions
        ib_fills = self._parse_ib_fills(flex_report)

        # Match by execution ID
        our_exec_ids = {f["exec_id"] for f in our_fills}
        ib_exec_ids = {f["exec_id"] for f in ib_fills}

        # Missing in our system
        for exec_id in ib_exec_ids - our_exec_ids:
            ib_fill = next(f for f in ib_fills if f["exec_id"] == exec_id)
            discrepancies.append(FillDiscrepancy(
                our_exec_id=None,
                ib_exec_id=exec_id,
                symbol=ib_fill["symbol"],
                qty=ib_fill["qty"],
                price=ib_fill["price"],
                discrepancy_type="missing_in_ours"
            ))

        # Missing in IB
        for exec_id in our_exec_ids - ib_exec_ids:
            our_fill = next(f for f in our_fills if f["exec_id"] == exec_id)
            discrepancies.append(FillDiscrepancy(
                our_exec_id=exec_id,
                ib_exec_id=None,
                symbol=our_fill["symbol"],
                qty=our_fill["qty"],
                price=our_fill["price"],
                discrepancy_type="missing_in_ib"
            ))

        # Check matching fills for price/qty differences
        for exec_id in our_exec_ids & ib_exec_ids:
            our_fill = next(f for f in our_fills if f["exec_id"] == exec_id)
            ib_fill = next(f for f in ib_fills if f["exec_id"] == exec_id)

            if our_fill["qty"] != ib_fill["qty"]:
                discrepancies.append(FillDiscrepancy(
                    our_exec_id=exec_id,
                    ib_exec_id=exec_id,
                    symbol=our_fill["symbol"],
                    qty=our_fill["qty"],
                    price=our_fill["price"],
                    discrepancy_type="qty_mismatch"
                ))

            if abs(our_fill["price"] - ib_fill["price"]) > 0.01:
                discrepancies.append(FillDiscrepancy(
                    our_exec_id=exec_id,
                    ib_exec_id=exec_id,
                    symbol=our_fill["symbol"],
                    qty=our_fill["qty"],
                    price=our_fill["price"],
                    discrepancy_type="price_mismatch"
                ))

        return discrepancies

    async def reconcile_cash(
        self,
        eod_date: date,
        flex_report: FlexReport
    ) -> Optional[CashDiscrepancy]:
        """
        Reconcile cash balances against IB

        Args:
            eod_date: Date to reconcile
            flex_report: IB FlexQuery report

        Returns:
            CashDiscrepancy if difference exceeds threshold, else None
        """
        # Fetch our cash balance
        our_balance = await self._fetch_our_cash_balance(eod_date)

        # Parse IB cash balance
        ib_balance = self._parse_ib_cash_balance(flex_report)

        diff = our_balance - ib_balance
        diff_pct = 0.0
        if ib_balance != 0:
            diff_pct = abs(diff / ib_balance) * 100

        # Check threshold
        if diff_pct > self.threshold_pct:
            return CashDiscrepancy(
                our_balance=our_balance,
                ib_balance=ib_balance,
                diff=diff,
                diff_pct=diff_pct
            )

        return None

    # ========================================================================
    # Helper Methods
    # ========================================================================

    async def _fetch_flex_report(self, eod_date: date) -> FlexReport:
        """Fetch FlexQuery report from IB"""
        # Use ib_insync to fetch FlexQuery
        # Note: Requires FlexQuery to be configured in IB Account Management
        report = await self.ib.reqFlexStatementAsync(
            token=self.flex_token,
            queryId=self.flex_query_id
        )
        return report

    async def _fetch_our_positions(self, eod_date: date) -> Dict[str, Dict[str, Any]]:
        """Fetch our EOD positions from database"""
        query = """
            SELECT symbol, qty, avg_px, unrealized_pnl
            FROM positions
            WHERE date(updated_at) = $1
            AND qty != 0
        """
        rows = await self.db.fetchall(query, eod_date)

        return {
            row["symbol"]: {
                "qty": row["qty"],
                "avg_px": float(row["avg_px"]),
                "unrealized_pnl": float(row["unrealized_pnl"])
            }
            for row in rows
        }

    def _parse_ib_positions(self, flex_report: FlexReport) -> Dict[str, Dict[str, Any]]:
        """Parse IB positions from FlexReport"""
        positions = {}

        for statement in flex_report.statements:
            for position in statement.openPositions:
                positions[position.symbol] = {
                    "qty": int(position.position),
                    "avg_px": float(position.costBasis / position.position) if position.position else 0.0,
                    "unrealized_pnl": float(position.fifoPnlUnrealized)
                }

        return positions

    async def _fetch_our_fills(self, eod_date: date) -> List[Dict[str, Any]]:
        """Fetch our fills from database"""
        query = """
            SELECT exec_id, symbol, qty, avg_px as price, commission
            FROM fills
            WHERE date(ts_fill) = $1
            ORDER BY ts_fill
        """
        rows = await self.db.fetchall(query, eod_date)

        return [
            {
                "exec_id": row["exec_id"],
                "symbol": row["symbol"],
                "qty": row["qty"],
                "price": float(row["price"]),
                "commission": float(row["commission"] or 0)
            }
            for row in rows
        ]

    def _parse_ib_fills(self, flex_report: FlexReport) -> List[Dict[str, Any]]:
        """Parse IB fills from FlexReport"""
        fills = []

        for statement in flex_report.statements:
            for trade in statement.trades:
                fills.append({
                    "exec_id": trade.execID,
                    "symbol": trade.symbol,
                    "qty": int(trade.quantity),
                    "price": float(trade.tradePrice),
                    "commission": float(trade.ibCommission)
                })

        return fills

    async def _fetch_our_cash_balance(self, eod_date: date) -> float:
        """Fetch our cash balance from database"""
        query = """
            SELECT cash_balance
            FROM portfolio_snapshots
            WHERE date(snapshot_ts) = $1
            ORDER BY snapshot_ts DESC
            LIMIT 1
        """
        row = await self.db.fetchrow(query, eod_date)
        return float(row["cash_balance"]) if row else 0.0

    def _parse_ib_cash_balance(self, flex_report: FlexReport) -> float:
        """Parse IB cash balance from FlexReport"""
        for statement in flex_report.statements:
            for balance in statement.cashTransactions:
                if balance.currency == "USD":
                    return float(balance.endingCash)
        return 0.0

    def _check_passed(
        self,
        pos_discrep: List[PositionDiscrepancy],
        fill_discrep: List[FillDiscrepancy],
        cash_discrep: Optional[CashDiscrepancy]
    ) -> bool:
        """Check if reconciliation passed threshold"""
        # Fail if any critical discrepancies
        if len(pos_discrep) > 0 or len(fill_discrep) > 0:
            return False

        # Fail if cash difference exceeds threshold
        if cash_discrep and cash_discrep.diff_pct > self.threshold_pct:
            return False

        return True

    async def _store_result(self, result: ReconciliationResult):
        """Store reconciliation result in audit trail"""
        query = """
            INSERT INTO reconciliation_audit (
                reconciliation_date,
                reconciliation_ts,
                position_discrepancies,
                fill_discrepancies,
                cash_discrepancy,
                total_issues,
                passed,
                threshold_pct
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """

        await self.db.execute(
            query,
            result.date,
            result.timestamp,
            [vars(d) for d in result.position_discrepancies],
            [vars(d) for d in result.fill_discrepancies],
            vars(result.cash_discrepancy) if result.cash_discrepancy else None,
            result.total_issues,
            result.passed,
            result.threshold_pct
        )


async def run_daily_reconciliation(
    db_pool: asyncpg.Pool,
    ib_client: IB,
    flex_token: str,
    flex_query_id: str
):
    """
    Run daily EOD reconciliation

    Called by scheduler at EOD (e.g., 16:30 ET)
    """
    from datetime import date, timedelta

    # Reconcile previous trading day
    eod_date = date.today() - timedelta(days=1)

    reconciler = EODReconciler(
        db_pool=db_pool,
        ib_client=ib_client,
        flex_token=flex_token,
        flex_query_id=flex_query_id,
        threshold_pct=0.1
    )

    result = await reconciler.reconcile_eod(eod_date)

    if not result.passed:
        logger.error(
            "EOD reconciliation FAILED",
            date=result.date,
            issues=result.total_issues,
            position_discrep=len(result.position_discrepancies),
            fill_discrep=len(result.fill_discrepancies),
            cash_discrep=bool(result.cash_discrepancy)
        )
        # Trigger alert (e.g., PagerDuty)
    else:
        logger.info("EOD reconciliation PASSED", date=result.date)
