"""
Regulatory Reporter
Based on doc/76.md specification

Generate regulatory reports for compliance (CAT, OATS, etc.)

Features:
- CAT (Consolidated Audit Trail) reporting
- OATS (Order Audit Trail System) format
- Daily trade blotter
- Position reconciliation reports
- Risk exposure reports
- Incident reporting
"""
import asyncio
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import io

from libs.logger import get_logger
from services.compliance.audit_trail import AuditTrail, AuditEventType

logger = get_logger(__name__)


@dataclass
class CATReport:
    """CAT report structure"""
    report_date: str  # YYYYMMDD
    firm_id: str
    events: List[Dict[str, Any]]


@dataclass
class TradeBlotterEntry:
    """Trade blotter entry"""
    trade_date: str
    trade_time: str
    symbol: str
    side: str
    qty: int
    price: float
    order_id: str
    strategy_id: str
    commission: float
    net_amount: float


class RegulatoryReporter:
    """
    Regulatory reporting service

    Generates compliance reports in standard formats.
    """

    def __init__(
        self,
        audit_trail: AuditTrail,
        firm_id: str = "FIRM001"
    ):
        """
        Initialize regulatory reporter

        Args:
            audit_trail: Audit trail service
            firm_id: Firm identifier
        """
        self.audit = audit_trail
        self.firm_id = firm_id

        logger.info("RegulatoryReporter initialized", firm_id=firm_id)

    async def generate_cat_report(
        self,
        report_date: str
    ) -> CATReport:
        """
        Generate CAT (Consolidated Audit Trail) report

        Args:
            report_date: Report date (YYYYMMDD)

        Returns:
            CATReport
        """
        # Parse date
        dt = datetime.strptime(report_date, "%Y%m%d")
        start_ts = dt.replace(hour=0, minute=0, second=0, tzinfo=timezone.utc).timestamp()
        end_ts = dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc).timestamp()

        # Get order events
        events = await self.audit.query(
            event_types=[
                AuditEventType.ORDER_SUBMITTED,
                AuditEventType.ORDER_CANCELLED,
                AuditEventType.ORDER_MODIFIED,
                AuditEventType.ORDER_FILLED,
                AuditEventType.ORDER_REJECTED
            ],
            start_time=start_ts,
            end_time=end_ts
        )

        # Format as CAT events
        cat_events = []
        for event in events:
            cat_events.append({
                "eventTimestamp": self._format_cat_timestamp(event.timestamp),
                "eventType": self._map_to_cat_event_type(event.event_type),
                "symbol": event.symbol,
                "firmDesignatedID": event.data.get("order_id") if event.data else None,
                "orderKeyDate": report_date,
                "side": event.data.get("side") if event.data else None,
                "quantity": event.data.get("qty") if event.data else None,
                "price": event.data.get("limit_px") or event.data.get("fill_px") if event.data else None,
                "orderType": event.data.get("order_type") if event.data else None,
                "accountHolderType": "P",  # Proprietary
                "aggregatedOrders": "false"
            })

        return CATReport(
            report_date=report_date,
            firm_id=self.firm_id,
            events=cat_events
        )

    def _format_cat_timestamp(self, ts: float) -> str:
        """Format timestamp for CAT (YYYYMMDDHHMMSS.microseconds)"""
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y%m%d%H%M%S.%f")

    def _map_to_cat_event_type(self, event_type: AuditEventType) -> str:
        """Map internal event type to CAT event type"""
        mapping = {
            AuditEventType.ORDER_SUBMITTED: "MEOR",  # Order
            AuditEventType.ORDER_CANCELLED: "MEOF",  # Cancel
            AuditEventType.ORDER_MODIFIED: "MEOR",  # Replace
            AuditEventType.ORDER_FILLED: "MEOT",  # Trade
            AuditEventType.ORDER_REJECTED: "MEOR"  # Rejected order
        }
        return mapping.get(event_type, "MEOR")

    async def generate_trade_blotter(
        self,
        start_date: str,
        end_date: str
    ) -> List[TradeBlotterEntry]:
        """
        Generate daily trade blotter

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            List of TradeBlotterEntry
        """
        # Parse dates
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()

        # Get fill events
        fills = await self.audit.query(
            event_types=[AuditEventType.ORDER_FILLED],
            start_time=start_ts,
            end_time=end_ts
        )

        # Build blotter
        blotter = []
        for fill in fills:
            if not fill.data:
                continue

            dt = datetime.fromtimestamp(fill.timestamp, tz=timezone.utc)
            qty = fill.data.get("qty", 0)
            price = fill.data.get("fill_px", 0.0)
            commission = fill.data.get("commission", 0.0)

            # Calculate net amount
            gross = qty * price
            net = abs(gross) + commission

            blotter.append(TradeBlotterEntry(
                trade_date=dt.strftime("%Y-%m-%d"),
                trade_time=dt.strftime("%H:%M:%S"),
                symbol=fill.symbol or "",
                side=fill.data.get("side", ""),
                qty=abs(qty),
                price=price,
                order_id=fill.data.get("order_id", ""),
                strategy_id=fill.strategy_id or "",
                commission=commission,
                net_amount=net
            ))

        return blotter

    def export_trade_blotter_csv(
        self,
        blotter: List[TradeBlotterEntry]
    ) -> str:
        """
        Export trade blotter to CSV

        Args:
            blotter: Trade blotter entries

        Returns:
            CSV string
        """
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow([
            "Trade Date",
            "Trade Time",
            "Symbol",
            "Side",
            "Quantity",
            "Price",
            "Order ID",
            "Strategy ID",
            "Commission",
            "Net Amount"
        ])

        # Data
        for entry in blotter:
            writer.writerow([
                entry.trade_date,
                entry.trade_time,
                entry.symbol,
                entry.side,
                entry.qty,
                f"{entry.price:.4f}",
                entry.order_id,
                entry.strategy_id,
                f"{entry.commission:.2f}",
                f"{entry.net_amount:.2f}"
            ])

        return output.getvalue()

    async def generate_position_reconciliation(
        self,
        report_date: str
    ) -> Dict[str, Any]:
        """
        Generate position reconciliation report

        Args:
            report_date: Report date (YYYY-MM-DD)

        Returns:
            Reconciliation report
        """
        # Parse date (end of day)
        dt = datetime.strptime(report_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        end_ts = dt.timestamp()

        # Get position events up to date
        events = await self.audit.query(
            event_types=[
                AuditEventType.POSITION_OPENED,
                AuditEventType.POSITION_CLOSED,
                AuditEventType.ORDER_FILLED
            ],
            end_time=end_ts
        )

        # Reconstruct positions per symbol
        positions = {}
        for event in events:
            if not event.symbol or not event.data:
                continue

            if event.symbol not in positions:
                positions[event.symbol] = {
                    "qty": 0,
                    "cost_basis": 0.0,
                    "realized_pnl": 0.0
                }

            if event.event_type == AuditEventType.ORDER_FILLED:
                qty = event.data.get("qty", 0)
                price = event.data.get("fill_px", 0.0)

                pos = positions[event.symbol]
                pos["qty"] += qty
                pos["cost_basis"] += qty * price

        # Calculate unrealized PnL (would need market data)
        return {
            "report_date": report_date,
            "positions": positions,
            "total_symbols": len(positions)
        }

    async def generate_risk_exposure_report(
        self,
        report_date: str
    ) -> Dict[str, Any]:
        """
        Generate risk exposure report

        Args:
            report_date: Report date (YYYY-MM-DD)

        Returns:
            Risk exposure report
        """
        dt = datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ts = dt.timestamp()
        end_ts = dt.replace(hour=23, minute=59, second=59).timestamp()

        # Get risk events
        risk_events = await self.audit.query(
            event_types=[
                AuditEventType.RISK_LIMIT_BREACH,
                AuditEventType.CIRCUIT_BREAKER_OPENED,
                AuditEventType.KILL_SWITCH_TRIGGERED,
                AuditEventType.EMERGENCY_FLATTEN
            ],
            start_time=start_ts,
            end_time=end_ts
        )

        # Get capital quota changes
        quota_events = await self.audit.query(
            event_types=[AuditEventType.CAPITAL_QUOTA_CHANGED],
            start_time=start_ts,
            end_time=end_ts
        )

        return {
            "report_date": report_date,
            "risk_violations": len(risk_events),
            "quota_changes": len(quota_events),
            "events": [
                {
                    "timestamp": e.timestamp,
                    "type": e.event_type.value,
                    "strategy_id": e.strategy_id,
                    "data": e.data
                }
                for e in risk_events
            ]
        }

    async def generate_incident_report(
        self,
        incident_id: str,
        start_time: float,
        end_time: float,
        description: str
    ) -> Dict[str, Any]:
        """
        Generate incident report

        Args:
            incident_id: Incident identifier
            start_time: Incident start timestamp
            end_time: Incident end timestamp
            description: Incident description

        Returns:
            Incident report
        """
        # Get all events during incident
        all_events = await self.audit.query(
            start_time=start_time,
            end_time=end_time,
            min_severity=None  # Include all
        )

        # Categorize events
        critical_events = [e for e in all_events if e.severity.value >= 4]
        error_events = [e for e in all_events if e.severity.value == 3]

        # Get affected strategies
        affected_strategies = set(e.strategy_id for e in all_events if e.strategy_id)

        # Get affected symbols
        affected_symbols = set(e.symbol for e in all_events if e.symbol)

        return {
            "incident_id": incident_id,
            "start_time": start_time,
            "end_time": end_time,
            "duration_s": end_time - start_time,
            "description": description,
            "total_events": len(all_events),
            "critical_events": len(critical_events),
            "error_events": len(error_events),
            "affected_strategies": list(affected_strategies),
            "affected_symbols": list(affected_symbols),
            "timeline": [
                {
                    "timestamp": e.timestamp,
                    "event_type": e.event_type.value,
                    "severity": e.severity.name,
                    "user": e.user,
                    "data": e.data
                }
                for e in all_events[:100]  # Limit to first 100 for report
            ]
        }
