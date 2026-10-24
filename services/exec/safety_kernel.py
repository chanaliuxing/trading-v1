"""
Execution Safety Kernel
Based on doc/30.md specification

Key capabilities:
- First fill immediately attaches OCO (stop loss + take profit)
- Partial fill handling with proportional adjustments
- OCO invariant monitoring (100% protection)
- Rate limiting (place/modify/cancel RPM)
- Idempotent order placement
- Orphan order adoption on restart
- Memory Ghost protection (TTL for in-flight decisions)
"""
import asyncio
import os
import time
import uuid
import json
import inspect
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional, List, Any, Callable, Awaitable
from datetime import datetime, timezone, timedelta

from prometheus_client import Counter, Gauge, Histogram

from .manual_intervention_lock import (
    ManualInterventionLockStore,
    PostgresManualInterventionLockStore,
)
from .flatten_controller import FlattenController
from .models import OrderRole, PlacedOrder, OrderState, Fill, OCOPair, ProtectiveOrderOwner
from libs.config import config
from libs.logger import get_logger
from libs.db.postgres import PostgresDB
from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract
from services.ibkr.ib_connection_manager import get_ib_connection_manager
from services.orders.ledger import PositionLedger
from services.orders.protection import build_protection_metadata
from .protection_engine import (
    ProtectionEngine,
    ProtectionSpec,
    ProtectiveOrderState,
    close_action_for_side,
    order_type_for_mode,
)

logger = get_logger(__name__)
_UUID_ZERO = "00000000-0000-0000-0000-000000000000"


# ============================================================================
# Utility Classes
# ============================================================================

class TokenBucket:
    """Token bucket for rate limiting"""

    def __init__(self, rpm: int):
        self.capacity = max(1, rpm)
        self.refill_per_sec = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self.ts = time.time()

    def take(self, n: float = 1.0) -> bool:
        """Try to take n tokens"""
        now = time.time()
        elapsed = now - self.ts
        self.ts = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    async def wait(self, n: float = 1.0):
        """Wait until n tokens are available"""
        while not self.take(n):
            await asyncio.sleep(0.05)


def now_us() -> int:
    """Current timestamp in microseconds"""
    return int(time.time() * 1e6)


def side_flip(side: str) -> str:
    """Flip order side"""
    s = side.upper()
    return "BUY" if s == "SELL" else "SELL"


def _pos_float(value: Any) -> Optional[float]:
    try:
        x = float(value)
        return x if x > 0 else None
    except Exception:
        return None


def _merge_note_json(existing_note: Any, updates: Dict[str, Any]) -> str:
    payload: Dict[str, Any] = {}
    if isinstance(existing_note, str) and existing_note.strip():
        try:
            parsed = json.loads(existing_note)
            if isinstance(parsed, dict):
                payload = dict(parsed)
        except Exception:
            payload = {}
    payload.update({k: v for k, v in (updates or {}).items() if v is not None})
    return json.dumps(payload, ensure_ascii=False)


def _core_position_lock_key(strategy_id: str, symbol: str) -> str:
    return f"core_position:{str(strategy_id or '').strip()}:{str(symbol or '').strip().upper()}"


def _note_json_dict(note: Any) -> Dict[str, Any]:
    if isinstance(note, dict):
        return dict(note)
    if isinstance(note, str) and note.strip():
        try:
            parsed = json.loads(note)
            if isinstance(parsed, dict):
                return dict(parsed)
        except Exception:
            return {}
    return {}


def _lot_id_from_protection_key(protection_key: Any) -> str:
    parts = str(protection_key or "").split("|")
    if len(parts) >= 4:
        return str(parts[3] or "").strip()
    return ""


def _position_owner_key(
    *,
    strategy_id: Any,
    strategy_instance_id: Any,
    symbol: Any,
    lot_id: Any,
) -> str:
    strategy = str(strategy_id or "").strip()
    instance = str(strategy_instance_id or "").strip()
    sym = str(symbol or "").strip().upper()
    lot = str(lot_id or "").strip()
    if not (strategy and instance and sym and lot):
        return ""
    return f"{strategy}:{instance}:{sym}:{lot}"


def _protective_order_role(note_payload: Dict[str, Any], extras: Dict[str, Any], order_type: Any = None) -> Optional[OrderRole]:
    raw = str(
        extras.get("order_role")
        or note_payload.get("order_role")
        or extras.get("protection_kind")
        or note_payload.get("protection_kind")
        or note_payload.get("type")
        or ""
    ).strip().upper()
    if raw in {"STOP_LOSS", "STOP", "PROTECTIVE_STOP"}:
        return OrderRole.STOP_LOSS
    if raw in {"TAKE_PROFIT", "TARGET", "PROFIT_TAKER"}:
        return OrderRole.TAKE_PROFIT
    if raw in {"TRAILING_STOP", "TRAIL"}:
        return OrderRole.TRAILING_STOP
    if bool(extras.get("protective_stop")):
        return OrderRole.STOP_LOSS
    if str(order_type or "").strip().upper() in {"STP", "STP_LMT"} and (
        note_payload.get("protection_key") or extras.get("protection_key")
    ):
        return OrderRole.STOP_LOSS
    return None


def _dt_to_ts_us(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        try:
            return int(value.timestamp() * 1e6)
        except Exception:
            return None
    return None


# ============================================================================
# Metrics
# ============================================================================

metrics = {
    'kern_place': Counter('exec_place_total', 'Orders placed', ['kind']),
    'kern_modify': Counter('exec_modify_total', 'Orders modified'),
    'kern_cancel': Counter('exec_cancel_total', 'Orders cancelled'),
    'kern_err': Counter('exec_errors_total', 'Errors', ['op']),
    'kern_oco_attach': Counter('exec_oco_attach_total', 'OCO pairs attached'),
    'kern_oco_missing': Gauge('exec_oco_missing', 'Plans missing protection'),
    'kern_unprotected_win': Histogram(
        'exec_unprotected_window_ms',
        'Milliseconds from first fill to OCO',
        buckets=(50, 100, 200, 400, 800, 1600, 3200, 6400)
    ),
    'kern_inspect_ms': Histogram('exec_inspect_latency_ms', 'Inspection loop latency'),
    'kern_adopted': Counter('exec_orphan_adopted_total', 'Adopted orders', ['reason']),
}


# ============================================================================
# Position & Plan State
# ============================================================================

@dataclass
class Position:
    """Position state with FIFO average cost tracking"""
    qty: float = 0.0
    avg_px: float = 0.0

    def on_fill(self, side: str, fill_qty: float, fill_px: float):
        """Update position on fill"""
        s = 1 if side.upper() == "BUY" else -1
        new_qty = self.qty + s * fill_qty

        if self.qty == 0 or (self.qty > 0 and new_qty > 0) or (self.qty < 0 and new_qty < 0):
            # Same direction: weighted average
            total_qty = abs(self.qty) + fill_qty
            if total_qty > 0:
                self.avg_px = (abs(self.qty) * self.avg_px + fill_qty * fill_px) / total_qty
        elif (self.qty > 0 and new_qty < 0) or (self.qty < 0 and new_qty > 0):
            # Reversal: new position uses fill price
            self.avg_px = fill_px
        elif new_qty == 0:
            self.avg_px = 0.0

        self.qty = new_qty


@dataclass
class Plan:
    """Execution plan tracking"""
    strategy_id: str
    symbol: str
    account_id: str = "UNKNOWN_ACCOUNT"
    strategy_instance_id: str = ""
    lot_id: str = ""
    entry_order_id: Optional[int] = None
    entry_qty: float = 0.0
    entry_sent_ts_us: Optional[int] = None
    entry_timeout_sec: Optional[float] = None
    attach_oco: bool = True
    oco_config: Dict[str, Any] = field(default_factory=dict)
    filled_qty: float = 0.0
    avg_fill_px: float = 0.0
    first_fill_ts_us: Optional[int] = None
    oco_attached_ts_us: Optional[int] = None
    protection_failure_ts_us: Optional[int] = None
    protection_failure_reason: Optional[str] = None
    oco_pair: Optional[OCOPair] = None
    position: Position = field(default_factory=Position)
    status: str = "ACTIVE"  # ACTIVE, PROTECTED, PROTECTION_FAILED, CLOSED
    protection_rearm_count: int = 0
    last_protection_rearm_ts_us: Optional[int] = None
    protection_lock_set_ts_us: Optional[int] = None

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol or "").strip().upper()
        self.account_id = str(self.account_id or "UNKNOWN_ACCOUNT").strip() or "UNKNOWN_ACCOUNT"
        # Transitional compatibility only: legacy paper/unit callers may not yet
        # pass strategy_instance_id. Live ENTRY owner validation rejects that path
        # before broker placement.
        self.strategy_instance_id = str(self.strategy_instance_id or self.strategy_id or "").strip()
        self.lot_id = str(
            self.lot_id
            or self.oco_config.get("position_lot_id")
            or SafetyKernel.default_lot_id(
                account_id=self.account_id,
                strategy_instance_id=self.strategy_instance_id,
                symbol=self.symbol,
            )
        ).strip()


@dataclass(frozen=True)
class PlanOwnerKey:
    account_id: str
    strategy_instance_id: str
    symbol: str
    lot_id: str

    def as_key(self) -> str:
        return (
            f"{str(self.account_id or 'UNKNOWN_ACCOUNT').strip() or 'UNKNOWN_ACCOUNT'}:"
            f"{str(self.strategy_instance_id or '').strip()}:"
            f"{str(self.symbol or '').strip().upper()}:"
            f"{str(self.lot_id or '').strip()}"
        )


# ============================================================================
# Safety Kernel
# ============================================================================

class SafetyKernel:
    """
    Execution safety kernel with OCO protection

    Guarantees:
    - Every filled entry order has OCO protection within 1 second
    - Rate limiting on all broker operations
    - Idempotent order placement
    - Automatic orphan adoption on restart
    """

    def __init__(
        self,
        broker_adapter,
        pg: Optional[PostgresDB] = None,
        reservation_service: Optional[Any] = None,
        netting_fill_fanout: Optional[Any] = None,
        decision_finalizer: Optional[Callable[[str, str], Awaitable[None]]] = None,
        position_ledger: Optional[PositionLedger] = None,
        manual_intervention_lock_store: Optional[
            ManualInterventionLockStore | PostgresManualInterventionLockStore
        ] = None,
        flatten_controller: Optional[FlattenController] = None,
        place_rpm: int = 10,
        modify_rpm: int = 30,
        cancel_rpm: int = 30,
        oco_attach_timeout_sec: float = 1.0,
        oco_fill_coalesce_sec: float = 0.25,
        protection_failure_lock_ttl_sec: int = 1800,
        trail_limit_watchdog_enabled: bool = True,
        trail_limit_unfilled_timeout_sec: float = 3.0,
        trail_limit_fallback: str = "MARKETABLE_LMT",
        trail_limit_emergency_slippage_bps: float = 20.0,
    ):
        """
        Initialize safety kernel

        Args:
            broker_adapter: Broker adapter (IBInsyncAdapter)
            pg: PostgreSQL database (optional, for order persistence)
            reservation_service: Reservation service (optional, for capital release)
            place_rpm: Rate limit for placing orders (per minute)
            modify_rpm: Rate limit for modifying orders
            cancel_rpm: Rate limit for cancelling orders
            oco_attach_timeout_sec: Max time to attach OCO after first fill
        """
        self.broker = broker_adapter
        self.pg = pg
        self.reservation = reservation_service
        self.netting_fill_fanout = netting_fill_fanout
        self.decision_finalizer = decision_finalizer
        self.position_ledger = position_ledger or (PositionLedger(pg=self.pg) if self.pg is not None else None)
        self.manual_intervention_lock_store = manual_intervention_lock_store
        self.flatten_controller = flatten_controller or (
            FlattenController(
                pg=self.pg,
                manual_intervention_lock_store=self.manual_intervention_lock_store,
            )
            if self.pg is not None
            else None
        )

        # Rate limiters
        self.place_limiter = TokenBucket(place_rpm)
        self.modify_limiter = TokenBucket(modify_rpm)
        self.cancel_limiter = TokenBucket(cancel_rpm)

        # State
        self.plans: Dict[str, Plan] = {}  # account_id:strategy_instance_id:symbol:lot_id -> Plan
        self.orders: Dict[int, PlacedOrder] = {}  # order_id -> PlacedOrder
        self.oco_pairs: Dict[str, OCOPair] = {}  # oca_group -> OCOPair
        self.order_id_to_idemp: Dict[int, str] = {}  # order_id -> idempotency_key (for reservation release)

        # Config
        self.oco_timeout = oco_attach_timeout_sec
        self.oco_fill_coalesce_sec = max(
            0.0,
            min(float(oco_fill_coalesce_sec or 0.0), float(oco_attach_timeout_sec or 0.0)),
        )
        self.protection_pending_grace_sec = 8.0
        self.protection_rearm_cooldown_sec = 5.0
        self.protection_rearm_max_attempts = 3
        self.protection_failure_lock_ttl_sec = max(1, int(protection_failure_lock_ttl_sec or 1800))
        self.trail_limit_watchdog_enabled = bool(trail_limit_watchdog_enabled)
        self.trail_limit_unfilled_timeout_sec = max(0.0, float(trail_limit_unfilled_timeout_sec or 0.0))
        self.trail_limit_fallback = str(trail_limit_fallback or "MARKETABLE_LMT").strip().upper()
        self.trail_limit_emergency_slippage_bps = max(0.0, float(trail_limit_emergency_slippage_bps or 0.0))
        self._trail_limit_triggered_ts_by_order_id: Dict[int, float] = {}
        self._fill_order_resolve_retry_attempts = 4
        self._fill_order_resolve_retry_sleep_sec = 0.02
        self._fill_broker_order_id_fallback_recent_sec = 300.0
        self._allow_broker_order_id_fill_fallback = False

        # Locks
        self._lock = asyncio.Lock()
        self._running = False

        db_enabled = "enabled" if pg else "disabled"
        logger.info("SafetyKernel initialized",
                    place_rpm=place_rpm, modify_rpm=modify_rpm, cancel_rpm=cancel_rpm,
                    db_persistence=db_enabled)

    @staticmethod
    def _strategy_is_shadow_or_dry_run(strategy_id: str) -> bool:
        sid = str(strategy_id or "").strip()
        if not sid:
            return False
        for item in config.get("strategies", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() != sid:
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
            return bool(
                item.get("shadow_mode")
                or extras.get("dry_run")
                or extras.get("shadow_only")
            )
        return False

    async def start(self):
        """Start the kernel and background tasks"""
        self._running = True

        # Start inspection loop
        asyncio.create_task(self._inspection_loop())
        await self._bootstrap_open_positions()

        logger.info("SafetyKernel started")

    async def stop(self):
        """Stop the kernel"""
        self._running = False
        logger.info("SafetyKernel stopped")

    async def _set_protection_failure_lock(self, plan: Plan) -> None:
        if self.manual_intervention_lock_store is None:
            return
        if plan.protection_lock_set_ts_us:
            return
        try:
            await self.manual_intervention_lock_store.lock(
                strategy_id=plan.strategy_id,
                symbol=plan.symbol,
                ttl_sec=self.protection_failure_lock_ttl_sec,
                reason="protection_failed",
                context={
                    "status": plan.status,
                    "protection_failure_ts_us": int(plan.protection_failure_ts_us or now_us()),
                    "protection_failure_reason": str(plan.protection_failure_reason or ""),
                    "entry_order_id": int(plan.entry_order_id or 0),
                },
            )
            plan.protection_lock_set_ts_us = now_us()
            logger.error(
                "protection_failure_entry_lock_set",
                strategy_id=plan.strategy_id,
                symbol=plan.symbol,
                ttl_sec=self.protection_failure_lock_ttl_sec,
                reason=str(plan.protection_failure_reason or ""),
            )
        except Exception as exc:
            logger.warning(
                "protection_failure_entry_lock_set_failed",
                strategy_id=plan.strategy_id,
                symbol=plan.symbol,
                error=str(exc),
            )

    @staticmethod
    def _validate_oco_price_sanity(
        *,
        plan: Plan,
        entry_px: Any,
        sl_px: Any,
        tp_px: Any,
        oco_cfg: Dict[str, Any],
    ) -> tuple[bool, str]:
        entry = _pos_float(entry_px)
        sl = _pos_float(sl_px)
        tp = _pos_float(tp_px)
        if entry is None:
            return False, "oco_bad_entry_px"
        if sl is None or tp is None:
            return False, "oco_bad_protection_px"

        max_dev_bps = float(oco_cfg.get("price_sanity_max_dev_bps") or os.getenv("OCO_PRICE_SANITY_MAX_DEV_BPS", "5000"))
        max_dev = max(0.01, max_dev_bps / 10000.0)
        low = entry * (1.0 - max_dev)
        high = entry * (1.0 + max_dev)
        if sl < low or sl > high or tp < low or tp > high:
            return False, (
                "oco_price_out_of_entry_band:"
                f"entry={entry:.6f},sl={sl:.6f},tp={tp:.6f},band=[{low:.6f},{high:.6f}]"
            )

        market_ref = (
            _pos_float(oco_cfg.get("market_ref_px"))
            or _pos_float(oco_cfg.get("quote_px"))
            or _pos_float(oco_cfg.get("last_px"))
            or _pos_float(oco_cfg.get("manual_ref_px"))
        )
        if market_ref is not None:
            ref_low = market_ref * (1.0 - max_dev)
            ref_high = market_ref * (1.0 + max_dev)
            if entry < ref_low or entry > ref_high or sl < ref_low or sl > ref_high or tp < ref_low or tp > ref_high:
                return False, (
                    "oco_price_out_of_market_band:"
                    f"market_ref={market_ref:.6f},entry={entry:.6f},sl={sl:.6f},tp={tp:.6f},"
                    f"band=[{ref_low:.6f},{ref_high:.6f}]"
                )

        long_position = float(getattr(getattr(plan, "position", None), "qty", 0.0) or 0.0) > 0
        if long_position and not (sl < entry < tp):
            return False, f"oco_long_price_order_invalid:entry={entry:.6f},sl={sl:.6f},tp={tp:.6f}"
        if not long_position and not (tp < entry < sl):
            return False, f"oco_short_price_order_invalid:entry={entry:.6f},sl={sl:.6f},tp={tp:.6f}"
        return True, "ok"

    async def _clear_entry_lock(self, *, strategy_id: str, symbol: str, reason: str) -> None:
        if self.manual_intervention_lock_store is None:
            return
        unlock = getattr(self.manual_intervention_lock_store, "unlock", None)
        if not callable(unlock):
            return
        try:
            cleared = await unlock(strategy_id=str(strategy_id).strip(), symbol=str(symbol).strip().upper())
            if cleared:
                logger.info(
                    "entry_lock_cleared",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    reason=reason,
                )
        except Exception as exc:
            logger.warning(
                "entry_lock_clear_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                reason=reason,
                error=str(exc),
            )

    async def _clear_entry_lock_if_flat(self, *, strategy_id: str, symbol: str, reason: str) -> None:
        if self.pg is None or self.manual_intervention_lock_store is None:
            return
        try:
            row = await self.pg.fetchone(
                """
                SELECT pos_qty
                FROM core.core_position
                WHERE strategy_id = $1 AND symbol = $2
                """,
                strategy_id,
                symbol,
            )
            pos_qty = float((row or {}).get("pos_qty") or 0.0)
        except Exception:
            return
        if abs(pos_qty) > 1e-6:
            return
        try:
            open_count = await self.pg.fetchval(
                """
                SELECT COUNT(*)
                FROM core.core_order
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                """,
                strategy_id,
                symbol,
            )
        except Exception:
            try:
                _count_row = await self.pg.fetchone(
                    """
                    SELECT COUNT(*) AS count
                    FROM core.core_order
                    WHERE strategy_id = $1
                      AND symbol = $2
                      AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                    """,
                    strategy_id,
                    symbol,
                )
                open_count = int((_count_row or {}).get("count") or 0)
            except Exception:
                return
        if int(open_count or 0) > 0:
            return
        await self._clear_entry_lock(strategy_id=strategy_id, symbol=symbol, reason=reason)

    async def _update_close_only_lifecycle_note(
        self,
        *,
        order_uuid: Any,
        resolved_order: Dict[str, Any],
        state: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.pg or order_uuid is None or not resolved_order:
            return
        try:
            controller = self.flatten_controller or FlattenController(
                pg=self.pg,
                manual_intervention_lock_store=self.manual_intervention_lock_store,
            )
            await controller.mark_order_lifecycle(
                order_uuid=order_uuid,
                resolved_order=resolved_order,
                state=str(state or "").strip(),
                payload=payload,
            )
        except Exception as exc:
            logger.warning(
                "close_only_lifecycle_note_update_failed",
                order_id=str(order_uuid),
                state=state,
                error=str(exc),
            )

    @staticmethod
    def default_lot_id(*, account_id: str, strategy_instance_id: str, symbol: str) -> str:
        """Default owner-scoped lot id for callers that do not provide an entry lot."""
        account = str(account_id or "UNKNOWN_ACCOUNT").strip() or "UNKNOWN_ACCOUNT"
        instance = str(strategy_instance_id or "").strip()
        sym = str(symbol or "").strip().upper()
        return f"lot:{account}:{instance}:{sym}"

    def _plan_key(
        self,
        strategy_id: str,
        symbol: str,
        *,
        account_id: Optional[str] = None,
        strategy_instance_id: Optional[str] = None,
        lot_id: Optional[str] = None,
    ) -> str:
        """Generate owner-scoped plan key.

        Legacy two-argument callers intentionally receive a unique unsafe key so
        they cannot overwrite another strategy instance by collapsing to
        strategy_id:symbol. Production plan creation passes explicit owner fields.
        """
        account = str(account_id or "UNKNOWN_ACCOUNT").strip() or "UNKNOWN_ACCOUNT"
        instance = str(strategy_instance_id or "").strip()
        sym = str(symbol or "").strip().upper()
        if not instance:
            instance = f"legacy:{str(strategy_id or '').strip()}:{uuid.uuid4().hex[:8]}"
        lot = str(lot_id or "").strip() or self.default_lot_id(
            account_id=account,
            strategy_instance_id=instance,
            symbol=sym,
        )
        return PlanOwnerKey(
            account_id=account,
            strategy_instance_id=instance,
            symbol=sym,
            lot_id=lot,
        ).as_key()

    def _plan_key_for_plan(self, plan: Plan) -> str:
        return self._plan_key(
            plan.strategy_id,
            plan.symbol,
            account_id=plan.account_id,
            strategy_instance_id=plan.strategy_instance_id,
            lot_id=plan.lot_id,
        )

    def _build_placed_order_from_row(self, row: Dict[str, Any]) -> Optional[PlacedOrder]:
        try:
            broker_order_id = row.get("broker_order_id")
            if broker_order_id in (None, ""):
                return None
            order_id = int(str(broker_order_id))
        except Exception:
            return None
        try:
            perm_id = int(row.get("ib_perm_id") or 0)
        except Exception:
            perm_id = 0
        try:
            qty = float(row.get("qty") or 0.0)
        except Exception:
            qty = 0.0
        return PlacedOrder(
            order_id=order_id,
            broker_order_id=str(broker_order_id),
            perm_id=perm_id,
            order_ref=str(row.get("order_ref") or "").strip() or None,
            symbol=str(row.get("symbol") or "").strip().upper(),
            side=str(row.get("side") or "").strip().upper(),
            order_type=str(row.get("order_type") or "").strip().upper(),
            qty=qty,
            limit_px=float(row["limit_px"]) if row.get("limit_px") is not None else None,
            stop_px=float(row["stop_px"]) if row.get("stop_px") is not None else None,
            tif=str(row.get("tif") or "DAY").strip() or "DAY",
            status=str(row.get("state") or "SUBMITTED").strip() or "SUBMITTED",
            ts_us=_dt_to_ts_us(row.get("updated_at")) or now_us(),
        )

    @staticmethod
    def _broker_position_symbol(position: Any) -> str:
        contract = getattr(position, "contract", None)
        if contract is None:
            return ""
        try:
            mapped = internal_opt_symbol_from_ib_contract(contract)
            if mapped:
                return str(mapped).strip().upper()
        except Exception:
            pass
        return str(getattr(contract, "symbol", "") or "").strip().upper()

    async def _broker_position_qty_map(self) -> Optional[Dict[str, float]]:
        rows: List[Any]
        try:
            if hasattr(self.broker, "ib"):
                rows = list(self.broker.ib.positions() or [])
            elif hasattr(self.broker, "positions") and callable(getattr(self.broker, "positions")):
                result = self.broker.positions()
                rows = await result if inspect.isawaitable(result) else list(result or [])
            else:
                return None
        except Exception as exc:
            logger.warning("safety_kernel_bootstrap_positions_broker_probe_failed", error=str(exc))
            return None

        qty_by_symbol: Dict[str, float] = {}
        for row in rows or []:
            try:
                symbol = self._broker_position_symbol(row)
                if not symbol:
                    continue
                qty = float(getattr(row, "position", 0.0) or 0.0)
                if abs(qty) <= 1e-6:
                    continue
                qty_by_symbol[symbol] = float(qty_by_symbol.get(symbol, 0.0) + qty)
            except Exception:
                continue
        return qty_by_symbol

    async def _bootstrap_open_positions(self) -> None:
        if self.pg is None:
            return

        try:
            position_rows = await self.pg.fetchall(
                """
                SELECT strategy_id, symbol, pos_qty, avg_px, updated_at
                FROM core.core_position
                WHERE abs(COALESCE(pos_qty, 0)) > 0.000001
                ORDER BY strategy_id, symbol
                """
            )
        except Exception as exc:
            logger.warning("safety_kernel_bootstrap_positions_failed", error=str(exc))
            return

        if not position_rows:
            return

        broker_position_qty = await self._broker_position_qty_map()
        broker_open_orders: List[PlacedOrder] = []
        if hasattr(self.broker, "open_orders"):
            try:
                broker_open_orders = list(await self.broker.open_orders(force_refresh=True) or [])
            except Exception as exc:
                logger.warning("safety_kernel_bootstrap_open_orders_failed", error=str(exc))
                broker_open_orders = []

        broker_by_ref = {
            str(order.order_ref or "").strip(): order
            for order in broker_open_orders
            if str(order.order_ref or "").strip()
        }
        broker_by_id = {
            str(order.order_id): order
            for order in broker_open_orders
            if getattr(order, "order_id", None) is not None
        }

        attach_queue: List[Plan] = []
        recovered = 0
        active_owner_count_by_symbol: Dict[str, int] = {}
        active_owner_qty_by_symbol: Dict[str, float] = {}
        for row in position_rows or []:
            symbol_key = str(row.get("symbol") or "").strip().upper()
            if not symbol_key:
                continue
            try:
                row_qty = float(row.get("pos_qty") or 0.0)
            except Exception:
                row_qty = 0.0
            if abs(row_qty) <= 1e-6:
                continue
            active_owner_count_by_symbol[symbol_key] = active_owner_count_by_symbol.get(symbol_key, 0) + 1
            active_owner_qty_by_symbol[symbol_key] = active_owner_qty_by_symbol.get(symbol_key, 0.0) + row_qty

        for pos_row in position_rows or []:
            strategy_id = str(pos_row.get("strategy_id") or "").strip()
            symbol = str(pos_row.get("symbol") or "").strip().upper()
            if not strategy_id or not symbol:
                continue
            try:
                pos_qty = float(pos_row.get("pos_qty") or 0.0)
            except Exception:
                pos_qty = 0.0
            if abs(pos_qty) <= 1e-6:
                continue
            if broker_position_qty is not None:
                live_qty = float(broker_position_qty.get(symbol, 0.0) or 0.0)
                if abs(live_qty) <= 1e-6:
                    logger.warning(
                        "safety_kernel_bootstrap_skip_stale_db_position_broker_flat",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        db_pos_qty=pos_qty,
                    )
                    continue
                if pos_qty * live_qty < 0:
                    logger.warning(
                        "safety_kernel_bootstrap_skip_position_sign_mismatch",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        db_pos_qty=pos_qty,
                        broker_pos_qty=live_qty,
                    )
                    continue
                if (
                    active_owner_count_by_symbol.get(symbol, 0) > 1
                    and abs(abs(active_owner_qty_by_symbol.get(symbol, 0.0)) - abs(live_qty)) <= 1e-6
                ):
                    logger.info(
                        "safety_kernel_bootstrap_keep_owner_lane_position_qty",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        db_pos_qty=pos_qty,
                        broker_pos_qty=live_qty,
                        active_owner_count=active_owner_count_by_symbol.get(symbol, 0),
                    )
                elif abs(abs(pos_qty) - abs(live_qty)) > 1e-6:
                    logger.warning(
                        "safety_kernel_bootstrap_use_broker_position_qty",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        db_pos_qty=pos_qty,
                        broker_pos_qty=live_qty,
                    )
                    pos_qty = float(live_qty)

            try:
                parent_row = await self.pg.fetchrow(
                    """
                    SELECT order_id, broker_order_id, ib_perm_id, order_ref, strategy_id, symbol,
                           side, order_type, qty, limit_px, stop_px, tif, state, note, updated_at
                    FROM core.core_order
                    WHERE strategy_id = $1
                      AND symbol = $2
                      AND parent_order_id IS NULL
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    strategy_id,
                    symbol,
                )
            except Exception as exc:
                logger.warning(
                    "safety_kernel_bootstrap_parent_lookup_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
                continue

            if not parent_row:
                logger.warning(
                    "safety_kernel_bootstrap_parent_missing",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    pos_qty=pos_qty,
                )
                continue

            parent_order = self._build_placed_order_from_row(dict(parent_row))
            if parent_order is None:
                logger.warning(
                    "safety_kernel_bootstrap_parent_identity_missing",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    pos_qty=pos_qty,
                )
                continue

            note = _note_json_dict(parent_row.get("note"))
            extras = note.get("extras") if isinstance(note.get("extras"), dict) else {}
            account_id = str(
                extras.get("account_id")
                or note.get("account_id")
                or self._resolved_account_id()
                or "UNKNOWN_ACCOUNT"
            ).strip() or "UNKNOWN_ACCOUNT"
            strategy_instance_id = str(
                extras.get("strategy_instance_id")
                or note.get("strategy_instance_id")
                or strategy_id
            ).strip()
            lot_id = str(
                extras.get("position_lot_id")
                or extras.get("lot_id")
                or note.get("position_lot_id")
                or note.get("lot_id")
                or self.default_lot_id(
                    account_id=account_id,
                    strategy_instance_id=strategy_instance_id,
                    symbol=symbol,
                )
            ).strip()
            parent_side = str(getattr(parent_order, "side", "") or parent_row.get("side") or "").strip().upper()
            expected_entry_side = "BUY" if pos_qty > 0 else "SELL"
            close_only_parent = bool(
                extras.get("close_only")
                or extras.get("flatten_action")
                or extras.get("protective_stop")
                or extras.get("reduce_only")
            )
            attach_oco = (
                not bool(extras.get("disable_oco", False))
                and not close_only_parent
                and parent_side == expected_entry_side
            )
            if not attach_oco and close_only_parent:
                logger.warning(
                    "safety_kernel_bootstrap_skip_close_only_parent_protection",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    parent_order_id=parent_order.order_id,
                    parent_side=parent_side,
                )
            elif not attach_oco and parent_side and parent_side != expected_entry_side:
                logger.warning(
                    "safety_kernel_bootstrap_skip_parent_side_mismatch_protection",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    parent_order_id=parent_order.order_id,
                    parent_side=parent_side,
                    expected_entry_side=expected_entry_side,
                    pos_qty=pos_qty,
                )
            oco_config = dict(extras.get("protective_oco") or {})
            if attach_oco and (
                self._strategy_is_shadow_or_dry_run(strategy_id)
                or bool(extras.get("dry_run"))
                or bool(extras.get("shadow_only"))
            ):
                attach_oco = False
                logger.warning(
                    "safety_kernel_bootstrap_skip_shadow_dry_run_protection",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    pos_qty=pos_qty,
                )

            try:
                child_rows = await self.pg.fetchall(
                    """
                    SELECT order_id, broker_order_id, ib_perm_id, order_ref, symbol, side, order_type,
                           qty, limit_px, stop_px, tif, state, note, updated_at
                    FROM core.core_order
                    WHERE parent_order_id = $1
                      AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                    ORDER BY created_at ASC
                    """,
                    parent_row["order_id"],
                )
            except Exception as exc:
                logger.warning(
                    "safety_kernel_bootstrap_children_lookup_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
                child_rows = []

            matched_children: Dict[str, PlacedOrder] = {}
            for child_row in child_rows or []:
                child_dict = dict(child_row)
                broker_match: Optional[PlacedOrder] = None
                child_ref = str(child_dict.get("order_ref") or "").strip()
                if child_ref:
                    broker_match = broker_by_ref.get(child_ref)
                if broker_match is None:
                    broker_match = broker_by_id.get(str(child_dict.get("broker_order_id") or ""))
                if broker_match is None:
                    continue
                child_note = _note_json_dict(child_dict.get("note"))
                child_type = str(child_note.get("type") or "").strip().lower()
                if child_type == "stop_loss":
                    matched_children["sl"] = broker_match
                elif child_type == "take_profit":
                    matched_children["tp"] = broker_match
                elif str(broker_match.order_type or "").upper() in {"STP", "STP_LMT", "STOP", "STOP_LIMIT"}:
                    matched_children.setdefault("sl", broker_match)
                elif str(broker_match.order_type or "").upper() == "LMT":
                    matched_children.setdefault("tp", broker_match)

            avg_px_raw = pos_row.get("avg_px")
            try:
                avg_px = float(avg_px_raw) if avg_px_raw is not None else 0.0
            except Exception:
                avg_px = 0.0

            plan = Plan(
                strategy_id=strategy_id,
                symbol=symbol,
                account_id=account_id,
                strategy_instance_id=strategy_instance_id,
                lot_id=lot_id,
                entry_order_id=parent_order.order_id,
                entry_qty=float(abs(pos_qty)),
                attach_oco=attach_oco,
                oco_config=oco_config,
                filled_qty=float(abs(pos_qty)),
                avg_fill_px=avg_px if avg_px > 0.0 else (float(parent_order.limit_px or 0.0) or float(parent_order.stop_px or 0.0)),
                first_fill_ts_us=_dt_to_ts_us(parent_row.get("updated_at")) or _dt_to_ts_us(pos_row.get("updated_at")) or now_us(),
                position=Position(qty=pos_qty, avg_px=avg_px if avg_px > 0.0 else 0.0),
                status="ACTIVE",
            )

            if matched_children:
                oca_group = str((matched_children.get("sl") or matched_children.get("tp")).oca_group or "").strip()
                plan.oco_pair = OCOPair(
                    parent_order_id=parent_order.order_id,
                    stop_loss_order_id=matched_children.get("sl").order_id if matched_children.get("sl") else None,
                    take_profit_order_id=matched_children.get("tp").order_id if matched_children.get("tp") else None,
                    oca_group=oca_group or f"OCO_RECOVERED_{uuid.uuid4().hex[:8]}",
                    status="ACTIVE",
                )
                plan.status = "PROTECTED"

            async with self._lock:
                self.orders[parent_order.order_id] = parent_order
                if matched_children.get("sl") is not None:
                    self.orders[matched_children["sl"].order_id] = matched_children["sl"]
                if matched_children.get("tp") is not None:
                    self.orders[matched_children["tp"].order_id] = matched_children["tp"]
                self.plans[self._plan_key_for_plan(plan)] = plan

            recovered += 1
            if attach_oco and plan.oco_pair is None and abs(float(plan.position.qty or 0.0)) > 0.0:
                attach_queue.append(plan)
                logger.warning(
                    "safety_kernel_recovered_unprotected_position",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    pos_qty=pos_qty,
                )
            else:
                logger.info(
                    "safety_kernel_recovered_protected_position",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    pos_qty=pos_qty,
                    has_stop=plan.oco_pair.stop_loss_order_id is not None if plan.oco_pair else False,
                    has_tp=plan.oco_pair.take_profit_order_id is not None if plan.oco_pair else False,
                )

        for plan in attach_queue:
            try:
                await self._attach_oco_protection(plan)
            except Exception as exc:
                logger.error(
                    "safety_kernel_bootstrap_attach_failed",
                    strategy_id=plan.strategy_id,
                    symbol=plan.symbol,
                    error=str(exc),
                )

        if recovered:
            logger.info(
                "safety_kernel_bootstrap_complete",
                recovered_positions=recovered,
                attach_requested=len(attach_queue),
            )

    async def place_entry_order(
        self,
        strategy_id: str,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_px: Optional[float] = None,
        stop_px: Optional[float] = None,
        trail_amount: Optional[float] = None,
        trail_percent: Optional[float] = None,
        trail_stop_px: Optional[float] = None,
        lmt_price_offset: Optional[float] = None,
        tif: str = "DAY",
        order_ref: Optional[str] = None,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        attach_oco: bool = True,
        oco_protection: Optional[Dict[str, Any]] = None,
        decision_ttl_sec: float = 5.0, # Max time an order can wait in memory
        entry_timeout_sec: Optional[float] = None,
        expected_epoch: Optional[int] = None,
        account_id: Optional[str] = None,
        strategy_instance_id: Optional[str] = None,
        lot_id: Optional[str] = None,
    ) -> Optional[PlacedOrder]:
        """
        Place entry order with memory-ghost protection.

        NOTE:
        - This method creates an in-memory Plan (used for OCO enforcement).
        - For non-entry intents (flatten / protective_stop), prefer place_order(...)
          so we don't overwrite the entry plan for the same strategy+symbol.
        """
        start_wait_ts = time.time()
        try:
            order_type = str(order_type or "").strip().upper().replace(" ", "_")
            # 1. Rate limit wait
            await self.place_limiter.wait()

            # 2. EXPRIY CHECK: If we waited too long in memory, abort!
            if (time.time() - start_wait_ts) > decision_ttl_sec:
                logger.error("Order expired in memory queue", symbol=symbol, strategy_id=strategy_id, 
                             waited_sec=round(time.time() - start_wait_ts, 2))
                return None

            # Generate order reference
            if not order_ref:
                order_ref = f"entry:{strategy_id}:{symbol}:{uuid.uuid4().hex[:8]}"

            # Place order via broker
            if order_type == "MKT":
                order = await self.broker.place_market(
                    symbol, side, qty, tif, order_ref, outside_rth=outside_rth, exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "LMT":
                order = await self.broker.place_limit(
                    symbol, side, qty, limit_px, tif, order_ref, outside_rth=outside_rth, exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "STP":
                order = await self.broker.place_stop(
                    symbol, side, qty, stop_px, tif, order_ref, outside_rth=outside_rth, exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "STP_LMT":
                order = await self.broker.place_stop_limit(
                    symbol,
                    side,
                    qty,
                    stop_px,
                    limit_px,
                    tif,
                    order_ref,
                    outside_rth=outside_rth,
                    exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "TRAIL":
                order = await self.broker.place_trailing_stop(
                    symbol,
                    side,
                    qty,
                    tif,
                    order_ref,
                    trail_amount=trail_amount,
                    trail_percent=trail_percent,
                    trail_stop_px=trail_stop_px,
                    outside_rth=outside_rth,
                    exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "TRAIL_LIMIT":
                order = await self.broker.place_trailing_stop_limit(
                    symbol,
                    side,
                    qty,
                    tif,
                    order_ref,
                    trail_amount=trail_amount,
                    trail_percent=trail_percent,
                    trail_stop_px=trail_stop_px,
                    lmt_price_offset=lmt_price_offset,
                    outside_rth=outside_rth,
                    exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            else:
                logger.error("Unknown order type", order_type=order_type)
                return None

            # Create plan
            async with self._lock:
                resolved_account_id = str(account_id or self._resolved_account_id() or "UNKNOWN_ACCOUNT").strip() or "UNKNOWN_ACCOUNT"
                resolved_strategy_instance_id = str(strategy_instance_id or strategy_id or "").strip()
                resolved_lot_id = str(
                    lot_id
                    or (oco_protection or {}).get("position_lot_id")
                    or self.default_lot_id(
                        account_id=resolved_account_id,
                        strategy_instance_id=resolved_strategy_instance_id,
                        symbol=symbol,
                    )
                ).strip()
                plan = Plan(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    account_id=resolved_account_id,
                    strategy_instance_id=resolved_strategy_instance_id,
                    lot_id=resolved_lot_id,
                    entry_order_id=order.order_id,
                    entry_qty=qty,
                    attach_oco=bool(attach_oco),
                    oco_config=dict(oco_protection or {}),
                )
                plan_key = self._plan_key_for_plan(plan)
                plan.entry_sent_ts_us = now_us()
                if entry_timeout_sec is not None:
                    try:
                        plan.entry_timeout_sec = float(entry_timeout_sec)
                    except Exception:
                        plan.entry_timeout_sec = None
                self.plans[plan_key] = plan
                self.orders[order.order_id] = order

            metrics['kern_place'].labels(kind='entry').inc()
            logger.info("Entry order placed", order_id=order.order_id, symbol=symbol, side=side, qty=qty)

            return order

        except Exception as e:
            metrics['kern_err'].labels(op='place_entry').inc()
            logger.error("Failed to place entry order", error=str(e), symbol=symbol)
            return None

    async def place_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_px: Optional[float] = None,
        stop_px: Optional[float] = None,
        trail_amount: Optional[float] = None,
        trail_percent: Optional[float] = None,
        trail_stop_px: Optional[float] = None,
        lmt_price_offset: Optional[float] = None,
        tif: str = "DAY",
        order_ref: Optional[str] = None,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        decision_ttl_sec: float = 5.0,
        expected_epoch: Optional[int] = None,
        close_only: bool = False,
        reduce_only: bool = True,
        position_qty_hint: Optional[float] = None,
        previous_stop_px: Optional[float] = None,
        manual_override: bool = False,
        emergency: bool = False,
    ) -> Optional[PlacedOrder]:
        """
        Place a non-entry order (e.g., flatten / protective_stop) without creating a Plan.

        This prevents overwriting the entry Plan keyed by strategy_id:symbol.
        """
        start_wait_ts = time.time()
        try:
            order_type = str(order_type or "").strip().upper().replace(" ", "_")
            await self.place_limiter.wait()

            if (time.time() - start_wait_ts) > decision_ttl_sec:
                logger.error(
                    "Order expired in memory queue",
                    symbol=symbol,
                    waited_sec=round(time.time() - start_wait_ts, 2),
                )
                return None

            if not order_ref:
                order_ref = f"aux:{symbol}:{uuid.uuid4().hex[:8]}"
            if order_type in {"STP", "STP_LMT", "TRAIL", "TRAIL_LIMIT"} and (close_only or reduce_only):
                if position_qty_hint is not None:
                    pos_qty = float(position_qty_hint)
                    protection_side = "LONG" if pos_qty > 0 else "SHORT"
                    new_stop_px = trail_stop_px if order_type in {"TRAIL", "TRAIL_LIMIT"} else stop_px
                    ProtectionEngine.validate_replace(
                        side=protection_side,
                        action=side,
                        qty=float(qty),
                        position_qty=pos_qty,
                        old_stop_px=(float(previous_stop_px) if previous_stop_px is not None else None),
                        new_stop_px=(float(new_stop_px) if new_stop_px is not None else None),
                        manual_override=manual_override,
                        emergency=emergency,
                    )

            if order_type == "MKT":
                order = await self.broker.place_market(
                    symbol, side, qty, tif, order_ref, outside_rth=outside_rth, exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "LMT":
                order = await self.broker.place_limit(
                    symbol, side, qty, limit_px, tif, order_ref, outside_rth=outside_rth, exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "STP":
                order = await self.broker.place_stop(
                    symbol, side, qty, stop_px, tif, order_ref, outside_rth=outside_rth, exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "STP_LMT":
                order = await self.broker.place_stop_limit(
                    symbol,
                    side,
                    qty,
                    stop_px,
                    limit_px,
                    tif,
                    order_ref,
                    outside_rth=outside_rth,
                    exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "TRAIL":
                order = await self.broker.place_trailing_stop(
                    symbol,
                    side,
                    qty,
                    tif,
                    order_ref,
                    trail_amount=trail_amount,
                    trail_percent=trail_percent,
                    trail_stop_px=trail_stop_px,
                    outside_rth=outside_rth,
                    exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            elif order_type == "TRAIL_LIMIT":
                order = await self.broker.place_trailing_stop_limit(
                    symbol,
                    side,
                    qty,
                    tif,
                    order_ref,
                    trail_amount=trail_amount,
                    trail_percent=trail_percent,
                    trail_stop_px=trail_stop_px,
                    lmt_price_offset=lmt_price_offset,
                    outside_rth=outside_rth,
                    exchange_override=exchange_override,
                    expected_epoch=expected_epoch,
                )
            else:
                logger.error("Unknown order type", order_type=order_type)
                return None

            async with self._lock:
                self.orders[order.order_id] = order

            metrics['kern_place'].labels(kind='aux').inc()
            logger.info("Aux order placed", order_id=order.order_id, symbol=symbol, side=side, qty=qty, order_type=order_type)
            return order

        except Exception as e:
            metrics['kern_err'].labels(op='place_aux').inc()
            logger.error("Failed to place aux order", error=str(e), symbol=symbol)
            return None

    async def replace_protective_order_atomicish(
        self,
        *,
        old_order_id: Optional[int],
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_px: Optional[float] = None,
        stop_px: Optional[float] = None,
        trail_amount: Optional[float] = None,
        trail_percent: Optional[float] = None,
        trail_stop_px: Optional[float] = None,
        lmt_price_offset: Optional[float] = None,
        tif: str = "DAY",
        order_ref: Optional[str] = None,
        outside_rth: Optional[bool] = None,
        exchange_override: Optional[str] = None,
        decision_ttl_sec: float = 5.0,
        expected_epoch: Optional[int] = None,
        close_only: bool = True,
        reduce_only: bool = True,
        position_qty_hint: Optional[float] = None,
        previous_stop_px: Optional[float] = None,
        manual_override: bool = False,
        emergency: bool = False,
    ) -> Optional[PlacedOrder]:
        """Replace protective order by placing the new one before cancelling the old one."""
        start_wait_ts = time.time()
        try:
            order_type = str(order_type or "").strip().upper().replace(" ", "_")
            if not order_ref:
                order_ref = f"protect_replace:{symbol}:{uuid.uuid4().hex[:8]}"
            if close_only or reduce_only:
                if position_qty_hint is not None:
                    pos_qty = float(position_qty_hint)
                    protection_side = "LONG" if pos_qty > 0 else "SHORT"
                    new_stop_px = trail_stop_px if order_type in {"TRAIL", "TRAIL_LIMIT"} else stop_px
                    ProtectionEngine.validate_replace(
                        side=protection_side,
                        action=side,
                        qty=float(qty),
                        position_qty=pos_qty,
                        old_stop_px=(float(previous_stop_px) if previous_stop_px is not None else None),
                        new_stop_px=(float(new_stop_px) if new_stop_px is not None else None),
                        manual_override=manual_override,
                        emergency=emergency,
                    )
            if order_type == "STP" and old_order_id is not None:
                modify_stop_fn = getattr(self.broker, "modify_stop_order", None)
                if callable(modify_stop_fn):
                    await self.modify_limiter.wait()
                    if (time.time() - start_wait_ts) > decision_ttl_sec:
                        logger.error("Protective modification expired in memory queue", symbol=symbol, waited_sec=round(time.time() - start_wait_ts, 2))
                        return None
                    ok = await modify_stop_fn(
                        int(old_order_id),
                        stop_px=stop_px,
                        qty=qty,
                        expected_epoch=expected_epoch,
                    )
                    if not ok:
                        logger.error("Broker stop modification failed", symbol=symbol, old_order_id=old_order_id)
                        return None
                    async with self._lock:
                        order = self.orders.get(int(old_order_id))
                        if order is not None:
                            order.stop_px = stop_px
                            order.qty = qty
                            order.order_ref = order_ref
                            order.tif = tif
                        else:
                            order = PlacedOrder(
                                order_id=int(old_order_id),
                                order_ref=order_ref,
                                symbol=symbol,
                                side=side,
                                order_type="STP",
                                qty=qty,
                                stop_px=stop_px,
                                tif=tif,
                                status="SUBMITTED",
                            )
                            self.orders[int(old_order_id)] = order
                    metrics['kern_modify'].inc()
                    logger.info("Protective stop modified", order_id=old_order_id, symbol=symbol, stop_px=stop_px, qty=qty)
                    return order
            await self.place_limiter.wait()
            if (time.time() - start_wait_ts) > decision_ttl_sec:
                logger.error("Protective replacement expired in memory queue", symbol=symbol, waited_sec=round(time.time() - start_wait_ts, 2))
                return None
            replace_fn = getattr(self.broker, "replace_protective_order_atomicish", None)
            if not callable(replace_fn):
                logger.error("Broker does not support protective replacement", symbol=symbol, order_type=order_type)
                return None
            order = await replace_fn(
                old_order_id=(int(old_order_id) if old_order_id is not None else None),
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=order_type,
                limit_px=limit_px,
                stop_px=stop_px,
                trail_amount=trail_amount,
                trail_percent=trail_percent,
                trail_stop_px=trail_stop_px,
                lmt_price_offset=lmt_price_offset,
                tif=tif,
                order_ref=order_ref,
                outside_rth=outside_rth,
                exchange_override=exchange_override,
                expected_epoch=expected_epoch,
            )
            if order is None:
                logger.error("Broker protective replacement returned no order", symbol=symbol, old_order_id=old_order_id, order_type=order_type)
                return None
            async with self._lock:
                self.orders[order.order_id] = order
                if old_order_id is not None:
                    self.orders.pop(int(old_order_id), None)
            metrics['kern_place'].labels(kind='protect_replace').inc()
            logger.info("Protective order replaced", old_order_id=old_order_id, new_order_id=order.order_id, symbol=symbol, side=side, order_type=order_type, qty=qty)
            return order
        except Exception as e:
            metrics['kern_err'].labels(op='replace_protective').inc()
            logger.error("Failed to replace protective order", error=str(e), symbol=symbol)
            return None

    async def on_fill(self, fill: Fill):
        """
        Handle order fill

        Args:
            fill: Fill object
        """
        resolved_order = await self._resolve_order_row_for_fill(fill)
        if resolved_order is None:
            logger.warning(
                "Cannot resolve order row for fill",
                broker_order_id=fill.order_id,
                order_ref=fill.order_ref,
                exec_id=fill.exec_id,
                perm_id=fill.perm_id,
            )
            db_filled_qty = float(fill.qty or 0.0)
            order_uuid = None
        else:
            order_uuid = resolved_order["order_id"]
            if self._requires_known_account_for_fill_mutation() and self._resolved_account_id() == "UNKNOWN_ACCOUNT":
                logger.warning(
                    "fill_mutation_blocked_unknown_account",
                    broker_order_id=fill.order_id,
                    order_ref=fill.order_ref,
                    exec_id=fill.exec_id,
                    perm_id=fill.perm_id,
                    core_order_id=str(order_uuid or ""),
                    strategy_id=str(resolved_order.get("strategy_id") or ""),
                    symbol=str(resolved_order.get("symbol") or ""),
                )
                resolved_order = None
                order_uuid = None
                db_filled_qty = float(fill.qty or 0.0)
            else:
                # Always persist fills and update order state, even if the order is not tied to an in-memory Plan.
                inserted = await self._persist_fill(fill, order_uuid=order_uuid)
                if inserted:
                    try:
                        await self._apply_fill_to_core_position(resolved_order=resolved_order, fill=fill)
                    except Exception as exc:
                        logger.warning(
                            "position_update_from_fill_failed_non_blocking",
                            strategy_id=str(resolved_order.get("strategy_id") or ""),
                            symbol=str(resolved_order.get("symbol") or ""),
                            broker_order_id=fill.order_id,
                            exec_id=fill.exec_id,
                            error=str(exc),
                        )
                db_filled_qty = await self._get_filled_qty(order_uuid)

        order_qty: Optional[float] = None
        async with self._lock:
            order = self.orders.get(fill.order_id)
            if order is not None and order.qty is not None:
                order_qty = float(abs(order.qty))

        if order_qty is None and resolved_order is not None:
            try:
                qty_db = resolved_order.get("qty")
                if qty_db is not None:
                    order_qty = float(abs(qty_db))
            except Exception:
                order_qty = None

        # Determine state using DB filled qty; tolerate missing order_qty.
        if order_qty is not None and order_qty > 0:
            new_state = "FILLED" if db_filled_qty >= (order_qty - 1e-9) else "PARTIAL"
        else:
            new_state = "FILLED" if db_filled_qty > 0 else "PARTIAL"

        if order_uuid is not None:
            await self._update_order_state(order_uuid, new_state, db_filled_qty)
        if new_state == "FILLED" and resolved_order is not None:
            decision_id = self._extract_decision_id_from_note(resolved_order.get("note"))
            if not decision_id:
                decision_id = await self._resolve_decision_id_from_events(
                    order_uuid=resolved_order.get("order_id"),
                    broker_order_id=fill.order_id,
                )
            await self._finalize_decision_terminal(
                decision_id=decision_id,
                terminal_state="FILLED",
            )
            await self._release_reservation(
                fill.order_id,
                "FILLED",
                idempotency_key_override=str(resolved_order.get("idempotency_key") or ""),
            )
            try:
                note_data = _note_json_dict(resolved_order.get("note"))
                extras = note_data.get("extras") if isinstance(note_data.get("extras"), dict) else {}
            except Exception:
                extras = {}
            if bool(extras.get("flatten_action") or extras.get("close_only") or extras.get("protective_stop")):
                controller = self.flatten_controller or FlattenController(
                    pg=self.pg,
                    manual_intervention_lock_store=self.manual_intervention_lock_store,
                )
                await controller.resolve_from_position(
                    order_uuid=order_uuid,
                    resolved_order=resolved_order,
                    confirmed_by="safety_kernel_fill",
                    fill_broker_order_id=fill.order_id,
                    unlock_reason=str(extras.get("intent_action") or "close_only_filled"),
                )
        if resolved_order is not None:
            try:
                if (
                    self.netting_fill_fanout is not None
                    and str(resolved_order.get("strategy_id") or "") == "__netting__"
                    and order_uuid is not None
                ):
                    await self.netting_fill_fanout.on_parent_fill(
                        parent_order_uuid=order_uuid,
                        parent_fill=fill,
                        parent_filled_qty=float(db_filled_qty),
                    )
            except Exception as exc:
                logger.warning(
                    "netting_fill_fanout_failed",
                    parent_order_id=str(order_uuid),
                    broker_order_id=fill.order_id,
                    error=str(exc),
                )

        # Best-effort Plan updates + OCO enforcement only apply to entry fills.
        async with self._lock:
            plan = None
            for p in self.plans.values():
                if p.entry_order_id == fill.order_id:
                    plan = p
                    break
            if plan is None or order is None:
                return

            plan.position.on_fill(order.side, fill.qty, fill.price)
            plan.filled_qty += fill.qty

            if plan.avg_fill_px == 0:
                plan.avg_fill_px = fill.price
            else:
                total_filled = plan.filled_qty
                plan.avg_fill_px = ((plan.avg_fill_px * (total_filled - fill.qty)) + (fill.price * fill.qty)) / total_filled

            if plan.first_fill_ts_us is None:
                plan.first_fill_ts_us = fill.ts_us
                logger.info("First fill received", order_id=fill.order_id, symbol=plan.symbol)
                if plan.attach_oco:
                    asyncio.create_task(self._attach_oco_protection(plan))
            elif plan.attach_oco and plan.oco_pair is not None:
                await self._sync_oco_child_quantities(plan)

    async def _attach_oco_protection(self, plan: Plan):
        """
        Attach OCO (stop loss + take profit) protection to plan

        Args:
            plan: Plan to protect
        """
        try:
            start_ts = time.time()
            await self._coalesce_entry_fills_before_oco(plan)

            entry_px = plan.avg_fill_px
            oco_cfg = dict(plan.oco_config or {})
            protection_qty = await self._owner_lane_protection_qty(
                plan,
                candidate_qty=self._effective_protection_qty(plan),
            )
            if protection_qty <= 0.0:
                logger.error("OCO attachment skipped because filled quantity is still zero", symbol=plan.symbol)
                return

            sl_px = oco_cfg.get("sl_px")
            tp_px = oco_cfg.get("tp_px")

            if sl_px is None:
                sl_offset_abs = oco_cfg.get("sl_offset_abs")
                if sl_offset_abs is not None:
                    try:
                        sl_offset = abs(float(sl_offset_abs))
                    except Exception:
                        sl_offset = 0.0
                    if sl_offset > 0:
                        sl_px = entry_px - sl_offset if plan.position.qty > 0 else entry_px + sl_offset

            if tp_px is None:
                tp_offset_abs = oco_cfg.get("tp_offset_abs")
                if tp_offset_abs is not None:
                    try:
                        tp_offset = abs(float(tp_offset_abs))
                    except Exception:
                        tp_offset = 0.0
                    if tp_offset > 0:
                        tp_px = entry_px + tp_offset if plan.position.qty > 0 else entry_px - tp_offset

            if sl_px is None:
                sl_bp = oco_cfg.get("hard_stop_bp")
                try:
                    sl_bp_f = float(sl_bp) if sl_bp is not None else 0.0
                except Exception:
                    sl_bp_f = 0.0
                if sl_bp_f > 0.0:
                    sl_ratio = sl_bp_f / 1e4
                    sl_px = entry_px * (1 - sl_ratio) if plan.position.qty > 0 else entry_px * (1 + sl_ratio)

            if tp_px is None:
                tp_bp = oco_cfg.get("take_profit_bp")
                try:
                    tp_bp_f = float(tp_bp) if tp_bp is not None else 0.0
                except Exception:
                    tp_bp_f = 0.0
                if tp_bp_f > 0.0:
                    tp_ratio = tp_bp_f / 1e4
                    tp_px = entry_px * (1 + tp_ratio) if plan.position.qty > 0 else entry_px * (1 - tp_ratio)

            # Last-resort fallback for older callers that only toggle attach_oco.
            if sl_px is None:
                sl_px = entry_px * 0.98 if plan.position.qty > 0 else entry_px * 1.02
            if tp_px is None:
                tp_px = entry_px * 1.03 if plan.position.qty > 0 else entry_px * 0.97

            ok, reason = self._validate_oco_price_sanity(
                plan=plan,
                entry_px=entry_px,
                sl_px=sl_px,
                tp_px=tp_px,
                oco_cfg=oco_cfg,
            )
            if not ok:
                plan.status = "protection_failed"
                plan.protection_failure_ts_us = now_us()
                plan.protection_failure_reason = reason
                logger.error(
                    "oco_protection_price_sanity_failed",
                    strategy_id=plan.strategy_id,
                    symbol=plan.symbol,
                    reason=reason,
                    entry_px=entry_px,
                    sl_px=sl_px,
                    tp_px=tp_px,
                )
                await self._set_protection_failure_lock(plan)
                return

            # Generate OCO group
            oca_group = f"OCO_{uuid.uuid4().hex[:8]}"

            sl_side = side_flip(self.orders[plan.entry_order_id].side)
            tif = str(oco_cfg.get("tif") or "DAY").strip() or "DAY"
            outside_rth = oco_cfg.get("outside_rth")
            if outside_rth is not None:
                outside_rth = bool(outside_rth)
            exchange_override = str(oco_cfg.get("exchange_override") or "").strip() or None
            raw_sl_order_type = str(oco_cfg.get("sl_order_type") or "STP").strip().upper()
            sl_order_type = "STP_LMT" if raw_sl_order_type in {"STP_LMT", "STOP_LIMIT"} else "STP"
            sl_order: Optional[PlacedOrder] = None
            tp_order: Optional[PlacedOrder] = None
            raw_expected_epoch = self._current_ib_order_identity().get("ib_connection_epoch")
            expected_epoch = int(raw_expected_epoch) if raw_expected_epoch is not None else None
            account_id = self._resolved_account_id()
            if account_id == "UNKNOWN_ACCOUNT":
                plan.status = "protection_failed"
                plan.protection_failure_ts_us = now_us()
                plan.protection_failure_reason = "account_id_unresolved"
                logger.error(
                    "oco_protection_account_id_unresolved",
                    strategy_id=plan.strategy_id,
                    symbol=plan.symbol,
                )
                await self._set_protection_failure_lock(plan)
                return
            lot_id = str(oco_cfg.get("position_lot_id") or plan.lot_id).strip()
            protection_band_width = oco_cfg.get("protection_band_width") or oco_cfg.get("price_band_width")
            sl_protection_key = ""
            tp_protection_key = ""

            if sl_px is not None:
                sl_meta = build_protection_metadata(
                    account_id=account_id,
                    strategy_id=plan.strategy_id,
                    symbol=plan.symbol,
                    lot_id=lot_id,
                    protection_kind="stop_loss",
                    side=sl_side,
                    qty=protection_qty,
                    price=sl_px,
                    tick_size=oco_cfg.get("tick_size") or 0.01,
                    configured_band_width=protection_band_width,
                )
                sl_protection_key = str(sl_meta["protection_key"])
                sl_ref = f"sl:{sl_protection_key}"[:120]
                sl_order = await self._find_existing_open_protection_order(sl_protection_key)
                if sl_order is None:
                    sl_wait_start = time.time()
                    await self.place_limiter.wait()
                    if (time.time() - sl_wait_start) > 10.0:
                        logger.error("OCO SL placement expired in memory", symbol=plan.symbol)
                        return
                    if sl_order_type == "STP_LMT":
                        sl_limit_px = oco_cfg.get("sl_limit_px")
                        if sl_limit_px is None:
                            try:
                                sl_limit_offset_abs = abs(float(oco_cfg.get("sl_limit_offset_abs") or 0.0))
                            except Exception:
                                sl_limit_offset_abs = 0.0
                            if sl_limit_offset_abs <= 0.0:
                                sl_limit_offset_abs = max(0.02, min(0.25, abs(float(entry_px) - float(sl_px)) * 0.5))
                            sl_limit_px = float(sl_px) - sl_limit_offset_abs if plan.position.qty > 0 else float(sl_px) + sl_limit_offset_abs
                        sl_order = await self.broker.place_stop_limit(
                            plan.symbol,
                            sl_side,
                            protection_qty,
                            float(sl_px),
                            float(sl_limit_px),
                            tif,
                            sl_ref,
                            oca_group,
                            outside_rth=outside_rth,
                            exchange_override=exchange_override,
                            expected_epoch=expected_epoch,
                        )
                    else:
                        sl_order = await self.broker.place_stop(
                            plan.symbol,
                            sl_side,
                            protection_qty,
                            float(sl_px),
                            tif,
                            sl_ref,
                            oca_group,
                            outside_rth=outside_rth,
                            exchange_override=exchange_override,
                            expected_epoch=expected_epoch,
                        )
                else:
                    logger.info(
                        "oco_stop_loss_deduped",
                        strategy_id=plan.strategy_id,
                        symbol=plan.symbol,
                        protection_key=sl_protection_key,
                        broker_order_id=sl_order.order_id,
                    )

            if tp_px is not None:
                tp_meta = build_protection_metadata(
                    account_id=account_id,
                    strategy_id=plan.strategy_id,
                    symbol=plan.symbol,
                    lot_id=lot_id,
                    protection_kind="take_profit",
                    side=sl_side,
                    qty=protection_qty,
                    price=tp_px,
                    tick_size=oco_cfg.get("tick_size") or 0.01,
                    configured_band_width=protection_band_width,
                )
                tp_protection_key = str(tp_meta["protection_key"])
                tp_ref = f"tp:{tp_protection_key}"[:120]
                tp_order = await self._find_existing_open_protection_order(tp_protection_key)
                if tp_order is None:
                    tp_wait_start = time.time()
                    await self.place_limiter.wait()
                    if (time.time() - tp_wait_start) > 10.0:
                        logger.error("OCO TP placement expired in memory", symbol=plan.symbol)
                        return
                    tp_order = await self.broker.place_limit(
                        plan.symbol,
                        sl_side,
                        protection_qty,
                        float(tp_px),
                        tif,
                        tp_ref,
                        oca_group,
                        outside_rth=outside_rth,
                        exchange_override=exchange_override,
                        expected_epoch=expected_epoch,
                    )
                else:
                    logger.info(
                        "oco_take_profit_deduped",
                        strategy_id=plan.strategy_id,
                        symbol=plan.symbol,
                        protection_key=tp_protection_key,
                        broker_order_id=tp_order.order_id,
                    )
            oco_cfg["stop_loss_protection_key"] = sl_protection_key
            oco_cfg["take_profit_protection_key"] = tp_protection_key
            plan.oco_config = oco_cfg

            if sl_order is None and tp_order is None:
                logger.error("OCO attachment skipped because no valid protection levels were resolved", symbol=plan.symbol)
                return

            # Record OCO pair
            oco_pair = OCOPair(
                parent_order_id=plan.entry_order_id,
                stop_loss_order_id=sl_order.order_id if sl_order is not None else None,
                take_profit_order_id=tp_order.order_id if tp_order is not None else None,
                oca_group=oca_group,
                status="ACTIVE"
            )

            async with self._lock:
                plan.oco_pair = oco_pair
                plan.oco_attached_ts_us = now_us()
                plan.status = "PROTECTED"
                self.oco_pairs[oca_group] = oco_pair
                if sl_order is not None:
                    self.orders[sl_order.order_id] = sl_order
                if tp_order is not None:
                    self.orders[tp_order.order_id] = tp_order

            # Persist OCO orders to database
            await self._persist_oco_orders(
                parent_order_id=plan.entry_order_id,
                sl_order=sl_order,
                tp_order=tp_order,
                oca_group=oca_group,
                strategy_id=plan.strategy_id,
                account_id=plan.account_id,
                strategy_instance_id=plan.strategy_instance_id,
                bracket_id=oca_group,
                sl_protection_key=sl_protection_key,
                tp_protection_key=tp_protection_key,
            )
            await self._sync_oco_child_quantities(plan)

            # Calculate and record protection window
            window_ms = (time.time() - start_ts) * 1000
            metrics['kern_unprotected_win'].observe(window_ms)
            metrics['kern_oco_attach'].inc()

            logger.info("OCO protection attached",
                        symbol=plan.symbol,
                        sl_order=sl_order.order_id if sl_order is not None else None,
                        tp_order=tp_order.order_id if tp_order is not None else None,
                        window_ms=window_ms)

        except Exception as e:
            should_lock = False
            async with self._lock:
                if plan.status != "PROTECTED":
                    plan.status = "PROTECTION_FAILED"
                    plan.protection_failure_ts_us = now_us()
                    plan.protection_failure_reason = str(e)
                    should_lock = plan.protection_lock_set_ts_us is None
            if should_lock:
                await self._set_protection_failure_lock(plan)
            metrics['kern_err'].labels(op='attach_oco').inc()
            logger.error("Failed to attach OCO", error=str(e), symbol=plan.symbol)

    async def _coalesce_entry_fills_before_oco(self, plan: Plan) -> None:
        """
        Wait briefly for follow-on fills before sizing child protection orders.

        IB often emits multiple fills for the same parent within a few milliseconds.
        Without a short coalesce window, the first child orders can be placed at the
        first partial-fill size and then immediately hit the "PendingSubmit cannot
        modify" window while the parent finishes filling.
        """
        wait_sec = float(self.oco_fill_coalesce_sec or 0.0)
        target_qty = float(abs(plan.entry_qty or 0.0))
        if wait_sec <= 0.0 or target_qty <= 0.0:
            return

        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if self._effective_protection_qty(plan) >= (target_qty - 1e-9):
                return
            await asyncio.sleep(0.01)

    async def _find_existing_open_protection_order(self, protection_key: str) -> Optional[PlacedOrder]:
        if not self.pg or not str(protection_key or "").strip():
            return None
        fetchone = getattr(self.pg, "fetchone", None)
        if fetchone is None:
            return None
        row = fetchone(
            """
            SELECT broker_order_id, order_ref, symbol, side, order_type, qty, limit_px, stop_px, tif, ib_perm_id
            FROM core.core_order
            WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL')
              AND note IS NOT NULL
              AND (note::jsonb ->> 'protection_key') = $1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            str(protection_key),
        )
        if inspect.isawaitable(row):
            row = await row
        if not row:
            return None
        if not isinstance(row, dict):
            try:
                row = dict(row)
            except Exception:
                return None
        try:
            broker_order_id = int(row.get("broker_order_id"))
        except Exception:
            return None
        limit_px = row.get("limit_px")
        stop_px = row.get("stop_px")
        perm_id = row.get("ib_perm_id")
        return PlacedOrder(
            order_id=broker_order_id,
            order_ref=str(row.get("order_ref") or f"protection:{protection_key}")[:120],
            symbol=str(row.get("symbol") or ""),
            side=str(row.get("side") or ""),
            order_type=str(row.get("order_type") or ""),
            qty=float(row.get("qty") or 0.0),
            limit_px=(float(limit_px) if limit_px is not None else None),
            stop_px=(float(stop_px) if stop_px is not None else None),
            tif=str(row.get("tif") or "DAY"),
            perm_id=(int(perm_id) if perm_id is not None else None),
        )

    @staticmethod
    def _effective_protection_qty(plan: Plan) -> float:
        return max(
            float(abs(plan.position.qty or 0.0)),
            float(abs(plan.filled_qty or 0.0)),
        )

    async def _owner_lane_protection_qty(self, plan: Plan, *, candidate_qty: float) -> float:
        """Clamp protection sizing to the durable owner lane when available."""
        try:
            candidate = abs(float(candidate_qty or 0.0))
        except Exception:
            candidate = 0.0
        if candidate <= 0.0 or self.pg is None:
            return candidate
        account_id = str(plan.account_id or self._resolved_account_id() or "").strip()
        strategy_instance_id = str(plan.strategy_instance_id or plan.strategy_id or "").strip()
        symbol = str(plan.symbol or "").strip().upper()
        lot_id = str(plan.lot_id or (plan.oco_config or {}).get("position_lot_id") or "").strip()
        if not strategy_instance_id or not symbol or not lot_id:
            return candidate
        fetchone = getattr(self.pg, "fetchone", None)
        if fetchone is None:
            return candidate
        try:
            row = fetchone(
                """
                SELECT ABS(COALESCE(qty, 0))::float8 AS owner_lane_qty
                FROM core.position_ledger_current
                WHERE COALESCE(NULLIF(strategy_instance_id, ''), strategy_id) = $1
                  AND symbol = $2
                  AND lot_id = $3
                  AND ($4::text = '' OR account_id = $4::text)
                LIMIT 1
                """,
                strategy_instance_id,
                symbol,
                lot_id,
                account_id if account_id != "UNKNOWN_ACCOUNT" else "",
            )
            if inspect.isawaitable(row):
                row = await row
            payload = dict(row or {})
            lane_qty = abs(float(payload.get("owner_lane_qty") or 0.0))
        except Exception as exc:
            logger.warning(
                "oco_owner_lane_qty_lookup_failed",
                strategy_id=plan.strategy_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                lot_id=lot_id,
                error=str(exc),
            )
            return candidate
        if lane_qty <= 0.0:
            return candidate
        return min(candidate, lane_qty)

    def _is_stale_local_protective_pending(self, order: Optional[PlacedOrder]) -> bool:
        if order is None:
            return False
        status = str(getattr(order, "status", "") or "").strip()
        if status not in {"PendingSubmit", "ApiPending"}:
            return False
        try:
            perm_id = int(getattr(order, "perm_id", None) or 0)
        except Exception:
            perm_id = 0
        if perm_id > 0:
            return False
        ts_us = int(getattr(order, "ts_us", 0) or 0)
        if ts_us <= 0:
            return False
        age_sec = max(0.0, (now_us() - ts_us) / 1_000_000.0)
        return age_sec > float(self.protection_pending_grace_sec or 0.0)

    async def _mark_orders_cancelled_as_stale_pending(self, *, broker_order_ids: List[int], strategy_id: str, symbol: str) -> None:
        if not self.pg or not broker_order_ids:
            return
        try:
            await self.pg.execute(
                """
                UPDATE core.core_order
                SET state = 'CANCELLED',
                    updated_at = now()
                WHERE broker_order_id = ANY($1::text[])
                  AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                """,
                [str(order_id) for order_id in broker_order_ids],
            )
        except Exception as exc:
            logger.warning(
                "stale_protection_db_cancel_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                broker_order_ids=[int(order_id) for order_id in broker_order_ids],
                error=str(exc),
            )

    async def _sync_oco_child_quantities(self, plan: Plan) -> None:
        """Align child protection order quantities to the latest filled position size."""
        oco_pair = plan.oco_pair
        if oco_pair is None:
            return

        target_qty = await self._owner_lane_protection_qty(
            plan,
            candidate_qty=self._effective_protection_qty(plan),
        )
        if target_qty <= 0.0:
            return

        child_ids = [
            int(order_id)
            for order_id in (
                oco_pair.stop_loss_order_id,
                oco_pair.take_profit_order_id,
            )
            if order_id
        ]
        if not child_ids:
            return

        for child_order_id in child_ids:
            child = self.orders.get(child_order_id)
            if child is None:
                continue

            current_qty = float(abs(child.qty or 0.0))
            if abs(current_qty - target_qty) <= 1e-9:
                continue

            await self.modify_limiter.wait()
            raw_expected_epoch = self._current_ib_order_identity().get("ib_connection_epoch")
            expected_epoch = int(raw_expected_epoch) if raw_expected_epoch is not None else None
            if expected_epoch is not None and expected_epoch > 0:
                updated = await self.broker.modify(child_order_id, qty=target_qty, expected_epoch=expected_epoch)
            else:
                updated = await self.broker.modify(child_order_id, qty=target_qty)
            if not updated:
                child_status = str(getattr(child, "status", "") or "")
                deferred = child_status in {"PendingSubmit", "ApiPending"}
                log_fn = logger.info if deferred else logger.warning
                log_fn(
                    "oco_child_qty_sync_deferred" if deferred else "oco_child_qty_sync_failed",
                    strategy_id=plan.strategy_id,
                    symbol=plan.symbol,
                    child_order_id=child_order_id,
                    current_qty=current_qty,
                    target_qty=target_qty,
                    status=child_status,
                )
                continue

            child.qty = target_qty

            if self.pg:
                try:
                    await self.pg.execute(
                        """
                        UPDATE core.core_order
                        SET qty = $1,
                            updated_at = now()
                        WHERE broker_order_id = $2
                          AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                        """,
                        target_qty,
                        str(child_order_id),
                    )
                except Exception as exc:
                    logger.warning(
                        "oco_child_qty_db_sync_failed",
                        strategy_id=plan.strategy_id,
                        symbol=plan.symbol,
                        child_order_id=child_order_id,
                        target_qty=target_qty,
                        error=str(exc),
                    )

            metrics['kern_modify'].inc()
            logger.info(
                "OCO child quantity synchronized",
                strategy_id=plan.strategy_id,
                symbol=plan.symbol,
                child_order_id=child_order_id,
                from_qty=current_qty,
                to_qty=target_qty,
            )

    async def _inspection_loop(self):
        """Background loop to inspect and enforce invariants"""
        while self._running:
            start = time.time()

            try:
                qty_mismatch_plans: list[Plan] = []
                stale_protection_rearms: list[tuple[Plan, list[int]]] = []
                protection_failure_lock_plans: list[Plan] = []
                async with self._lock:
                    # Count unprotected plans
                    unprotected = 0
                    timed_out_entries: list[tuple[str, int]] = []
                    for plan in self.plans.values():
                        # Optional: entry timeout cancel (used by fast options MKT entries).
                        if (
                            plan.entry_order_id
                            and (plan.filled_qty or 0.0) <= 0.0
                            and plan.entry_sent_ts_us
                            and plan.entry_timeout_sec
                            and float(plan.entry_timeout_sec) > 0
                            and plan.status in {"ACTIVE"}
                        ):
                            elapsed_sec = (now_us() - int(plan.entry_sent_ts_us)) / 1e6
                            if elapsed_sec >= float(plan.entry_timeout_sec):
                                timed_out_entries.append((plan.symbol, int(plan.entry_order_id)))
                                # Disable repeated timeout cancels for the same plan.
                                plan.entry_timeout_sec = None

                        if (not plan.attach_oco):
                            continue
                        if plan.filled_qty > 0 and plan.status not in {"PROTECTED", "PROTECTION_FAILED"}:
                            unprotected += 1

                            # Check if timeout exceeded
                            if plan.first_fill_ts_us:
                                elapsed_sec = (now_us() - plan.first_fill_ts_us) / 1e6
                                if elapsed_sec > self.oco_timeout:
                                    logger.error("OCO attachment timeout exceeded",
                                               symbol=plan.symbol,
                                               elapsed_sec=elapsed_sec)

                        if plan.attach_oco and plan.oco_pair is not None and abs(float(plan.position.qty or 0.0)) > 0.0:
                            target_qty = float(abs(plan.position.qty or 0.0))
                            child_ids = [
                                int(order_id)
                                for order_id in (
                                    plan.oco_pair.stop_loss_order_id,
                                    plan.oco_pair.take_profit_order_id,
                                )
                                if order_id
                            ]
                            stale_child_ids: list[int] = []
                            for child_order_id in child_ids:
                                child = self.orders.get(child_order_id)
                                if child is None:
                                    continue
                                if self._is_stale_local_protective_pending(child):
                                    stale_child_ids.append(int(child_order_id))
                                    continue
                                if abs(float(abs(child.qty or 0.0)) - target_qty) > 1e-9:
                                    qty_mismatch_plans.append(plan)
                                    break
                            if stale_child_ids:
                                last_rearm_ts_us = int(plan.last_protection_rearm_ts_us or 0)
                                rearm_age_sec = (
                                    (now_us() - last_rearm_ts_us) / 1e6
                                    if last_rearm_ts_us > 0
                                    else None
                                )
                                if (
                                    plan.protection_rearm_count < int(self.protection_rearm_max_attempts)
                                    and (
                                        rearm_age_sec is None
                                        or rearm_age_sec >= float(self.protection_rearm_cooldown_sec or 0.0)
                                    )
                                ):
                                    for stale_child_id in stale_child_ids:
                                        self.orders.pop(int(stale_child_id), None)
                                    plan.oco_pair = None
                                    plan.status = "ACTIVE"
                                    plan.protection_rearm_count += 1
                                    plan.last_protection_rearm_ts_us = now_us()
                                    stale_protection_rearms.append((plan, stale_child_ids))
                                else:
                                    plan.status = "PROTECTION_FAILED"
                                    plan.protection_failure_ts_us = now_us()
                                    plan.protection_failure_reason = "stale_local_protective_pending"
                                    if plan.protection_lock_set_ts_us is None:
                                        protection_failure_lock_plans.append(plan)
                                    logger.error(
                                        "oco_stale_local_pending_rearm_exhausted",
                                        strategy_id=plan.strategy_id,
                                        symbol=plan.symbol,
                                        stale_child_ids=stale_child_ids,
                                        rearm_count=plan.protection_rearm_count,
                                    )

                    metrics['kern_oco_missing'].set(unprotected)

                # Execute cancels outside the plan lock to avoid blocking other paths.
                for sym, oid in timed_out_entries:
                    try:
                        await self.cancel_limiter.wait()
                        raw_expected_epoch = self._current_ib_order_identity().get("ib_connection_epoch")
                        expected_epoch = int(raw_expected_epoch) if raw_expected_epoch is not None else None
                        await self.broker.cancel(int(oid), expected_epoch=expected_epoch)
                        await self.on_cancel(int(oid), reason="entry_timeout")
                        logger.warning(
                            "entry_timeout_cancelled",
                            symbol=str(sym),
                            order_id=int(oid),
                        )
                    except Exception as exc:
                        logger.warning(
                            "entry_timeout_cancel_failed",
                            symbol=str(sym),
                            order_id=int(oid),
                            error=str(exc),
                        )

                for plan in qty_mismatch_plans:
                    try:
                        await self._sync_oco_child_quantities(plan)
                    except Exception as exc:
                        logger.warning(
                            "oco_child_qty_periodic_sync_failed",
                            strategy_id=plan.strategy_id,
                            symbol=plan.symbol,
                            error=str(exc),
                        )

                for plan, stale_child_ids in stale_protection_rearms:
                    try:
                        await self._mark_orders_cancelled_as_stale_pending(
                            broker_order_ids=stale_child_ids,
                            strategy_id=plan.strategy_id,
                            symbol=plan.symbol,
                        )
                        logger.warning(
                            "oco_stale_local_pending_rearm",
                            strategy_id=plan.strategy_id,
                            symbol=plan.symbol,
                            stale_child_ids=stale_child_ids,
                            rearm_count=plan.protection_rearm_count,
                        )
                        await self._attach_oco_protection(plan)
                    except Exception as exc:
                        plan.status = "PROTECTION_FAILED"
                        plan.protection_failure_ts_us = now_us()
                        plan.protection_failure_reason = str(exc)
                        if plan.protection_lock_set_ts_us is None:
                            protection_failure_lock_plans.append(plan)
                        logger.error(
                            "oco_stale_local_pending_rearm_failed",
                            strategy_id=plan.strategy_id,
                            symbol=plan.symbol,
                            stale_child_ids=stale_child_ids,
                            error=str(exc),
                        )

                for plan in protection_failure_lock_plans:
                    await self._set_protection_failure_lock(plan)

                if self.trail_limit_watchdog_enabled:
                    await self.watch_trail_limit_orders_once()
                await self.reconcile_protective_order_families_once(use_broker_symbol_qty=False)

                latency_ms = (time.time() - start) * 1000
                metrics['kern_inspect_ms'].observe(latency_ms)

            except Exception as e:
                logger.error("Inspection loop error", error=str(e))

            await asyncio.sleep(5.0)  # Run every 5 seconds

    @staticmethod
    def _snapshot_value(snapshot: Any, *names: str) -> Optional[float]:
        for name in names:
            value = snapshot.get(name) if isinstance(snapshot, dict) else getattr(snapshot, name, None)
            try:
                if value is None:
                    continue
                px = float(value)
                if px > 0:
                    return px
            except Exception:
                continue
        return None

    async def _resolve_market_snapshot(self, symbol: str) -> Any | None:
        for name in ("market_snapshot", "quote", "get_quote"):
            fn = getattr(self.broker, name, None)
            if not callable(fn):
                continue
            try:
                result = fn(symbol)
                return await result if inspect.isawaitable(result) else result
            except Exception as exc:
                logger.warning("trail_limit_watchdog_market_snapshot_failed", symbol=symbol, provider=name, error=str(exc))
                return None
        return None

    def _marketable_limit_px_for_trail_fallback(self, *, order: PlacedOrder, snapshot: Any) -> Optional[float]:
        side_u = str(order.side or "").strip().upper()
        if side_u == "SELL":
            bid = self._snapshot_value(snapshot, "bid", "bid_px", "bid_price")
            if bid is None:
                return None
            buffer_abs = max(0.01, bid * self.trail_limit_emergency_slippage_bps / 10_000.0)
            return round(max(0.01, bid - buffer_abs), 2)
        if side_u == "BUY":
            ask = self._snapshot_value(snapshot, "ask", "ask_px", "ask_price")
            if ask is None:
                return None
            buffer_abs = max(0.01, ask * self.trail_limit_emergency_slippage_bps / 10_000.0)
            return round(ask + buffer_abs, 2)
        return None

    async def watch_trail_limit_orders_once(
        self,
        *,
        market_snapshots: Optional[Dict[str, Any]] = None,
        now_ts: Optional[float] = None,
    ) -> int:
        """Escalate triggered, unfilled TRAIL_LIMIT protection to emergency exit."""
        if not self.trail_limit_watchdog_enabled:
            return 0
        now = float(now_ts if now_ts is not None else time.time())
        async with self._lock:
            candidates = [
                order
                for order in self.orders.values()
                if str(order.order_type or "").strip().upper().replace(" ", "_") == "TRAIL_LIMIT"
                and str(order.status or "").strip().upper() not in {"FILLED", "CANCELLED", "CANCELED", "APICANCELLED", "INACTIVE", "REJECTED"}
            ]

        escalated = 0
        for order in candidates:
            side_u = str(order.side or "").strip().upper()
            position_side = "LONG" if side_u == "SELL" else "SHORT" if side_u == "BUY" else ""
            if not position_side:
                continue
            snapshot = (market_snapshots or {}).get(str(order.symbol or "").strip().upper())
            if snapshot is None:
                snapshot = await self._resolve_market_snapshot(order.symbol)
            status_u = str(order.status or "").strip().upper()
            last_px = self._snapshot_value(snapshot, "last", "last_px", "last_price", "mark", "mark_px") if snapshot is not None else None
            triggered = status_u in {"TRIGGERED", "HELD", "PENDINGCANCEL"}
            if last_px is not None:
                triggered = triggered or ProtectionEngine.trail_limit_triggered(
                    side=position_side,
                    last_px=last_px,
                    trail_stop_px=order.trail_stop_px,
                )
            if not triggered:
                self._trail_limit_triggered_ts_by_order_id.pop(int(order.order_id), None)
                continue
            triggered_ts = self._trail_limit_triggered_ts_by_order_id.setdefault(int(order.order_id), now)
            po_state = ProtectiveOrderState(
                position_id=str(order.order_ref or order.order_id),
                symbol=order.symbol,
                order_type="TRAIL_LIMIT",
                action="SELL" if side_u == "SELL" else "BUY",
                qty=Decimal(str(order.qty)),
                ib_order_id=int(order.order_id),
                ib_perm_id=order.perm_id,
                trail_stop_px=order.trail_stop_px,
                limit_offset=order.lmt_price_offset,
                status=order.status,
                order_ref=order.order_ref,
                triggered_ts=triggered_ts,
            )
            if not ProtectionEngine.trail_limit_needs_fallback(
                order=po_state,
                now_ts=now,
                timeout_sec=self.trail_limit_unfilled_timeout_sec,
            ):
                continue

            emergency_fn = getattr(self.broker, "emergency_exit", None)
            if not callable(emergency_fn):
                logger.error("trail_limit_watchdog_emergency_exit_unavailable", symbol=order.symbol, order_id=order.order_id)
                continue

            await self.cancel_limiter.wait()
            raw_expected_epoch = self._current_ib_order_identity().get("ib_connection_epoch")
            expected_epoch = int(raw_expected_epoch) if raw_expected_epoch is not None else None
            try:
                await self.broker.cancel(int(order.order_id), expected_epoch=expected_epoch)
            except Exception as exc:
                logger.warning("trail_limit_watchdog_cancel_failed", symbol=order.symbol, order_id=order.order_id, error=str(exc))
                continue
            async with self._lock:
                cached_order = self.orders.get(int(order.order_id))
                if cached_order is not None:
                    cached_order.status = "CANCELLED"
                self._trail_limit_triggered_ts_by_order_id.pop(int(order.order_id), None)

            limit_px = None
            if self.trail_limit_fallback in {"MARKETABLE_LMT", "LMT", "LIMIT"}:
                if snapshot is None:
                    snapshot = await self._resolve_market_snapshot(order.symbol)
                if snapshot is not None:
                    limit_px = self._marketable_limit_px_for_trail_fallback(order=order, snapshot=snapshot)
                if limit_px is None:
                    logger.warning("trail_limit_watchdog_marketable_limit_unavailable_falling_back_mkt", symbol=order.symbol, order_id=order.order_id)

            new_ref = f"{str(order.order_ref or f'trail_limit:{order.order_id}')}::EMERGENCY"
            try:
                new_order = await emergency_fn(
                    order.symbol,
                    order.side,
                    order.qty,
                    order.tif,
                    new_ref,
                    limit_px=limit_px,
                    expected_epoch=expected_epoch,
                )
            except Exception as exc:
                logger.error(
                    "trail_limit_watchdog_emergency_exit_failed",
                    symbol=order.symbol,
                    order_id=order.order_id,
                    error=str(exc),
                )
                continue
            if new_order is None:
                logger.error("trail_limit_watchdog_emergency_exit_failed", symbol=order.symbol, order_id=order.order_id)
                continue
            async with self._lock:
                self.orders.pop(int(order.order_id), None)
                self.orders[int(new_order.order_id)] = new_order
                self._trail_limit_triggered_ts_by_order_id.pop(int(order.order_id), None)
            escalated += 1
            logger.warning(
                "trail_limit_watchdog_emergency_exit_submitted",
                symbol=order.symbol,
                old_order_id=order.order_id,
                new_order_id=new_order.order_id,
                fallback=self.trail_limit_fallback,
                limit_px=limit_px,
            )
        return escalated

    @staticmethod
    def _active_protective_order(order: PlacedOrder) -> bool:
        order_type = str(order.order_type or "").strip().upper().replace(" ", "_")
        status = str(order.status or "").strip().upper()
        return order_type in {"STP", "STP_LMT", "TRAIL", "TRAIL_LIMIT"} and status not in {
            "FILLED",
            "CANCELLED",
            "CANCELED",
            "APICANCELLED",
            "INACTIVE",
            "REJECTED",
        }

    @staticmethod
    def _protection_family_key(order: PlacedOrder) -> str:
        ref = str(order.order_ref or "").strip()
        if ref.startswith("protect:"):
            ref = ref[len("protect:"):]
        parts = ref.split(":")
        if len(parts) >= 6:
            namespace, strategy_id, symbol, kind, trigger_marker, trigger_id = parts[:6]
            kind_u = str(kind or "").strip().upper()
            if kind_u in {"STOP", "STP", "STP_LMT", "TRAIL", "TRAIL_LIMIT", "PROTECT"}:
                if kind_u == "PROTECT":
                    return f"{namespace}:{strategy_id}:{symbol}:PROTECT:{trigger_marker}:{trigger_id}"
                return f"{namespace}:{strategy_id}:{symbol}:PROTECT:{trigger_marker}:{trigger_id}"
        return ""

    async def _cancel_protective_order_for_reconcile(self, order: PlacedOrder, *, reason: str) -> bool:
        await self.cancel_limiter.wait()
        raw_expected_epoch = self._current_ib_order_identity().get("ib_connection_epoch")
        expected_epoch = int(raw_expected_epoch) if raw_expected_epoch is not None else None
        try:
            await self.broker.cancel(int(order.order_id), expected_epoch=expected_epoch)
        except Exception as exc:
            logger.warning(
                "protective_family_reconcile_cancel_failed",
                symbol=order.symbol,
                order_id=order.order_id,
                reason=reason,
                error=str(exc),
            )
            return False
        async with self._lock:
            self.orders.pop(int(order.order_id), None)
        metrics['kern_cancel'].inc()
        logger.warning(
            "protective_family_reconcile_cancelled",
            symbol=order.symbol,
            order_id=order.order_id,
            order_ref=order.order_ref,
            reason=reason,
        )
        return True

    async def _resize_protective_order_for_reconcile(self, order: PlacedOrder, *, target_qty: float, reason: str) -> bool:
        modify_stop_fn = getattr(self.broker, "modify_stop_order", None)
        if not callable(modify_stop_fn):
            logger.warning(
                "protective_family_reconcile_resize_unavailable",
                symbol=order.symbol,
                order_id=order.order_id,
                target_qty=target_qty,
                reason=reason,
            )
            return False
        raw_expected_epoch = self._current_ib_order_identity().get("ib_connection_epoch")
        expected_epoch = int(raw_expected_epoch) if raw_expected_epoch is not None else None
        ok = await modify_stop_fn(int(order.order_id), qty=float(target_qty), expected_epoch=expected_epoch)
        if not ok:
            logger.warning(
                "protective_family_reconcile_resize_failed",
                symbol=order.symbol,
                order_id=order.order_id,
                target_qty=target_qty,
                reason=reason,
            )
            return False
        async with self._lock:
            local = self.orders.get(int(order.order_id))
            if local is not None:
                local.qty = float(target_qty)
        metrics['kern_modify'].inc()
        logger.info(
            "protective_family_reconcile_resized",
            symbol=order.symbol,
            order_id=order.order_id,
            target_qty=target_qty,
            reason=reason,
        )
        return True

    async def reconcile_protective_order_families_once(
        self,
        *,
        target_qty_by_family: Optional[Dict[str, float]] = None,
        position_qty_by_symbol: Optional[Dict[str, float]] = None,
        desired_specs_by_family: Optional[Dict[str, ProtectionSpec]] = None,
        use_broker_symbol_qty: bool = True,
    ) -> Dict[str, int]:
        """Converge active protective orders by family without increasing risk."""
        if position_qty_by_symbol is None and use_broker_symbol_qty:
            broker_qty = await self._broker_position_qty_map()
            position_qty_by_symbol = broker_qty or {}
        elif position_qty_by_symbol is None:
            position_qty_by_symbol = {}
        target_qty_by_family = dict(target_qty_by_family or {})
        async with self._lock:
            orders = [order for order in self.orders.values() if self._active_protective_order(order)]

        grouped: Dict[str, List[PlacedOrder]] = {}
        for order in orders:
            family_key = self._protection_family_key(order)
            if not family_key:
                continue
            grouped.setdefault(family_key, []).append(order)

        desired_specs_by_family = dict(desired_specs_by_family or {})
        summary = {"cancelled": 0, "resized": 0, "rebuilt": 0, "families": len(grouped)}
        for family_key, spec in desired_specs_by_family.items():
            if family_key in grouped:
                continue
            if float(spec.qty or 0) <= 0:
                continue
            order_type = order_type_for_mode(spec.mode)
            if order_type not in {"STP", "TRAIL", "TRAIL_LIMIT"}:
                continue
            pos_qty_hint = float(spec.qty) if str(spec.side).upper() == "LONG" else -float(spec.qty)
            placed = await self.place_order(
                symbol=spec.symbol,
                side=close_action_for_side(spec.side),
                qty=float(spec.qty),
                order_type=order_type,
                stop_px=spec.desired_stop_px,
                trail_amount=spec.desired_trail_amount,
                trail_percent=spec.desired_trail_percent,
                trail_stop_px=spec.desired_stop_px,
                lmt_price_offset=spec.desired_limit_offset,
                order_ref=f"protect:{family_key}",
                close_only=True,
                reduce_only=True,
                position_qty_hint=pos_qty_hint,
                previous_stop_px=None,
            )
            if placed is not None:
                summary["rebuilt"] += 1

        for family_key, family_orders in grouped.items():
            live_orders = sorted(
                family_orders,
                key=lambda item: (int(getattr(item, "ts_us", 0) or 0), int(getattr(item, "order_id", 0) or 0)),
                reverse=True,
            )
            keep = live_orders[0]
            for stale in live_orders[1:]:
                if await self._cancel_protective_order_for_reconcile(stale, reason="duplicate_protection_family"):
                    summary["cancelled"] += 1

            symbol = str(keep.symbol or "").strip().upper()
            explicit_target = target_qty_by_family.get(family_key)
            symbol_qty = position_qty_by_symbol.get(symbol)
            target_qty: Optional[float] = None
            if explicit_target is not None:
                target_qty = abs(float(explicit_target or 0.0))
            elif symbol_qty is not None:
                symbol_abs = abs(float(symbol_qty or 0.0))
                if symbol_abs <= 1e-9:
                    target_qty = 0.0
                elif abs(float(keep.qty or 0.0)) > symbol_abs + 1e-9:
                    target_qty = symbol_abs
            if target_qty is None:
                continue
            if target_qty <= 1e-9:
                if await self._cancel_protective_order_for_reconcile(keep, reason="position_flat"):
                    summary["cancelled"] += 1
                continue
            if abs(float(keep.qty or 0.0) - target_qty) > 1e-9:
                if await self._resize_protective_order_for_reconcile(keep, target_qty=target_qty, reason="position_qty_mismatch"):
                    summary["resized"] += 1
        return summary

    # ============================================================================
    # Database Persistence Methods
    # ============================================================================

    async def _resolve_order_row_for_fill_once(self, fill: Fill) -> Optional[Dict[str, Any]]:
        """Resolve core_order row for an incoming fill with protection against broker orderId reuse."""
        if not self.pg:
            return None

        try:
            perm_id = int(fill.perm_id or 0)
        except Exception:
            perm_id = 0

        # Most reliable key: IB permanent order id.
        if perm_id > 0:
            try:
                rows = await self._pg_fetchall_compat(
                    """
                    SELECT order_id, idempotency_key, qty, strategy_id, symbol, side, state,
                           broker_order_id, ib_perm_id, order_ref, note, created_at, submitted_at, updated_at
                    FROM core.core_order
                    WHERE ib_perm_id = $1
                    ORDER BY updated_at DESC
                    LIMIT 5
                    """,
                    perm_id,
                )
                if not rows:
                    row_once = await self._pg_fetchone_compat(
                        """
                        SELECT order_id, idempotency_key, qty, strategy_id, symbol, side, state,
                               broker_order_id, ib_perm_id, order_ref, note, created_at, submitted_at, updated_at
                        FROM core.core_order
                        WHERE ib_perm_id = $1
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        perm_id,
                    )
                    rows = [row_once] if row_once else []
                row = self._select_verified_fill_order_candidate(fill=fill, rows=rows, lookup_kind="perm_id")
                if row:
                    return row
            except Exception as exc:
                logger.warning("order_resolve_by_perm_failed", perm_id=perm_id, error=str(exc))

        # Next best: exact order_ref (strategy-scoped tag for qqqv1).
        order_ref = str(fill.order_ref or "").strip()
        if order_ref:
            try:
                rows = await self._pg_fetchall_compat(
                    """
                    SELECT order_id, idempotency_key, qty, strategy_id, symbol, side, state,
                           broker_order_id, ib_perm_id, order_ref, note, created_at, submitted_at, updated_at
                    FROM core.core_order
                    WHERE order_ref = $1
                    ORDER BY created_at DESC
                    LIMIT 5
                    """,
                    order_ref,
                )
                if not rows:
                    row_once = await self._pg_fetchone_compat(
                        """
                        SELECT order_id, idempotency_key, qty, strategy_id, symbol, side, state,
                               broker_order_id, ib_perm_id, order_ref, note, created_at, submitted_at, updated_at
                        FROM core.core_order
                        WHERE order_ref = $1
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        order_ref,
                    )
                    rows = [row_once] if row_once else []
                row = self._select_verified_fill_order_candidate(fill=fill, rows=rows, lookup_kind="order_ref")
                if row:
                    return row
            except Exception as exc:
                logger.warning("order_resolve_by_ref_failed", order_ref=order_ref, error=str(exc))

        if not bool(getattr(self, "_allow_broker_order_id_fill_fallback", False)):
            return None

        # Final fallback: broker_order_id can be reused by IB across sessions. Pick the newest row only.
        try:
            rows = await self._pg_fetchall_compat(
                """
                SELECT order_id, idempotency_key, qty, strategy_id, symbol, side, state, order_ref, note,
                       broker_order_id, ib_perm_id, created_at, submitted_at, updated_at
                FROM core.core_order
                WHERE broker_order_id = $1
                ORDER BY created_at DESC
                LIMIT 5
                """,
                str(fill.order_id),
            )
            if not rows:
                row_once = await self._pg_fetchone_compat(
                    """
                    SELECT order_id, idempotency_key, qty, strategy_id, symbol, side, state, order_ref, note,
                           broker_order_id, ib_perm_id, created_at, submitted_at, updated_at
                    FROM core.core_order
                    WHERE broker_order_id = $1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    str(fill.order_id),
                )
                rows = [row_once] if row_once else []
            row = self._select_verified_fill_order_candidate(fill=fill, rows=rows, lookup_kind="broker_order_id")
            if row and self._is_recent_fill_fallback_candidate(row):
                return row
            if row:
                logger.warning(
                    "order_resolve_by_broker_id_stale_candidate_ignored",
                    broker_order_id=fill.order_id,
                    perm_id=fill.perm_id,
                    order_ref=str(fill.order_ref or ""),
                    candidate_order_id=str(row.get("order_id") or ""),
                    candidate_strategy_id=str(row.get("strategy_id") or ""),
                    candidate_symbol=str(row.get("symbol") or ""),
                    candidate_created_at=str(row.get("created_at") or ""),
                    candidate_updated_at=str(row.get("updated_at") or ""),
                    recent_sec=self._fill_broker_order_id_fallback_recent_sec,
                )
            return None
        except Exception as exc:
            logger.warning("order_resolve_by_broker_id_failed", broker_order_id=fill.order_id, error=str(exc))
        return None

    async def _pg_fetchall_compat(self, sql: str, *args: Any) -> list[Any]:
        fetchall = getattr(self.pg, "fetchall", None)
        if not callable(fetchall):
            return []
        result = fetchall(sql, *args)
        if inspect.isawaitable(result):
            result = await result
        return list(result or [])

    async def _pg_fetchone_compat(self, sql: str, *args: Any) -> Optional[Any]:
        fetchone = getattr(self.pg, "fetchone", None)
        if not callable(fetchone):
            return None
        result = fetchone(sql, *args)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _select_verified_fill_order_candidate(
        self,
        *,
        fill: Fill,
        rows: Any,
        lookup_kind: str,
    ) -> Optional[Dict[str, Any]]:
        candidates = [dict(row) for row in list(rows or [])]
        verified = [row for row in candidates if self._fill_matches_order_identity(row=row, fill=fill, lookup_kind=lookup_kind)]
        if len(verified) == 1:
            return verified[0]
        if candidates:
            logger.warning(
                "fill_order_attribution_not_unique",
                lookup_kind=lookup_kind,
                broker_order_id=fill.order_id,
                perm_id=fill.perm_id,
                order_ref=str(fill.order_ref or ""),
                symbol=str(fill.symbol or ""),
                side=str(getattr(fill, "side", "") or ""),
                candidate_count=len(candidates),
                verified_count=len(verified),
            )
        return None

    def _fill_matches_order_identity(self, *, row: Dict[str, Any], fill: Fill, lookup_kind: str) -> bool:
        symbol = str(fill.symbol or "").strip().upper()
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if symbol and row_symbol and symbol != row_symbol:
            return False

        fill_side = str(getattr(fill, "side", "") or "").strip().upper()
        row_side = str(row.get("side") or "").strip().upper()
        if row_side in {"BUY", "SELL"} and fill_side not in {"BUY", "SELL"}:
            return False
        if fill_side in {"BUY", "SELL"} and row_side in {"BUY", "SELL"} and fill_side != row_side:
            return False

        try:
            fill_qty = abs(float(fill.qty or 0.0))
            order_qty = abs(float(row.get("qty") or 0.0))
        except Exception:
            return False
        if fill_qty <= 0.0 or order_qty <= 0.0 or fill_qty > order_qty + 1e-9:
            return False

        if lookup_kind == "perm_id" and "ib_perm_id" in row:
            try:
                if int(fill.perm_id or 0) <= 0 or int(row.get("ib_perm_id") or 0) != int(fill.perm_id or 0):
                    return False
            except Exception:
                return False
        elif lookup_kind == "order_ref" and "order_ref" in row:
            if not str(fill.order_ref or "").strip() or str(row.get("order_ref") or "").strip() != str(fill.order_ref or "").strip():
                return False
        elif lookup_kind == "broker_order_id" and "broker_order_id" in row:
            if str(row.get("broker_order_id") or "").strip() != str(fill.order_id):
                return False

        fill_dt = self._fill_datetime(fill)
        if fill_dt is not None:
            anchors = [
                self._coerce_order_datetime(row.get(field))
                for field in ("created_at", "submitted_at", "updated_at")
            ]
            anchors = [item for item in anchors if item is not None]
            if anchors and fill_dt < (min(anchors) - timedelta(seconds=5)):
                return False
        return True

    @staticmethod
    def _fill_datetime(fill: Fill) -> Optional[datetime]:
        try:
            ts_us = int(fill.ts_us or 0)
        except Exception:
            return None
        if ts_us <= 0:
            return None
        try:
            return datetime.fromtimestamp(ts_us / 1e6, tz=timezone.utc)
        except Exception:
            return None

    def _is_recent_fill_fallback_candidate(self, row: Dict[str, Any]) -> bool:
        recent_sec = max(1.0, float(getattr(self, "_fill_broker_order_id_fallback_recent_sec", 300.0) or 300.0))
        now_utc = datetime.now(timezone.utc)
        for field in ("created_at", "updated_at"):
            ts = self._coerce_order_datetime(row.get(field))
            if ts is None:
                continue
            age_sec = max(0.0, (now_utc - ts).total_seconds())
            if age_sec <= recent_sec:
                return True
        return False

    @staticmethod
    def _coerce_order_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                return None
        else:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    async def _resolve_order_row_for_fill(self, fill: Fill) -> Optional[Dict[str, Any]]:
        row = await self._resolve_order_row_for_fill_once(fill)
        if row is not None:
            return row

        retry_attempts = max(0, int(getattr(self, "_fill_order_resolve_retry_attempts", 0) or 0))
        retry_sleep_sec = max(0.0, float(getattr(self, "_fill_order_resolve_retry_sleep_sec", 0.0) or 0.0))
        has_lookup_key = bool(fill.perm_id or fill.order_ref or fill.order_id)
        if not has_lookup_key or retry_attempts <= 0 or retry_sleep_sec <= 0.0:
            return None

        for attempt in range(1, retry_attempts + 1):
            await asyncio.sleep(retry_sleep_sec)
            row = await self._resolve_order_row_for_fill_once(fill)
            if row is not None:
                logger.info(
                    "order_row_resolved_after_fill_retry",
                    broker_order_id=fill.order_id,
                    perm_id=fill.perm_id,
                    order_ref=str(fill.order_ref or ""),
                    retry_count=attempt,
                )
                return row
        return None

    async def _get_filled_qty(self, order_uuid: Any) -> float:
        """
        Get total filled quantity for order from core_fill table
        Since filled_qty is not stored in core_order, we calculate it

        Args:
            order_uuid: core_order.order_id UUID

        Returns:
            Total filled quantity
        """
        if not self.pg:
            return 0.0

        try:
            result = await self.pg.fetchone(
                """
                SELECT COALESCE(SUM(f.qty), 0) as filled_qty
                FROM core.core_fill f
                WHERE f.order_id = $1
                """,
                order_uuid
            )
            return float(result['filled_qty']) if result else 0.0

        except Exception as e:
            logger.error("Failed to get filled quantity",
                        order_uuid=order_uuid,
                        error=str(e))
            return 0.0

    async def _fill_exists_by_exec_id(self, exec_id: str) -> bool:
        if not self.pg:
            return False
        exec_key = str(exec_id or "").strip()
        if not exec_key:
            return False
        try:
            row = await self.pg.fetchone(
                """
                SELECT 1
                FROM core.core_fill
                WHERE exec_id = $1
                LIMIT 1
                """,
                exec_key,
            )
            return bool(row)
        except Exception as exc:
            logger.warning("fill_exists_lookup_failed", exec_id=exec_key, error=str(exc))
            return False

    async def backfill_fill_after_order_visible(self, fill: Fill) -> Dict[str, Any]:
        """
        Recover the fill persistence path after a transient order-row visibility race.

        This path is intentionally narrower than on_fill():
        - resolve the order row again
        - persist core_fill if still missing
        - update core_position / core_order state
        - only finalize terminal side effects if the order was not already FILLED
        """
        resolved_order = await self._resolve_order_row_for_fill(fill)
        if resolved_order is None:
            return {"resolved": False, "persisted": False, "state_updated": False, "terminal_applied": False}

        order_uuid = resolved_order["order_id"]
        state_before = str(resolved_order.get("state") or "").strip().upper()
        fill_exists = await self._fill_exists_by_exec_id(str(fill.exec_id or ""))
        inserted = False
        if not fill_exists:
            inserted = await self._persist_fill(fill, order_uuid=order_uuid)
            if inserted:
                try:
                    await self._apply_fill_to_core_position(resolved_order=resolved_order, fill=fill)
                except Exception as exc:
                    logger.warning(
                        "position_update_from_fill_backfill_failed_non_blocking",
                        strategy_id=str(resolved_order.get("strategy_id") or ""),
                        symbol=str(resolved_order.get("symbol") or ""),
                        broker_order_id=fill.order_id,
                        exec_id=fill.exec_id,
                        error=str(exc),
                    )

        db_filled_qty = await self._get_filled_qty(order_uuid)
        try:
            qty_db = resolved_order.get("qty")
            order_qty = float(abs(qty_db)) if qty_db is not None else None
        except Exception:
            order_qty = None
        if order_qty is not None and order_qty > 0:
            new_state = "FILLED" if db_filled_qty >= (order_qty - 1e-9) else "PARTIAL"
        else:
            new_state = "FILLED" if db_filled_qty > 0 else "PARTIAL"

        await self._update_order_state(order_uuid, new_state, db_filled_qty)

        terminal_applied = False
        if new_state == "FILLED" and state_before != "FILLED":
            decision_id = self._extract_decision_id_from_note(resolved_order.get("note"))
            if not decision_id:
                decision_id = await self._resolve_decision_id_from_events(
                    order_uuid=resolved_order.get("order_id"),
                    broker_order_id=fill.order_id,
                )
            await self._finalize_decision_terminal(
                decision_id=decision_id,
                terminal_state="FILLED",
            )
            await self._release_reservation(
                fill.order_id,
                "FILLED",
                idempotency_key_override=str(resolved_order.get("idempotency_key") or ""),
            )
            try:
                note_data = _note_json_dict(resolved_order.get("note"))
                extras = note_data.get("extras") if isinstance(note_data.get("extras"), dict) else {}
            except Exception:
                extras = {}
            if bool(extras.get("flatten_action") or extras.get("close_only") or extras.get("protective_stop")):
                controller = self.flatten_controller or FlattenController(
                    pg=self.pg,
                    manual_intervention_lock_store=self.manual_intervention_lock_store,
                )
                await controller.resolve_from_position(
                    order_uuid=order_uuid,
                    resolved_order=resolved_order,
                    confirmed_by="safety_kernel_fill_backfill",
                    fill_broker_order_id=fill.order_id,
                    unlock_reason=str(extras.get("intent_action") or "close_only_filled"),
                )
            terminal_applied = True

        return {
            "resolved": True,
            "persisted": bool(inserted or fill_exists),
            "fill_inserted": bool(inserted),
            "state_updated": True,
            "terminal_applied": terminal_applied,
            "new_state": new_state,
            "state_before": state_before,
        }

    async def _persist_fill(self, fill: Fill, *, order_uuid: Any) -> bool:
        """
        Persist fill to database

        Args:
            fill: Fill object
        """
        if not self.pg:
            return False

        try:
            # Convert timestamp from microseconds to timestamptz
            ts = datetime.fromtimestamp(fill.ts_us / 1e6)

            row = await self.pg.fetchone(
                """
                INSERT INTO core.core_fill (
                    fill_id, order_id, ts, qty, price,
                    liquidity, commission_usd, exchange, exec_id, created_at
                ) VALUES (
                    gen_random_uuid(), $1, $2, $3, $4,
                    $5, $6, $7, $8, now()
                )
                ON CONFLICT (order_id, ts, qty, price) DO NOTHING
                RETURNING 1 AS inserted
                """,
                order_uuid, ts, fill.qty, fill.price,
                fill.liquidity, fill.commission,
                fill.exchange, fill.exec_id
            )

            logger.debug("Fill persisted to database",
                        fill_id=fill.fill_id,
                        order_id=fill.order_id,
                        qty=fill.qty,
                        price=fill.price)
            return bool(row and row.get("inserted") == 1)

        except Exception as e:
            logger.error("Failed to persist fill",
                        fill_id=fill.fill_id,
                        error=str(e))
            return False

    def _protective_owner_from_resolved_order(
        self,
        *,
        resolved_order: Dict[str, Any],
        note_payload: Dict[str, Any],
        extras: Dict[str, Any],
        lot_id: Optional[str],
    ) -> Optional[ProtectiveOrderOwner]:
        """Resolve the owner lane for a protective order fill.

        Missing owner identity is intentionally fail-closed. A protective fill
        must never fall back to strategy_id+symbol or broker aggregate position.
        """

        role = _protective_order_role(note_payload, extras, resolved_order.get("order_type"))
        if role not in {OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT, OrderRole.TRAILING_STOP}:
            return None

        strategy_id = str(resolved_order.get("strategy_id") or "").strip()
        symbol = str(resolved_order.get("symbol") or "").strip().upper()
        account_id = str(
            extras.get("account_id")
            or note_payload.get("account_id")
            or resolved_order.get("account_id")
            or self._resolved_account_id()
            or ""
        ).strip()
        strategy_instance_id = str(
            extras.get("strategy_instance_id")
            or note_payload.get("strategy_instance_id")
            or resolved_order.get("strategy_instance_id")
            or ""
        ).strip()
        explicit_lot_id = (
            extras.get("position_lot_id")
            or extras.get("lot_id")
            or note_payload.get("position_lot_id")
            or note_payload.get("lot_id")
            or _lot_id_from_protection_key(note_payload.get("protection_key") or extras.get("protection_key"))
        )
        resolved_lot_id = str(explicit_lot_id or "").strip()
        if not (account_id and account_id != "UNKNOWN_ACCOUNT" and strategy_id and strategy_instance_id and symbol and resolved_lot_id):
            return None

        return ProtectiveOrderOwner(
            account_id=account_id,
            strategy_id=strategy_id,
            strategy_instance_id=strategy_instance_id,
            symbol=symbol,
            lot_id=resolved_lot_id,
            order_role=role,
            parent_order_id=str(note_payload.get("parent_order_id") or extras.get("parent_order_id") or "").strip() or None,
            bracket_id=str(note_payload.get("bracket_id") or extras.get("bracket_id") or "").strip() or None,
            oca_group=str(note_payload.get("oca_group") or extras.get("oca_group") or "").strip() or None,
        )

    async def _apply_fill_to_core_position(self, *, resolved_order: Dict[str, Any], fill: Fill) -> None:
        if not self.pg:
            return

        strategy_id = str(resolved_order.get("strategy_id") or "").strip()
        symbol = str(resolved_order.get("symbol") or "").strip().upper()
        side = str(resolved_order.get("side") or "").strip().upper()
        if not strategy_id or not symbol or side not in {"BUY", "SELL"}:
            return

        fill_qty = float(fill.qty or 0.0)
        fill_px = float(fill.price or 0.0)
        if fill_qty <= 0 or fill_px <= 0:
            return

        try:
            note_payload = _note_json_dict(resolved_order.get("note"))
            extras = note_payload.get("extras") if isinstance(note_payload.get("extras"), dict) else {}
        except Exception:
            note_payload = {}
            extras = {}

        ledger_supported = isinstance(self.pg, PostgresDB) or bool(getattr(self.pg, "_supports_position_ledger", False))
        lot_id = None
        if self.position_ledger is not None and ledger_supported:
            lot_id = await self._resolve_fill_lot_id(
                resolved_order=resolved_order,
                note_payload=note_payload,
                extras=extras,
            )
        protective_owner = self._protective_owner_from_resolved_order(
            resolved_order=resolved_order,
            note_payload=note_payload,
            extras=extras,
            lot_id=lot_id,
        )
        if _protective_order_role(note_payload, extras, resolved_order.get("order_type")) == OrderRole.STOP_LOSS and protective_owner is None:
            logger.warning(
                "stop_loss_fill_position_update_blocked_unknown_owner",
                strategy_id=strategy_id,
                symbol=symbol,
                broker_order_id=fill.order_id,
                exec_id=fill.exec_id,
            )
            return
        if protective_owner is not None and protective_owner.order_role == OrderRole.STOP_LOSS and not (
            self.position_ledger is not None and ledger_supported
        ):
            logger.warning(
                "stop_loss_fill_position_update_blocked_without_owner_ledger",
                strategy_id=strategy_id,
                symbol=symbol,
                broker_order_id=fill.order_id,
                exec_id=fill.exec_id,
            )
            return
        if self.position_ledger is not None and ledger_supported:
            ledger_account_id = protective_owner.account_id if protective_owner is not None else self._resolved_account_id()
            if ledger_account_id != "UNKNOWN_ACCOUNT":
                try:
                    self.position_ledger.account_id = ledger_account_id
                except Exception:
                    pass
            ledger_write_succeeded = False
            try:
                await self.position_ledger.append_fill_event(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    side=side,
                    qty=fill_qty,
                    price=fill_px,
                    event_ts=datetime.fromtimestamp(float(fill.ts_us or 0) / 1e6),
                    commission=float(fill.commission or 0.0),
                    broker_order_id=fill.order_id,
                    ib_perm_id=fill.perm_id or resolved_order.get("ib_perm_id"),
                    exec_id=fill.exec_id,
                    idempotency_key=resolved_order.get("idempotency_key"),
                    order_ref=resolved_order.get("order_ref"),
                    lot_id=protective_owner.lot_id if protective_owner is not None else lot_id,
                    strategy_instance_id=protective_owner.strategy_instance_id if protective_owner is not None else (
                        extras.get("strategy_instance_id")
                        or note_payload.get("strategy_instance_id")
                        or resolved_order.get("strategy_instance_id")
                    ),
                    position_owner_key=(
                        _position_owner_key(
                            strategy_id=protective_owner.strategy_id,
                            strategy_instance_id=protective_owner.strategy_instance_id,
                            symbol=protective_owner.symbol,
                            lot_id=protective_owner.lot_id,
                        )
                        if protective_owner is not None
                        else None
                    ),
                    bracket_id=protective_owner.bracket_id if protective_owner is not None else (
                        extras.get("bracket_id") or note_payload.get("bracket_id")
                    ),
                    order_role=protective_owner.order_role.value if protective_owner is not None else (
                        extras.get("order_role") or note_payload.get("order_role")
                    ),
                    metadata={
                        "core_order_id": str(resolved_order.get("order_id") or ""),
                        "core_position_sync_source": "position_ledger_current_qty_fill_avg",
                        "protective_order_owner": (
                            {
                                "account_id": protective_owner.account_id,
                                "strategy_id": protective_owner.strategy_id,
                                "strategy_instance_id": protective_owner.strategy_instance_id,
                                "symbol": protective_owner.symbol,
                                "lot_id": protective_owner.lot_id,
                                "order_role": protective_owner.order_role.value,
                            }
                            if protective_owner is not None
                            else None
                        ),
                    },
                )
                ledger_write_succeeded = True
            except Exception as exc:
                logger.warning(
                    "position_ledger_fill_append_failed_non_blocking",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    broker_order_id=fill.order_id,
                    exec_id=fill.exec_id,
                    error=str(exc),
                )
            if ledger_write_succeeded:
                if protective_owner is not None:
                    return
                try:
                    await self._sync_core_position_from_ledger_current(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        side=side,
                        fill_qty=fill_qty,
                        fill_px=fill_px,
                    )
                    return
                except Exception as exc:
                    logger.warning(
                        "core_position_ledger_sync_failed_falling_back_legacy_update",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        broker_order_id=fill.order_id,
                        exec_id=fill.exec_id,
                        error=str(exc),
                    )

        await self._apply_fill_to_core_position_legacy(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            fill_qty=fill_qty,
            fill_px=fill_px,
        )

    async def _apply_fill_to_core_position_legacy(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        fill_qty: float,
        fill_px: float,
    ) -> None:
        async def _fetch_row(executor: Any, sql: str, *args: Any) -> Optional[Dict[str, Any]]:
            fetchrow_fn = getattr(executor, "fetchrow", None)
            fetchone_fn = getattr(executor, "fetchone", None)
            if fetchrow_fn is not None and inspect.iscoroutinefunction(fetchrow_fn):
                fetch = fetchrow_fn
            elif fetchone_fn is not None and inspect.iscoroutinefunction(fetchone_fn):
                fetch = fetchone_fn
            else:
                fetch = fetchrow_fn or fetchone_fn
            if fetch is None:
                raise AttributeError("executor missing fetchrow/fetchone")
            row = await fetch(sql, *args)
            if row and not isinstance(row, dict):
                row = dict(row)
            return row

        async def _apply_with_executor(executor: Any, *, lock_row: bool) -> None:
            select_sql = """
                SELECT pos_qty, avg_px, realized_pnl_usd, unrealized_pnl_usd
                FROM core.core_position
                WHERE strategy_id = $1 AND symbol = $2
            """
            if lock_row:
                select_sql += "\n                FOR UPDATE"
            row = await _fetch_row(executor, select_sql, strategy_id, symbol)

            old_qty = float(row.get("pos_qty") or 0.0) if row else 0.0
            old_avg = float(row.get("avg_px") or 0.0) if (row and row.get("avg_px") is not None) else 0.0
            old_realized = float(row.get("realized_pnl_usd") or 0.0) if row else 0.0
            old_unrealized = float(row.get("unrealized_pnl_usd") or 0.0) if row else 0.0

            signed = fill_qty if side == "BUY" else -fill_qty
            new_qty = old_qty + signed

            realized_delta = 0.0
            if old_qty > 1e-12 and signed < -1e-12 and old_avg > 0:
                close_qty = min(old_qty, abs(signed))
                realized_delta = close_qty * (fill_px - old_avg)
            elif old_qty < -1e-12 and signed > 1e-12 and old_avg > 0:
                close_qty = min(abs(old_qty), signed)
                realized_delta = close_qty * (old_avg - fill_px)

            new_realized = old_realized + realized_delta

            if abs(new_qty) <= 1e-12:
                new_avg = None
            elif old_qty == 0.0:
                new_avg = fill_px
            elif old_qty > 0 and new_qty > 0:
                new_avg = (((abs(old_qty) * old_avg) + (abs(signed) * fill_px)) / abs(new_qty)) if signed > 0 else (old_avg if old_avg > 0 else fill_px)
            elif old_qty < 0 and new_qty < 0:
                new_avg = (((abs(old_qty) * old_avg) + (abs(signed) * fill_px)) / abs(new_qty)) if signed < 0 else (old_avg if old_avg > 0 else fill_px)
            else:
                new_avg = fill_px

            if row:
                await executor.execute(
                    """
                    UPDATE core.core_position
                    SET pos_qty = $3,
                        avg_px = $4,
                        realized_pnl_usd = $5,
                        updated_at = now()
                    WHERE strategy_id = $1 AND symbol = $2
                    """,
                    strategy_id,
                    symbol,
                    new_qty,
                    new_avg,
                    new_realized,
                )
            else:
                await executor.execute(
                    """
                    INSERT INTO core.core_position (
                        strategy_id, symbol, pos_qty, avg_px, realized_pnl_usd, unrealized_pnl_usd, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, now()
                    )
                    """,
                    strategy_id,
                    symbol,
                    new_qty,
                    new_avg,
                    new_realized,
                    old_unrealized,
                )

        supports_position_locking = isinstance(self.pg, PostgresDB) or (
            getattr(self.pg, "_supports_position_locking", False) is True
        )
        if supports_position_locking:
            async with self.pg.acquire_conn() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        _core_position_lock_key(strategy_id, symbol),
                    )
                    await _apply_with_executor(conn, lock_row=True)
            return

        await _apply_with_executor(self.pg, lock_row=False)

    async def _resolve_fill_lot_id(
        self,
        *,
        resolved_order: Dict[str, Any],
        note_payload: Dict[str, Any],
        extras: Dict[str, Any],
    ) -> Optional[str]:
        direct = (
            extras.get("position_lot_id")
            or extras.get("position_id")
            or note_payload.get("position_lot_id")
            or note_payload.get("position_id")
        )
        direct_s = str(direct or "").strip()
        if direct_s:
            return direct_s

        protection_key = str(note_payload.get("protection_key") or extras.get("protection_key") or "").strip()
        lot_from_key = _lot_id_from_protection_key(protection_key)
        if lot_from_key:
            return lot_from_key

        strategy_id = str(resolved_order.get("strategy_id") or "").strip()
        symbol = str(resolved_order.get("symbol") or "").strip().upper()
        account_id = str(
            extras.get("account_id")
            or note_payload.get("account_id")
            or resolved_order.get("account_id")
            or self._resolved_account_id()
            or "UNKNOWN_ACCOUNT"
        ).strip() or "UNKNOWN_ACCOUNT"
        strategy_instance_id = str(
            extras.get("strategy_instance_id")
            or note_payload.get("strategy_instance_id")
            or resolved_order.get("strategy_instance_id")
            or strategy_id
        ).strip()
        deterministic_lot_id = (
            self.default_lot_id(
                account_id=account_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
            )
            if strategy_instance_id and symbol
            else None
        )

        if not self.pg:
            return deterministic_lot_id

        broker_order_id = str(resolved_order.get("broker_order_id") or "").strip()
        order_ref = str(resolved_order.get("order_ref") or "").strip()
        try:
            perm_id = int(resolved_order.get("ib_perm_id") or 0)
        except Exception:
            perm_id = 0

        try:
            parent_order_id = str(note_payload.get("parent_order_id") or "").strip()
            if parent_order_id:
                parent = await self.pg.fetchone(
                    """
                    SELECT note
                    FROM core.core_order
                    WHERE order_id = $1::uuid
                    LIMIT 1
                    """,
                    parent_order_id,
                )
                parent_note = _note_json_dict(parent.get("note") if parent else None)
                parent_extras = parent_note.get("extras") if isinstance(parent_note.get("extras"), dict) else {}
                parent_lot = (
                    parent_extras.get("position_lot_id")
                    or parent_extras.get("position_id")
                    or parent_note.get("position_lot_id")
                    or parent_note.get("position_id")
                )
                parent_lot_s = str(parent_lot or "").strip()
                if parent_lot_s:
                    return parent_lot_s
        except Exception as exc:
            logger.warning(
                "fill_lot_parent_order_lookup_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                parent_order_id=str(note_payload.get("parent_order_id") or ""),
                error=str(exc),
            )

        try:
            rows = await self.pg.fetchall(
                """
                SELECT DISTINCT lot_id
                FROM core.core_protection_order_registry
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND lot_id IS NOT NULL
                  AND lot_id <> ''
                  AND (
                    ($3 <> '' AND protection_key = $3)
                    OR ($4 <> '' AND broker_order_id::text = $4)
                    OR ($5::bigint > 0 AND ib_perm_id = $5::bigint)
                    OR ($6 <> '' AND order_ref = $6)
                  )
                LIMIT 2
                """,
                strategy_id,
                symbol,
                protection_key,
                broker_order_id,
                perm_id,
                order_ref,
            )
            lot_ids = [str(row.get("lot_id") or "").strip() for row in rows or [] if str(row.get("lot_id") or "").strip()]
            if len(lot_ids) == 1:
                return lot_ids[0]
            if len(lot_ids) > 1:
                logger.warning(
                    "fill_lot_registry_lookup_ambiguous",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    ib_perm_id=perm_id,
                    order_ref=order_ref,
                    lot_ids=lot_ids,
                )
                return None
        except Exception as exc:
            logger.warning(
                "fill_lot_registry_lookup_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                broker_order_id=broker_order_id,
                ib_perm_id=perm_id,
                order_ref=order_ref,
                error=str(exc),
            )
        return deterministic_lot_id

    async def _sync_core_position_from_ledger_current(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        fill_qty: float,
        fill_px: float,
    ) -> None:
        if not self.pg or self.position_ledger is None:
            return

        strategy_id = str(strategy_id or "").strip()
        symbol = str(symbol or "").strip().upper()
        if not strategy_id or not symbol:
            return

        account_id = str(getattr(self.position_ledger, "account_id", "") or "").strip()

        async def _fetch_row(executor: Any, sql: str, *args: Any) -> Optional[Dict[str, Any]]:
            fetchrow_fn = getattr(executor, "fetchrow", None)
            fetchone_fn = getattr(executor, "fetchone", None)
            if fetchrow_fn is not None and inspect.iscoroutinefunction(fetchrow_fn):
                fetch = fetchrow_fn
            elif fetchone_fn is not None and inspect.iscoroutinefunction(fetchone_fn):
                fetch = fetchone_fn
            else:
                fetch = fetchrow_fn or fetchone_fn
            if fetch is None:
                raise AttributeError("executor missing fetchrow/fetchone")
            row = await fetch(sql, *args)
            if row and not isinstance(row, dict):
                row = dict(row)
            return row

        async def _sync_with_executor(executor: Any) -> None:
            row = await _fetch_row(
                executor,
                """
                SELECT
                    COALESCE(SUM(qty), 0) AS ledger_qty
                FROM core.position_ledger_current
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND ($3::text = '' OR account_id = $3)
                """,
                strategy_id,
                symbol,
                account_id,
            )
            ledger_qty = float((row or {}).get("ledger_qty") or 0.0)
            existing = await _fetch_row(
                executor,
                """
                SELECT pos_qty, avg_px
                FROM core.core_position
                WHERE strategy_id = $1 AND symbol = $2
                """,
                strategy_id,
                symbol,
            )
            old_qty = float((existing or {}).get("pos_qty") or 0.0)
            old_avg = float((existing or {}).get("avg_px") or 0.0) if existing and (existing or {}).get("avg_px") is not None else 0.0
            side_u = str(side or "").upper()
            signed = abs(float(fill_qty or 0.0)) if side_u == "BUY" else -abs(float(fill_qty or 0.0))
            fill_px_f = float(fill_px or 0.0)
            avg_px: Optional[float]
            if abs(ledger_qty) <= 1e-12:
                avg_px = None
            elif abs(old_qty) <= 1e-12 or (old_qty > 0 > ledger_qty) or (old_qty < 0 < ledger_qty):
                avg_px = fill_px_f if fill_px_f > 0 else None
            elif (old_qty > 0 and ledger_qty > 0 and signed > 0) or (old_qty < 0 and ledger_qty < 0 and signed < 0):
                denom = abs(float(ledger_qty))
                if denom > 1e-12 and old_avg > 0 and fill_px_f > 0:
                    avg_px = ((abs(old_qty) * old_avg) + (abs(signed) * fill_px_f)) / denom
                else:
                    avg_px = old_avg if old_avg > 0 else (fill_px_f if fill_px_f > 0 else None)
            else:
                avg_px = old_avg if old_avg > 0 else (fill_px_f if fill_px_f > 0 else None)

            await executor.execute(
                """
                INSERT INTO core.core_position (
                    strategy_id, symbol, pos_qty, avg_px, realized_pnl_usd, unrealized_pnl_usd, updated_at
                ) VALUES (
                    $1, $2, $3, $4, 0, 0, now()
                )
                ON CONFLICT (strategy_id, symbol) DO UPDATE
                SET pos_qty = EXCLUDED.pos_qty,
                    avg_px = EXCLUDED.avg_px,
                    updated_at = now()
                """,
                strategy_id,
                symbol,
                ledger_qty,
                avg_px,
            )
            logger.info(
                "core_position_synced_from_position_ledger",
                strategy_id=strategy_id,
                symbol=symbol,
                pos_qty=ledger_qty,
                avg_px=avg_px,
                avg_px_source="fill_accounting_not_ledger_notional",
            )

        supports_position_locking = isinstance(self.pg, PostgresDB) or (
            getattr(self.pg, "_supports_position_locking", False) is True
        )
        if supports_position_locking:
            async with self.pg.acquire_conn() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        _core_position_lock_key(strategy_id, symbol),
                    )
                    await _sync_with_executor(conn)
            return

        await _sync_with_executor(self.pg)

    async def _persist_oco_orders(
        self,
        parent_order_id: int,
        sl_order: Optional[PlacedOrder],
        tp_order: Optional[PlacedOrder],
        oca_group: str,
        strategy_id: str,
        account_id: str = "UNKNOWN_ACCOUNT",
        strategy_instance_id: str = "",
        bracket_id: str = "",
        sl_protection_key: str = "",
        tp_protection_key: str = "",
    ) -> None:
        """
        Persist OCO (stop loss + take profit) orders to database

        Args:
            parent_order_id: Parent entry order broker_order_id
            sl_order: Stop loss PlacedOrder
            tp_order: Take profit PlacedOrder
            oca_group: OCO group identifier
            strategy_id: Strategy ID
        """
        if not self.pg:
            return

        try:
            # First, get the parent order UUID from broker_order_id
            parent_row = await self.pg.fetchone(
                """
                SELECT order_id FROM core.core_order
                WHERE broker_order_id = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(parent_order_id)
            )

            if not parent_row:
                logger.warning("Cannot persist OCO orders - parent order not found",
                              parent_broker_order_id=parent_order_id)
                return

            parent_uuid = parent_row['order_id']
            sl_lot_id = _lot_id_from_protection_key(sl_protection_key)
            tp_lot_id = _lot_id_from_protection_key(tp_protection_key)

            if sl_order is not None:
                sl_position_owner = _position_owner_key(
                    strategy_id=strategy_id,
                    strategy_instance_id=strategy_instance_id,
                    symbol=sl_order.symbol,
                    lot_id=sl_lot_id,
                )
                sl_note = json.dumps({
                    "parent_order_id": str(parent_uuid),
                    "oca_group": oca_group,
                    "bracket_id": bracket_id or oca_group,
                    "type": "stop_loss",
                    "order_role": OrderRole.STOP_LOSS.value,
                    "account_id": account_id,
                    "strategy_instance_id": strategy_instance_id,
                    "protection_key": str(sl_protection_key or ""),
                    "position_lot_id": sl_lot_id or None,
                    "position_owner": sl_position_owner or None,
                    "position_owner_key": sl_position_owner or None,
                })
                sl_child_key = str(sl_protection_key or f"oco:sl:{parent_uuid}:{sl_order.order_ref or sl_order.order_id}")
                await self._upsert_child_order(
                    strategy_id=strategy_id,
                    parent_uuid=parent_uuid,
                    order=sl_order,
                    idempotency_key=sl_child_key,
                    note=sl_note,
                    limit_px=None,
                    stop_px=sl_order.stop_px,
                )
                await self._upsert_protection_registry_from_order(
                    protection_key=sl_protection_key,
                    strategy_id=strategy_id,
                    order=sl_order,
                    protection_kind="stop_loss",
                    desired_price=sl_order.stop_px,
                    account_id=account_id,
                    strategy_instance_id=strategy_instance_id,
                    bracket_id=bracket_id or oca_group,
                    order_role=OrderRole.STOP_LOSS.value,
                )

            if tp_order is not None:
                tp_position_owner = _position_owner_key(
                    strategy_id=strategy_id,
                    strategy_instance_id=strategy_instance_id,
                    symbol=tp_order.symbol,
                    lot_id=tp_lot_id,
                )
                tp_note = json.dumps({
                    "parent_order_id": str(parent_uuid),
                    "oca_group": oca_group,
                    "bracket_id": bracket_id or oca_group,
                    "type": "take_profit",
                    "order_role": OrderRole.TAKE_PROFIT.value,
                    "account_id": account_id,
                    "strategy_instance_id": strategy_instance_id,
                    "protection_key": str(tp_protection_key or ""),
                    "position_lot_id": tp_lot_id or None,
                    "position_owner": tp_position_owner or None,
                    "position_owner_key": tp_position_owner or None,
                })
                tp_child_key = str(tp_protection_key or f"oco:tp:{parent_uuid}:{tp_order.order_ref or tp_order.order_id}")
                await self._upsert_child_order(
                    strategy_id=strategy_id,
                    parent_uuid=parent_uuid,
                    order=tp_order,
                    idempotency_key=tp_child_key,
                    note=tp_note,
                    limit_px=tp_order.limit_px,
                    stop_px=None,
                )
                await self._upsert_protection_registry_from_order(
                    protection_key=tp_protection_key,
                    strategy_id=strategy_id,
                    order=tp_order,
                    protection_kind="take_profit",
                    desired_price=tp_order.limit_px,
                    account_id=account_id,
                    strategy_instance_id=strategy_instance_id,
                    bracket_id=bracket_id or oca_group,
                    order_role=OrderRole.TAKE_PROFIT.value,
                )

            logger.debug("OCO orders persisted to database",
                        parent_order_id=parent_order_id,
                        sl_order_id=sl_order.order_id if sl_order is not None else None,
                        tp_order_id=tp_order.order_id if tp_order is not None else None,
                        oca_group=oca_group)

        except Exception as e:
            logger.error("Failed to persist OCO orders",
                        parent_order_id=parent_order_id,
                        error=str(e))

    async def _upsert_protection_registry_from_order(
        self,
        *,
        protection_key: str,
        strategy_id: str,
        order: PlacedOrder,
        protection_kind: str,
        desired_price: Optional[float],
        account_id: Optional[str] = None,
        strategy_instance_id: Optional[str] = None,
        bracket_id: Optional[str] = None,
        order_role: Optional[str] = None,
    ) -> None:
        key = str(protection_key or "").strip()
        if not self.pg or not key:
            return
        parts = key.split("|")
        account_id = str(account_id or (parts[0] if len(parts) > 0 and parts[0] else str(os.getenv("IB_ACCOUNT", "") or "UNKNOWN_ACCOUNT"))).strip()
        lot_id = parts[3] if len(parts) > 3 else ""
        kind = parts[4] if len(parts) > 4 and parts[4] else str(protection_kind or "")
        position_owner = _position_owner_key(
            strategy_id=strategy_id,
            strategy_instance_id=strategy_instance_id,
            symbol=order.symbol,
            lot_id=lot_id,
        )
        qty_bucket = parts[6] if len(parts) > 6 else str(abs(float(order.qty or 0.0)))
        price_band_id = parts[7] if len(parts) > 7 else ""
        try:
            await self.pg.execute(
                """
                INSERT INTO core.core_protection_order_registry (
                    protection_key, account_id, strategy_id, strategy_instance_id,
                    symbol, lot_id, position_owner, position_owner_key, bracket_id, order_role,
                    protection_kind, side, qty_bucket, price_band_id,
                    desired_qty, desired_price, broker_order_id, ib_perm_id, order_ref, state,
                    first_seen_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17, $18, $19, 'SUBMITTED',
                    now(), now()
                )
                ON CONFLICT (protection_key) DO UPDATE
                SET broker_order_id = COALESCE(EXCLUDED.broker_order_id, core.core_protection_order_registry.broker_order_id),
                    ib_perm_id = COALESCE(EXCLUDED.ib_perm_id, core.core_protection_order_registry.ib_perm_id),
                    order_ref = COALESCE(EXCLUDED.order_ref, core.core_protection_order_registry.order_ref),
                    strategy_instance_id = COALESCE(EXCLUDED.strategy_instance_id, core.core_protection_order_registry.strategy_instance_id),
                    position_owner = COALESCE(EXCLUDED.position_owner, core.core_protection_order_registry.position_owner),
                    position_owner_key = COALESCE(EXCLUDED.position_owner_key, core.core_protection_order_registry.position_owner_key),
                    bracket_id = COALESCE(EXCLUDED.bracket_id, core.core_protection_order_registry.bracket_id),
                    order_role = COALESCE(EXCLUDED.order_role, core.core_protection_order_registry.order_role),
                    state = EXCLUDED.state,
                    desired_qty = EXCLUDED.desired_qty,
                    desired_price = EXCLUDED.desired_price,
                    updated_at = now()
                """,
                key,
                account_id,
                str(strategy_id or ""),
                str(strategy_instance_id or "").strip() or None,
                str(order.symbol or "").strip().upper(),
                lot_id,
                position_owner or None,
                position_owner or None,
                str(bracket_id or "").strip() or None,
                str(order_role or "").strip().upper() or str(protection_kind or "").strip().upper() or None,
                kind,
                str(order.side or "").strip().upper(),
                qty_bucket,
                price_band_id,
                abs(float(order.qty or 0.0)),
                float(desired_price) if desired_price is not None else None,
                int(order.order_id) if order.order_id is not None else None,
                int(order.perm_id) if order.perm_id else None,
                str(order.order_ref or "")[:120],
            )
        except Exception as exc:
            logger.warning(
                "safety_kernel_protection_registry_upsert_failed",
                protection_key=key,
                strategy_id=strategy_id,
                symbol=getattr(order, "symbol", None),
                error=str(exc),
            )

    def ensure_non_oco_protection_metadata(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        qty: Any,
        desired_price: Any,
        envelope_extras: Dict[str, Any],
    ) -> Dict[str, Any]:
        extras = envelope_extras if isinstance(envelope_extras, dict) else {}
        if not (
            extras.get("protective_stop")
            or extras.get("uom_protection")
            or extras.get("protection_key")
            or extras.get("protection_kind")
        ):
            return extras

        lot_id = str(
            extras.get("position_lot_id")
            or extras.get("position_id")
            or f"lot:{strategy_id}:{symbol}"
        ).strip()
        extras.setdefault("position_lot_id", lot_id)
        protection_kind = str(extras.get("protection_kind") or "protective_stop").strip().lower()
        price = (
            desired_price
            if desired_price is not None
            else extras.get("stop_px")
            or extras.get("limit_px")
            or extras.get("target_px")
            or extras.get("take_profit_px")
            or extras.get("ref_px")
        )
        if not str(extras.get("protection_key") or "").strip():
            protection_meta = build_protection_metadata(
                account_id=self._resolved_account_id(),
                strategy_id=str(strategy_id or ""),
                symbol=str(symbol or ""),
                lot_id=lot_id,
                protection_kind=protection_kind,
                side=str(side or ""),
                qty=qty,
                price=price,
                tick_size=extras.get("tick_size") or 0.01,
                configured_band_width=(extras.get("protection_band_width") or extras.get("price_band_width")),
            )
            extras.update(protection_meta)
        else:
            extras.setdefault("protection_kind", protection_kind)
        return extras

    async def upsert_non_oco_protection_registry(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        qty: Any,
        desired_price: Any,
        order: Optional[PlacedOrder],
        envelope_extras: Dict[str, Any],
    ) -> None:
        if order is None:
            return
        extras = self.ensure_non_oco_protection_metadata(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            qty=qty,
            desired_price=desired_price,
            envelope_extras=envelope_extras,
        )
        protection_key = str(extras.get("protection_key") or "").strip()
        if not protection_key:
            return
        price = (
            desired_price
            if desired_price is not None
            else extras.get("stop_px")
            or extras.get("limit_px")
            or extras.get("target_px")
            or extras.get("take_profit_px")
            or extras.get("ref_px")
        )
        try:
            desired_price_v = float(price) if price is not None else None
        except Exception:
            desired_price_v = None
        await self._upsert_protection_registry_from_order(
            protection_key=protection_key,
            strategy_id=strategy_id,
            order=order,
            protection_kind=str(extras.get("protection_kind") or "protective_stop"),
            desired_price=desired_price_v,
        )

    def _resolved_account_id(self) -> str:
        candidates = [os.getenv("IB_ACCOUNT", "")]
        broker_account = getattr(self.broker, "account", "")
        if isinstance(broker_account, str):
            candidates.append(broker_account)
        ib = getattr(self.broker, "ib", None)
        managed_accounts_fn = getattr(ib, "managedAccounts", None)
        if callable(managed_accounts_fn):
            try:
                accounts = [str(item or "").strip() for item in list(managed_accounts_fn() or [])]
            except Exception:
                accounts = []
            non_empty_accounts = [item for item in accounts if item and item != "UNKNOWN_ACCOUNT"]
            if len(non_empty_accounts) == 1:
                candidates.append(non_empty_accounts[0])
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value and value != "UNKNOWN_ACCOUNT":
                return value
        return "UNKNOWN_ACCOUNT"

    def _requires_known_account_for_fill_mutation(self) -> bool:
        return isinstance(self.pg, PostgresDB) or getattr(self.pg, "_requires_known_account_for_fill_mutation", False) is True

    async def _upsert_child_order(
        self,
        *,
        strategy_id: str,
        parent_uuid: Any,
        order: PlacedOrder,
        idempotency_key: str,
        note: str,
        limit_px: Optional[float],
        stop_px: Optional[float],
    ) -> None:
        existing = None
        existing = await self.pg.fetchone(
            """
            SELECT order_id
            FROM core.core_order
            WHERE idempotency_key = $1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            idempotency_key,
        )
        perm_id = int(order.perm_id) if order.perm_id else None
        if existing is None and perm_id:
            existing = await self.pg.fetchone(
                """
                SELECT order_id
                FROM core.core_order
                WHERE ib_perm_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                perm_id,
            )
        identity = self._current_ib_order_identity()
        if existing:
            await self.pg.execute(
                """
                UPDATE core.core_order
                SET strategy_id = $2,
                    symbol = $3,
                    idempotency_key = $4,
                    ib_perm_id = $5,
                    order_ref = $6,
                    side = $7,
                    order_type = $8,
                    qty = $9,
                    limit_px = $10,
                    stop_px = $11,
                    tif = $12,
                    parent_order_id = $13,
                    note = $14,
                    ib_client_id = $15,
                    ib_session_role = $16,
                    ib_connection_epoch = $17,
                    state = 'SUBMITTED',
                    updated_at = now()
                WHERE order_id = $1
                """,
                existing["order_id"],
                strategy_id,
                order.symbol,
                idempotency_key,
                perm_id,
                order.order_ref,
                order.side,
                order.order_type,
                order.qty,
                limit_px,
                stop_px,
                order.tif,
                parent_uuid,
                note,
                identity["ib_client_id"],
                identity["ib_session_role"],
                identity["ib_connection_epoch"],
            )
            return

        await self.pg.execute(
            """
            INSERT INTO core.core_order (
                order_id, strategy_id, symbol, idempotency_key,
                broker_order_id, ib_perm_id, order_ref, side, order_type, qty,
                limit_px, stop_px, tif, state, parent_order_id,
                ib_client_id, ib_session_role, ib_connection_epoch,
                note, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), $1, $2, $3,
                $4, $5, $6, $7, $8, $9,
                $10, $11, $12, 'SUBMITTED', $13,
                $14, $15, $16,
                $17, now(), now()
            )
            """,
            strategy_id,
            order.symbol,
            idempotency_key,
            str(order.order_id),
            perm_id,
            order.order_ref,
            order.side,
            order.order_type,
            order.qty,
            limit_px,
            stop_px,
            order.tif,
            parent_uuid,
            identity["ib_client_id"],
            identity["ib_session_role"],
            identity["ib_connection_epoch"],
            note,
        )

    @staticmethod
    def _current_ib_order_identity() -> Dict[str, Any]:
        ib_client_id: Optional[int] = None
        ib_connection_epoch: Optional[int] = None
        try:
            ibm = get_ib_connection_manager()
            ib_connection_epoch = int(ibm.exec_connection_epoch())
            adapter = getattr(ibm, "_exec_adapter", None)
            if adapter is not None:
                raw = getattr(adapter, "client_id", None)
                ib_client_id = int(raw) if raw is not None else None
        except Exception:
            ib_client_id = None
            ib_connection_epoch = None
        return {
            "ib_client_id": ib_client_id,
            "ib_session_role": "exec",
            "ib_connection_epoch": ib_connection_epoch,
        }

    async def _update_order_state(
        self,
        order_uuid: Any,
        state: str,
        filled_qty: float
    ) -> None:
        """
        Update order state in database
        Note: filled_qty is not stored in core_order, calculated from core_fill

        Args:
            order_uuid: core_order.order_id UUID
            state: New state (PARTIAL/FILLED)
            filled_qty: Filled quantity (for logging only)
        """
        if not self.pg:
            return

        try:
            raw_expected_epoch = self._current_ib_order_identity().get("ib_connection_epoch")
            expected_epoch = int(raw_expected_epoch) if raw_expected_epoch is not None else None
            await self.pg.execute(
                """
                UPDATE core.core_order
                SET state = $1,
                    updated_at = now()
                WHERE state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')
                  AND order_id = $2::uuid
                  AND ($3::int IS NULL OR ib_connection_epoch IS NULL OR ib_connection_epoch <= $3::int)
                """,
                state, order_uuid, expected_epoch
            )

            logger.debug("Order state updated",
                        order_uuid=order_uuid,
                        state=state,
                        filled_qty=filled_qty)

        except Exception as e:
            logger.error("Failed to update order state",
                        order_uuid=order_uuid,
                        state=state,
                        error=str(e))

    async def _release_reservation(self, order_id: int, terminal_state: str, *, idempotency_key_override: str = "") -> None:
        """
        Release capital reservation on terminal state with retry logic

        Args:
            order_id: Order ID
            terminal_state: Terminal state (FILLED/CANCELLED/REJECTED)
        """
        if not self.reservation:
            return

        # Get idempotency key from mapping / DB. We deliberately pass broker_order_id through as `order_id`
        # to the reservation layer to preserve existing semantics and regression behavior.
        idempotency_key = str(idempotency_key_override or "").strip() or self.order_id_to_idemp.get(order_id)
        if not idempotency_key and self.pg:
            try:
                row = await self.pg.fetchone(
                    """
                    SELECT order_id, idempotency_key FROM core.core_order
                    WHERE broker_order_id = $1
                    """,
                    str(order_id),
                )
                if row:
                    if row.get("idempotency_key"):
                        idempotency_key = str(row["idempotency_key"])
            except Exception as exc:
                logger.warning("Failed to resolve idempotency_key for order", order_id=order_id, error=str(exc))

        if not idempotency_key:
            logger.warning("No idempotency key found for order", order_id=order_id)
            return

        # Retry logic: 3 attempts with exponential backoff
        max_retries = 3
        backoff_seconds = [0.1, 0.5, 2.0]

        for attempt in range(max_retries):
            try:
                released, amount = await self.reservation.release_on_terminal(
                    idempotency_key=idempotency_key,
                    order_id=str(order_id),
                    terminal_state=terminal_state,
                    reason=f"Order {terminal_state}"
                )

                if released:
                    logger.info("Capital reservation released",
                               order_id=order_id,
                               terminal_state=terminal_state,
                               amount=amount,
                               attempt=attempt + 1)
                    return  # Success
                else:
                    logger.warning("Failed to release capital reservation",
                                  order_id=order_id,
                                  terminal_state=terminal_state,
                                  attempt=attempt + 1)
                    # Don't retry if release_on_terminal returned False (already released, etc.)
                    break

            except Exception as e:
                logger.error("Error releasing capital reservation",
                            order_id=order_id,
                            terminal_state=terminal_state,
                            attempt=attempt + 1,
                            error=str(e))

                # If this was the last attempt, mark for manual cleanup
                if attempt == max_retries - 1:
                    await self._mark_failed_release(order_id, idempotency_key, terminal_state, str(e))
                    metrics['kern_err'].labels(op='release_reservation').inc()
                else:
                    # Wait before retry
                    await asyncio.sleep(backoff_seconds[attempt])

    @staticmethod
    def _extract_decision_id_from_note(note_value: Any) -> str:
        raw = str(note_value or "").strip()
        if not raw:
            return ""
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                return ""
            direct = str(obj.get("decision_id") or "").strip()
            if direct:
                return direct
            trace = obj.get("trace")
            if isinstance(trace, dict):
                direct = str(trace.get("decision_id") or "").strip()
                if direct:
                    return direct
            extras = obj.get("extras")
            if isinstance(extras, dict):
                direct = str(extras.get("decision_id") or "").strip()
                if direct:
                    return direct
        except Exception:
            return ""
        return ""

    async def _finalize_decision_terminal(self, *, decision_id: str, terminal_state: str) -> None:
        if self.decision_finalizer is None:
            return
        did = str(decision_id or "").strip()
        if not did:
            return
        try:
            ret = self.decision_finalizer(did, str(terminal_state or "").upper())
            if inspect.isawaitable(ret):
                await ret
        except Exception as exc:
            logger.warning(
                "decision_finalizer_failed",
                decision_id=did,
                terminal_state=terminal_state,
                error=str(exc),
            )

    async def _resolve_decision_id_from_events(
        self,
        *,
        order_uuid: Any = None,
        broker_order_id: Any = None,
    ) -> str:
        if not self.pg:
            return ""
        try:
            oid = str(order_uuid or "").strip()
            bid_raw = str(broker_order_id or "").strip()
            bid = None
            if bid_raw:
                try:
                    bid = int(bid_raw)
                except Exception:
                    bid = None
            row = None
            if oid:
                try:
                    row = await self.pg.fetchone(
                        """
                        SELECT decision_id
                        FROM core.core_order_event
                        WHERE order_id = $1::uuid
                          AND COALESCE(decision_id, '') <> ''
                        ORDER BY event_ts DESC
                        LIMIT 1
                        """,
                        oid,
                    )
                except Exception:
                    row = None
            if (not row) and (bid is not None):
                row = await self.pg.fetchone(
                    """
                    SELECT decision_id
                    FROM core.core_order_event
                    WHERE broker_order_id = $1
                      AND COALESCE(decision_id, '') <> ''
                    ORDER BY event_ts DESC
                    LIMIT 1
                    """,
                    str(bid),
                )
            return str((row or {}).get("decision_id") or "").strip()
        except Exception:
            return ""

    async def _mark_failed_release(
        self,
        order_id: int,
        idempotency_key: str,
        terminal_state: str,
        error: str
    ) -> None:
        """
        Mark failed capital release in database note field for manual cleanup

        Args:
            order_id: Broker order ID
            idempotency_key: Idempotency key
            terminal_state: Terminal state
            error: Error message
        """
        if not self.pg:
            return

        try:
            note_data = json.dumps({
                "release_failed": True,
                "failed_at": datetime.now().isoformat(),
                "terminal_state": terminal_state,
                "idempotency_key": idempotency_key,
                "error": error
            })

            await self.pg.execute(
                """
                UPDATE core.core_order
                SET note = CASE
                    WHEN note IS NULL THEN $1
                    ELSE note || ' | ' || $1
                    END
                WHERE idempotency_key = $2
                """,
                note_data, idempotency_key
            )

            logger.error("Marked order for manual capital release cleanup",
                        order_id=order_id,
                        idempotency_key=idempotency_key,
                        terminal_state=terminal_state)

        except Exception as e:
            logger.error("Failed to mark failed release in database",
                        order_id=order_id,
                        error=str(e))

    def register_idempotency_key(self, order_id: int, idempotency_key: str) -> None:
        """
        Register idempotency key for order (for later capital release)

        Args:
            order_id: Order ID
            idempotency_key: Idempotency key
        """
        self.order_id_to_idemp[order_id] = idempotency_key

    async def on_cancel(self, order_id: int, reason: str = "") -> None:
        """
        Handle order cancellation

        Args:
            order_id: Broker order ID
            reason: Cancellation reason
        """
        decision_id = ""
        if self.pg:
            try:
                order_row = None
                try:
                    order_row = await self.pg.fetchone(
                        """
                        SELECT order_id::text AS order_id, note
                        FROM core.core_order
                        WHERE broker_order_id = $1
                        ORDER BY
                            CASE
                                WHEN state IN ('NEW', 'SUBMITTED', 'PARTIAL') THEN 0
                                WHEN state = 'FILLED' THEN 1
                                ELSE 2
                            END,
                            updated_at DESC
                        LIMIT 1
                        """,
                        str(order_id),
                    )
                    decision_id = self._extract_decision_id_from_note((order_row or {}).get("note"))
                except Exception:
                    decision_id = ""
                if not decision_id:
                    decision_id = await self._resolve_decision_id_from_events(
                        broker_order_id=order_id,
                    )
                if order_row and order_row.get("order_id"):
                    note_dict = _note_json_dict((order_row or {}).get("note"))
                    extras = note_dict.get("extras") if isinstance(note_dict.get("extras"), dict) else {}
                    note_data = _merge_note_json((order_row or {}).get("note"), {"cancel_reason": reason})
                    raw_expected_epoch = self._current_ib_order_identity().get("ib_connection_epoch")
                    expected_epoch = int(raw_expected_epoch) if raw_expected_epoch is not None else None
                    expected_epoch_sql = "NULL" if expected_epoch is None else str(expected_epoch)
                    await self.pg.execute(
                        f"""
                        UPDATE core.core_order
                        SET state = CASE
                                WHEN state IN ('NEW', 'SUBMITTED', 'PARTIAL') THEN 'CANCELLED'
                                ELSE state
                            END,
                            note = $1,
                            updated_at = now()
                        WHERE state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')
                          AND order_id = $2::uuid
                          AND ({expected_epoch_sql}::int IS NULL OR ib_connection_epoch IS NULL OR ib_connection_epoch <= {expected_epoch_sql}::int)
                        """,
                        note_data,
                        str(order_row.get("order_id")),
                    )
                    if bool(extras.get("flatten_action") or extras.get("close_only") or extras.get("protective_stop")):
                        controller = self.flatten_controller or FlattenController(
                            pg=self.pg,
                            manual_intervention_lock_store=self.manual_intervention_lock_store,
                        )
                        await controller.mark_order_lifecycle(
                            order_uuid=str(order_row.get("order_id")),
                            resolved_order=order_row,
                            state="cancelled",
                            payload={
                                "cancelled_at": datetime.utcnow().isoformat(),
                                "cancel_reason": reason,
                                "flatten_action": bool(extras.get("flatten_action")),
                                "close_only": bool(extras.get("close_only")),
                                "protective_stop": bool(extras.get("protective_stop")),
                            },
                            note_updates={"cancel_reason": reason},
                        )

                logger.info("Order cancelled",
                           order_id=order_id,
                           reason=reason)

            except Exception as e:
                logger.error("Failed to update cancelled order",
                            order_id=order_id,
                            error=str(e))

        await self._finalize_decision_terminal(
            decision_id=decision_id,
            terminal_state="CANCELLED",
        )
        # Release reservation
        await self._release_reservation(order_id, "CANCELLED")

    async def on_reject(self, order_id: int, reason: str = "") -> None:
        """
        Handle order rejection

        Args:
            order_id: Broker order ID
            reason: Rejection reason
        """
        decision_id = ""
        if self.pg:
            try:
                try:
                    row = await self.pg.fetchone(
                        """
                        SELECT order_id::text AS order_id, note
                        FROM core.core_order
                        WHERE broker_order_id = $1
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        str(order_id),
                    )
                    decision_id = self._extract_decision_id_from_note((row or {}).get("note"))
                except Exception:
                    decision_id = ""
                if not decision_id:
                    decision_id = await self._resolve_decision_id_from_events(
                        broker_order_id=order_id,
                    )
                note_dict = _note_json_dict((row or {}).get("note"))
                extras = note_dict.get("extras") if isinstance(note_dict.get("extras"), dict) else {}
                note_data = _merge_note_json((row or {}).get("note"), {"reject_reason": reason})

                await self.pg.execute(
                    """
                    UPDATE core.core_order
                    SET state = 'REJECTED',
                        note = $1,
                        updated_at = now()
                    WHERE broker_order_id = $2
                    """,
                    note_data, str(order_id)
                )
                if bool(extras.get("flatten_action") or extras.get("close_only") or extras.get("protective_stop")) and row:
                    controller = self.flatten_controller or FlattenController(
                        pg=self.pg,
                        manual_intervention_lock_store=self.manual_intervention_lock_store,
                    )
                    await controller.mark_order_lifecycle(
                        order_uuid=str((row or {}).get("order_id") or ""),
                        resolved_order=row,
                        state="rejected",
                        payload={
                            "rejected_at": datetime.utcnow().isoformat(),
                            "reject_reason": reason,
                            "flatten_action": bool(extras.get("flatten_action")),
                            "close_only": bool(extras.get("close_only")),
                            "protective_stop": bool(extras.get("protective_stop")),
                        },
                        note_updates={"reject_reason": reason},
                    )

                logger.info("Order rejected",
                           order_id=order_id,
                           reason=reason)

            except Exception as e:
                logger.error("Failed to update rejected order",
                            order_id=order_id,
                            error=str(e))

        await self._finalize_decision_terminal(
            decision_id=decision_id,
            terminal_state="REJECTED",
        )
        # Release reservation
        await self._release_reservation(order_id, "REJECTED")
