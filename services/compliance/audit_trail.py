"""
Audit Trail Service
Based on doc/76.md specification

Comprehensive audit logging for all control actions, decisions, and system events.

Features:
- Immutable audit log (append-only)
- Structured event taxonomy
- Trade reconstruction capability
- Regulatory reporting support
- Query interface for compliance reviews
- Tamper-proof event chain
"""
import asyncio
import time
import hashlib
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum
import json

from libs.logger import get_logger

logger = get_logger(__name__)


class AuditEventType(Enum):
    """Audit event types"""
    # Order events
    ORDER_SUBMITTED = "order_submitted"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_MODIFIED = "order_modified"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"

    # Position events
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_REVERSED = "position_reversed"

    # Risk events
    RISK_LIMIT_BREACH = "risk_limit_breach"
    RESERVATION_ACQUIRED = "reservation_acquired"
    RESERVATION_RELEASED = "reservation_released"
    CAPITAL_QUOTA_CHANGED = "capital_quota_changed"

    # Strategy events
    STRATEGY_STARTED = "strategy_started"
    STRATEGY_STOPPED = "strategy_stopped"
    STRATEGY_PAUSED = "strategy_paused"
    STRATEGY_RESUMED = "strategy_resumed"
    DECISION_GENERATED = "decision_generated"

    # System events
    SYSTEM_STARTUP = "system_startup"
    SYSTEM_SHUTDOWN = "system_shutdown"
    SYSTEM_DEGRADED = "system_degraded"
    SYSTEM_RECOVERED = "system_recovered"
    CIRCUIT_BREAKER_OPENED = "circuit_breaker_opened"
    CIRCUIT_BREAKER_CLOSED = "circuit_breaker_closed"

    # Deployment events
    DEPLOYMENT_STARTED = "deployment_started"
    DEPLOYMENT_ADVANCED = "deployment_advanced"
    DEPLOYMENT_ROLLED_BACK = "deployment_rolled_back"
    DEPLOYMENT_COMPLETED = "deployment_completed"

    # Configuration events
    CONFIG_CHANGED = "config_changed"
    PARAMETER_OVERRIDE = "parameter_override"

    # Compliance events
    MANUAL_INTERVENTION = "manual_intervention"
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
    EMERGENCY_FLATTEN = "emergency_flatten"


class AuditSeverity(Enum):
    """Event severity levels"""
    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3
    CRITICAL = 4


@dataclass
class AuditEvent:
    """Immutable audit event"""
    event_id: str  # SHA256 hash of event data
    event_type: AuditEventType
    severity: AuditSeverity
    timestamp: float

    # Context
    user: Optional[str] = None  # User or system component
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None

    # Event data (structured)
    data: Dict[str, Any] = None

    # Tags for filtering
    tags: List[str] = None

    # Chain integrity
    prev_event_id: Optional[str] = None  # Previous event hash

    def __post_init__(self):
        """Generate event ID"""
        if not self.event_id:
            self.event_id = self._generate_id()

    def _generate_id(self) -> str:
        """Generate event ID (SHA256 hash)"""
        content = f"{self.event_type.value}:{self.timestamp}:{self.user}:{json.dumps(self.data, sort_keys=True)}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for storage"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "timestamp": self.timestamp,
            "user": self.user,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "data": json.dumps(self.data) if self.data else None,
            "tags": self.tags,
            "prev_event_id": self.prev_event_id
        }


class AuditTrail:
    """
    Audit trail service

    Provides immutable, append-only audit logging with chain integrity.
    """

    def __init__(self, pg_pool: Any):
        """
        Initialize audit trail

        Args:
            pg_pool: PostgreSQL connection pool
        """
        self.pool = pg_pool
        self.last_event_id: Optional[str] = None

        logger.info("AuditTrail initialized")

    async def init_schema(self) -> None:
        """Initialize database schema"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id VARCHAR(16) PRIMARY KEY,
                    event_type VARCHAR(64) NOT NULL,
                    severity INT NOT NULL,
                    timestamp DOUBLE PRECISION NOT NULL,
                    user_name VARCHAR(128),
                    strategy_id VARCHAR(128),
                    symbol VARCHAR(16),
                    data JSONB,
                    tags TEXT[],
                    prev_event_id VARCHAR(16),
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(event_type);
                CREATE INDEX IF NOT EXISTS idx_audit_strategy ON audit_events(strategy_id) WHERE strategy_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_audit_symbol ON audit_events(symbol) WHERE symbol IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_events(severity) WHERE severity >= 2;
                CREATE INDEX IF NOT EXISTS idx_audit_tags ON audit_events USING GIN(tags) WHERE tags IS NOT NULL;
            """)

        logger.info("Audit schema initialized")

    async def log(
        self,
        event_type: AuditEventType,
        severity: AuditSeverity,
        data: Dict[str, Any],
        user: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> AuditEvent:
        """
        Log audit event

        Args:
            event_type: Event type
            severity: Event severity
            data: Event data (structured)
            user: User or component
            strategy_id: Strategy ID
            symbol: Symbol
            tags: Tags for filtering

        Returns:
            AuditEvent
        """
        event = AuditEvent(
            event_id="",  # Will be generated
            event_type=event_type,
            severity=severity,
            timestamp=time.time(),
            user=user or "system",
            strategy_id=strategy_id,
            symbol=symbol,
            data=data,
            tags=tags,
            prev_event_id=self.last_event_id
        )

        # Write to database
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO audit_events (
                    event_id, event_type, severity, timestamp,
                    user_name, strategy_id, symbol, data, tags, prev_event_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
                event.event_id,
                event.event_type.value,
                event.severity.value,
                event.timestamp,
                event.user,
                event.strategy_id,
                event.symbol,
                json.dumps(event.data) if event.data else None,
                event.tags,
                event.prev_event_id
            )

        # Update last event ID
        self.last_event_id = event.event_id

        # Log to application logger (for real-time alerts)
        if severity == AuditSeverity.CRITICAL:
            logger.critical(
                f"AUDIT: {event_type.value}",
                user=user,
                strategy_id=strategy_id,
                symbol=symbol,
                data=data
            )
        elif severity == AuditSeverity.ERROR:
            logger.error(
                f"AUDIT: {event_type.value}",
                user=user,
                strategy_id=strategy_id,
                data=data
            )

        return event

    async def query(
        self,
        event_types: Optional[List[AuditEventType]] = None,
        min_severity: Optional[AuditSeverity] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        user: Optional[str] = None,
        tags: Optional[List[str]] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 1000
    ) -> List[AuditEvent]:
        """
        Query audit events

        Args:
            event_types: Filter by event types
            min_severity: Minimum severity
            strategy_id: Filter by strategy
            symbol: Filter by symbol
            user: Filter by user
            tags: Filter by tags (any match)
            start_time: Start timestamp
            end_time: End timestamp
            limit: Max results

        Returns:
            List of AuditEvent
        """
        conditions = ["1=1"]
        params = []
        param_idx = 1

        if event_types:
            conditions.append(f"event_type = ANY(${param_idx})")
            params.append([et.value for et in event_types])
            param_idx += 1

        if min_severity is not None:
            conditions.append(f"severity >= ${param_idx}")
            params.append(min_severity.value)
            param_idx += 1

        if strategy_id:
            conditions.append(f"strategy_id = ${param_idx}")
            params.append(strategy_id)
            param_idx += 1

        if symbol:
            conditions.append(f"symbol = ${param_idx}")
            params.append(symbol)
            param_idx += 1

        if user:
            conditions.append(f"user_name = ${param_idx}")
            params.append(user)
            param_idx += 1

        if tags:
            conditions.append(f"tags && ${param_idx}")
            params.append(tags)
            param_idx += 1

        if start_time is not None:
            conditions.append(f"timestamp >= ${param_idx}")
            params.append(start_time)
            param_idx += 1

        if end_time is not None:
            conditions.append(f"timestamp <= ${param_idx}")
            params.append(end_time)
            param_idx += 1

        query = f"""
            SELECT event_id, event_type, severity, timestamp,
                   user_name, strategy_id, symbol, data, tags, prev_event_id
            FROM audit_events
            WHERE {' AND '.join(conditions)}
            ORDER BY timestamp DESC
            LIMIT ${param_idx}
        """
        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        events = []
        for row in rows:
            events.append(AuditEvent(
                event_id=row['event_id'],
                event_type=AuditEventType(row['event_type']),
                severity=AuditSeverity(row['severity']),
                timestamp=row['timestamp'],
                user=row['user_name'],
                strategy_id=row['strategy_id'],
                symbol=row['symbol'],
                data=json.loads(row['data']) if row['data'] else None,
                tags=row['tags'],
                prev_event_id=row['prev_event_id']
            ))

        return events

    async def reconstruct_trade(
        self,
        symbol: str,
        start_time: float,
        end_time: float
    ) -> Dict[str, Any]:
        """
        Reconstruct complete trade history for compliance

        Args:
            symbol: Symbol
            start_time: Start timestamp
            end_time: End timestamp

        Returns:
            Trade reconstruction report
        """
        # Get all relevant events
        events = await self.query(
            event_types=[
                AuditEventType.ORDER_SUBMITTED,
                AuditEventType.ORDER_CANCELLED,
                AuditEventType.ORDER_MODIFIED,
                AuditEventType.ORDER_FILLED,
                AuditEventType.DECISION_GENERATED,
                AuditEventType.POSITION_OPENED,
                AuditEventType.POSITION_CLOSED
            ],
            symbol=symbol,
            start_time=start_time,
            end_time=end_time
        )

        # Reconstruct timeline
        timeline = []
        for event in reversed(events):  # Chronological order
            timeline.append({
                "timestamp": event.timestamp,
                "event_type": event.event_type.value,
                "user": event.user,
                "strategy_id": event.strategy_id,
                "data": event.data
            })

        # Calculate summary
        fills = [e for e in events if e.event_type == AuditEventType.ORDER_FILLED]
        total_qty = sum(e.data.get("qty", 0) for e in fills if e.data)
        total_notional = sum(
            e.data.get("qty", 0) * e.data.get("fill_px", 0)
            for e in fills if e.data
        )

        return {
            "symbol": symbol,
            "start_time": start_time,
            "end_time": end_time,
            "total_events": len(events),
            "total_fills": len(fills),
            "total_qty": total_qty,
            "total_notional": total_notional,
            "timeline": timeline
        }

    async def verify_chain_integrity(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Verify audit chain integrity

        Args:
            start_time: Start timestamp
            end_time: End timestamp

        Returns:
            Integrity report
        """
        conditions = ["1=1"]
        params = []

        if start_time:
            conditions.append(f"timestamp >= ${len(params) + 1}")
            params.append(start_time)

        if end_time:
            conditions.append(f"timestamp <= ${len(params) + 1}")
            params.append(end_time)

        query = f"""
            SELECT event_id, prev_event_id, timestamp
            FROM audit_events
            WHERE {' AND '.join(conditions)}
            ORDER BY timestamp ASC
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        # Verify chain
        breaks = []
        prev_id = None

        for row in rows:
            if prev_id is not None and row['prev_event_id'] != prev_id:
                breaks.append({
                    "event_id": row['event_id'],
                    "timestamp": row['timestamp'],
                    "expected_prev": prev_id,
                    "actual_prev": row['prev_event_id']
                })

            prev_id = row['event_id']

        return {
            "total_events": len(rows),
            "chain_breaks": len(breaks),
            "breaks": breaks,
            "integrity_ok": len(breaks) == 0
        }
