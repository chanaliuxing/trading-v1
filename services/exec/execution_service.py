"""
Execution Service
Bridges Decision Pipeline → Risk Approval → Order Placement → Database Persistence

Responsibilities:
- Consume approved execution plans from Decision Pipeline
- Request risk approval and capital reservation
- Place orders via SafetyKernel
- Persist order lifecycle to PostgreSQL
- Prevent ghost orders via idempotency and database tracking

Based on doc/system/1.md specification.
"""
import asyncio
import time
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass
from enum import Enum

from prometheus_client import Counter, Histogram, Gauge

from libs.logger import get_logger
from libs.db.postgres import PostgresDB
from libs.obs.audit_writer import LocalAuditWriter
from services.strategy_runtime_v2 import build_local_audit_transition_sink
from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.ibkr.ib_connection_manager import get_ib_connection_manager
from services.ibkr.ib_order_truth_schema import assert_ib_order_truth_schema
from services.risk.risk_service import RiskService
from .safety_kernel import SafetyKernel
from .models import OrderRole, PlacedOrder, cancel_request_matches_order_owner, quarantine_matches_intent
from .flip_guard import FlipGuardRegistry
from services.notifications import NotificationEvent, NotificationManager
from .netting import ExecutionRouter, NettedExecutionService
from .order_event_writer import emit_order_event
from .trace import (
    TraceContext,
    build_trace_context,
    build_idempotency_key,
    validate_trace_context,
)
from .manual_intervention_lock import ManualInterventionLockStore
from .flatten_controller import FlattenController
from services.production.scoped_tradability import (
    FlowType,
    TradabilityDecision,
    TradabilityInputs,
    TradabilityScope,
    decision_to_dict,
    evaluate_tradability,
    inputs_from_metadata,
)

logger = get_logger(__name__)


class BrokerTruthUnavailableError(RuntimeError):
    """Raised when safety sizing would otherwise rely on stale caller state."""


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _merge_note_payload(existing_note: Any, updates: Dict[str, Any]) -> str:
    payload: Dict[str, Any] = {}
    if isinstance(existing_note, str) and existing_note.strip():
        try:
            parsed = json.loads(existing_note)
            if isinstance(parsed, dict):
                payload = dict(parsed)
        except Exception:
            payload = {}
    payload.update(_json_safe(dict(updates or {})))
    return json.dumps(payload, ensure_ascii=False)


def _note_payload_dict(existing_note: Any) -> Dict[str, Any]:
    if isinstance(existing_note, dict):
        return dict(existing_note)
    if isinstance(existing_note, str) and existing_note.strip():
        try:
            parsed = json.loads(existing_note)
            if isinstance(parsed, dict):
                return dict(parsed)
        except Exception:
            return {}
    return {}


# ============================================================================
# Metrics
# ============================================================================

exec_decisions_total = Counter(
    'exec_decisions_total',
    'Total decisions received',
    ['status']
)

exec_orders_placed_total = Counter(
    'exec_orders_placed_total',
    'Total orders placed',
    ['order_type']
)

exec_risk_rejections_total = Counter(
    'exec_risk_rejections_total',
    'Risk service rejections'
)

exec_placement_errors_total = Counter(
    'exec_placement_errors_total',
    'Order placement errors'
)

exec_latency_seconds = Histogram(
    'exec_latency_seconds',
    'Execution latency',
    ['stage'],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0]
)

exec_trace_missing_total = Counter(
    'exec_trace_missing_total',
    'Missing trace fields detected at execution ingress',
    ['field']
)

exec_trace_validation_total = Counter(
    'exec_trace_validation_total',
    'Trace validation result at execution ingress',
    ['result']
)

exec_flip_guard_blocks_total = Counter(
    'exec_flip_guard_blocks_total',
    'System-level flip guard blocks',
    ['reason']
)

exec_flip_guard_exemption_used_total = Counter(
    'exec_flip_guard_exemption_used_total',
    'System-level flip guard exemption uses',
    ['strategy_id', 'reason']
)

exec_identity_validation_total = Counter(
    'exec_identity_validation_total',
    'Runtime identity validation result at execution ingress',
    ['result', 'reason']
)


# ============================================================================
# Execution Service
# ============================================================================

@dataclass
class ExecutionResult:
    """Result of order execution"""
    success: bool
    decision_id: str
    order_id: Optional[int] = None
    idempotency_key: Optional[str] = None
    reason: str = ""
    stage: str = ""  # risk/placement/database
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class ExecutionService:
    """
    Execution Service

    Flow:
    1. Receive approved PipelineResult from DecisionPipeline
    2. Request risk approval & capital reservation
    3. Persist order to database (state=NEW)
    4. Place order via SafetyKernel → IBAdapter
    5. Update database with order_id (state=SUBMITTED)
    6. Return ExecutionResult

    Ghost Order Prevention:
    - All orders persisted to database before IBKR submission
    - Idempotency keys prevent duplicates
    - Reconciliation can match orders via idempotency_key
    """

    def __init__(
        self,
        pg: PostgresDB,
        risk_service: RiskService,
        safety_kernel: SafetyKernel,
        enable_execution: bool = False,
        notification_manager: Optional[NotificationManager] = None,
        execution_router: Optional[ExecutionRouter] = None,
        netted_execution_service: Optional[NettedExecutionService] = None,
        trace_validation_enabled: bool = True,
        trace_validation_blocking: bool = False,
        order_event_enabled: bool = True,
        entry_isolation_enabled: bool = False,
        entry_isolation_symbols: Optional[list[str]] = None,
        entry_isolation_mode: str = "cross_strategy",
        manual_intervention_lock_store: Optional[ManualInterventionLockStore] = None,
        flatten_controller: Optional[FlattenController] = None,
        trading_gate_provider: Optional[Callable[[str], Any]] = None,
        tradability_inputs_provider: Optional[Callable[[Dict[str, Any]], Any]] = None,
        audit_writer: Optional[LocalAuditWriter] = None,
        flip_guard_enabled: bool = True,
        flip_guard_cooldown_sec: float = 60.0,
        flip_guard_same_bar_reversal_block: bool = True,
        flip_guard_exempt_strategy_ids: Optional[list[Any]] = None,
        flip_guard_exemptions: Optional[list[Any]] = None,
    ):
        """
        Initialize Execution Service

        Args:
            pg: PostgreSQL database
            risk_service: Risk service for approval
            safety_kernel: Safety kernel for order placement
            enable_execution: If False, dry-run mode (no actual orders)
        """
        self.pg = pg
        self.risk = risk_service
        self.kernel = safety_kernel
        self.enable_execution = enable_execution
        self.notification_manager = notification_manager
        self.execution_router = execution_router
        self.netted_execution_service = netted_execution_service
        self.trace_validation_enabled = bool(trace_validation_enabled)
        self.trace_validation_blocking = bool(trace_validation_blocking)
        self.order_event_enabled = bool(order_event_enabled)
        self.entry_isolation_enabled = bool(entry_isolation_enabled)
        self.entry_isolation_symbols = {
            str(s).strip().upper()
            for s in (entry_isolation_symbols or [])
            if str(s).strip()
        }
        mode = str(entry_isolation_mode or "cross_strategy").strip().lower()
        self.entry_isolation_mode = mode if mode in {"cross_strategy", "strategy_only"} else "cross_strategy"
        self.manual_intervention_lock_store = manual_intervention_lock_store
        self.trading_gate_provider = trading_gate_provider
        self.tradability_inputs_provider = tradability_inputs_provider
        self.flatten_controller = flatten_controller or FlattenController(
            pg=self.pg,
            manual_intervention_lock_store=self.manual_intervention_lock_store,
            transition_sink=build_local_audit_transition_sink(
                audit_writer,
                component="execution_service_flatten",
            ),
        )
        self.audit_writer = audit_writer
        self.flip_guard_enabled = bool(flip_guard_enabled)
        self.flip_guard = FlipGuardRegistry(
            cooldown_sec=flip_guard_cooldown_sec,
            same_bar_reversal_block=flip_guard_same_bar_reversal_block,
            exempt_strategy_ids=(flip_guard_exemptions if flip_guard_exemptions is not None else flip_guard_exempt_strategy_ids),
        )
        self._entry_isolation_scope_audited = False
        if self.entry_isolation_enabled and self.audit_writer is not None and not self.entry_isolation_symbols:
            self._entry_isolation_scope_audited = True
            self._emit_local_audit(
                stage="entry_isolation_scope_all_symbols",
                trace_ctx=None,
                symbol=None,
                status="enabled",
                reason="entry_isolation_symbols_empty_means_all_symbols",
                payload={"mode": self.entry_isolation_mode},
            )
        self._event_emit_tasks: set[asyncio.Task] = set()
        self._event_emit_max_pending: int = 256
        self._event_emit_overflow: deque[Dict[str, Any]] = deque(maxlen=1024)
        self._event_emit_stopping = False

        # Stats
        self.stats = {
            "decisions_received": 0,
            "orders_placed": 0,
            "risk_rejections": 0,
            "placement_errors": 0,
        }

        mode = "ENABLED" if enable_execution else "DRY-RUN"
        logger.info(f"ExecutionService initialized (mode={mode})",
                   enable_execution=enable_execution)

    @staticmethod
    def _build_intent_payload(envelope_extras: Dict[str, Any]) -> Dict[str, Any]:
        extras = dict(envelope_extras or {})
        return {
            "intent_action": extras.get("intent_action"),
            "intent_source": extras.get("intent_source"),
            "ref_px": extras.get("ref_px"),
            "ref_px_source": extras.get("ref_px_source"),
            "intent_notional_usd": extras.get("intent_notional_usd"),
            "trigger_id": extras.get("trigger_id"),
            "strategy_run_id": extras.get("strategy_run_id"),
        }

    @staticmethod
    def _ensure_flip_guard_bar_id(envelope_extras: Dict[str, Any], *, ts_us: Any = None) -> Dict[str, Any]:
        extras = dict(envelope_extras or {})
        if str(extras.get("bar_id") or "").strip():
            return extras
        try:
            ts_epoch_s = float(ts_us) / 1_000_000.0 if ts_us is not None else time.time()
        except Exception:
            ts_epoch_s = time.time()
        extras["bar_id"] = f"m1:{int(max(0.0, ts_epoch_s) // 60)}"
        return extras

    def _flip_guard_action(self, *, envelope_extras: Dict[str, Any]) -> str:
        return self.flip_guard.classify_action(extras=envelope_extras)

    @staticmethod
    def _build_owner_scoped_non_entry_order_ref(
        *,
        prefix: str,
        account_id: Any,
        strategy_instance_id: Any,
        symbol: Any,
        lot_id: Any,
        intent_id: Any,
    ) -> str:
        account = str(account_id or "").strip()
        instance = str(strategy_instance_id or "").strip()
        sym = str(symbol or "").strip().upper()
        lot = str(lot_id or "").strip()
        if not account or not instance or not sym or not lot:
            return ""
        intent = "".join(ch for ch in str(intent_id or "") if ch.isalnum())[-12:]
        suffix = intent or "manual"
        return f"{str(prefix or 'aux').strip().lower()}:{account}|{instance}|{sym}|{lot}|{suffix}"

    def _ensure_owner_scoped_non_entry_order_ref(
        self,
        *,
        strategy_id: str,
        symbol: str,
        envelope_extras: Dict[str, Any],
        idempotency_key: str,
        is_non_entry_flow: bool,
    ) -> Dict[str, Any]:
        extras = dict(envelope_extras or {})
        if not is_non_entry_flow or str(extras.get("order_ref") or "").strip():
            return extras
        if bool(extras.get("protective_stop") or extras.get("uom_protection") or extras.get("protection_key")):
            return extras
        if bool(extras.get("flatten_action")):
            prefix = "rflat" if bool(extras.get("runtime_recovery_action") or extras.get("position_safety_recovery")) else "flat"
        elif bool(extras.get("close_only") or extras.get("reduce_only")):
            prefix = "close"
        else:
            prefix = "aux"
        lot_id = extras.get("position_lot_id") or extras.get("lot_id") or extras.get("position_id")
        order_ref = self._build_owner_scoped_non_entry_order_ref(
            prefix=prefix,
            account_id=extras.get("account_id"),
            strategy_instance_id=extras.get("strategy_instance_id") or strategy_id,
            symbol=symbol,
            lot_id=lot_id,
            intent_id=extras.get("recovery_intent_id") or extras.get("intent_id") or idempotency_key,
        )
        if order_ref:
            extras["order_ref"] = order_ref[:120]
        return extras

    @classmethod
    def _inject_runtime_identity(cls, envelope_extras: Dict[str, Any]) -> Dict[str, Any]:
        extras = dict(envelope_extras or {})
        identity = cls._current_ib_order_identity()
        extras.setdefault("origin_ib_client_id", identity.get("ib_client_id"))
        extras.setdefault("origin_ib_session_role", identity.get("ib_session_role"))
        extras.setdefault("origin_ib_epoch", identity.get("ib_connection_epoch"))
        return extras

    @classmethod
    def _validate_runtime_identity(cls, envelope_extras: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
        extras = dict(envelope_extras or {})
        identity = cls._current_ib_order_identity()
        current_epoch = identity.get("ib_connection_epoch")
        current_role = str(identity.get("ib_session_role") or "")
        current_client = identity.get("ib_client_id")
        origin_epoch = extras.get("origin_ib_epoch")
        origin_role = str(extras.get("origin_ib_session_role") or "")
        origin_client = extras.get("origin_ib_client_id")
        if origin_epoch is not None and current_epoch is not None and int(origin_epoch) != int(current_epoch):
            return False, "origin_ib_epoch_mismatch", {
                "origin_ib_epoch": int(origin_epoch),
                "current_ib_epoch": int(current_epoch),
            }
        if origin_role and current_role and origin_role != current_role:
            return False, "origin_ib_session_role_mismatch", {
                "origin_ib_session_role": origin_role,
                "current_ib_session_role": current_role,
            }
        if origin_client is not None and current_client is not None and int(origin_client) != int(current_client):
            return False, "origin_ib_client_id_mismatch", {
                "origin_ib_client_id": int(origin_client),
                "current_ib_client_id": int(current_client),
            }
        return True, "ok", {
            "origin_ib_epoch": origin_epoch,
            "origin_ib_session_role": origin_role or None,
            "origin_ib_client_id": origin_client,
            "current_ib_epoch": current_epoch,
            "current_ib_session_role": current_role or None,
            "current_ib_client_id": current_client,
        }

    def _is_live_broker_execution_path(self) -> bool:
        broker = getattr(self.kernel, "broker", None)
        broker_cls = broker.__class__ if broker is not None else None
        broker_name = str(getattr(broker_cls, "__name__", "") or "")
        broker_module = str(getattr(broker_cls, "__module__", "") or "")
        return broker_name == "IBAdapter" or broker_module.endswith(".ib_adapter")

    @staticmethod
    def _order_role_from_extras(envelope_extras: Dict[str, Any], *, is_non_entry_flow: bool) -> OrderRole:
        raw_role = str((envelope_extras or {}).get("order_role") or "").strip().upper()
        if raw_role:
            return OrderRole(raw_role)
        if bool((envelope_extras or {}).get("protective_stop")):
            return OrderRole.STOP_LOSS
        if bool((envelope_extras or {}).get("flatten_action")):
            return OrderRole.STRATEGY_FLATTEN
        if bool((envelope_extras or {}).get("close_only")) or is_non_entry_flow:
            return OrderRole.EXIT
        return OrderRole.ENTRY

    @staticmethod
    def _flow_type_from_order_role(order_role: OrderRole) -> FlowType:
        try:
            return FlowType(order_role.value)
        except Exception:
            if order_role == OrderRole.ACCOUNT_EMERGENCY_FLATTEN:
                return FlowType.ACCOUNT_EMERGENCY_FLATTEN
            return FlowType.ENTRY

    def _tradability_inputs_for_execution(
        self,
        *,
        decision_id: str,
        strategy_id: str,
        symbol: str,
        envelope_extras: Dict[str, Any],
        metadata: Dict[str, Any],
        scope: TradabilityScope,
    ) -> TradabilityInputs:
        context = {
            "decision_id": decision_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "scope": scope,
            "envelope_extras": dict(envelope_extras or {}),
            "metadata": dict(metadata or {}),
        }
        provider = self.tradability_inputs_provider
        if callable(provider):
            provided = provider(context)
            if isinstance(provided, TradabilityInputs):
                return provided
            if isinstance(provided, dict):
                return inputs_from_metadata(provided)
        return inputs_from_metadata(metadata)

    def _evaluate_scoped_tradability_for_execution(
        self,
        *,
        decision_id: str,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        envelope_extras: Dict[str, Any],
        metadata: Dict[str, Any],
        is_non_entry_flow: bool,
    ) -> TradabilityDecision:
        role = self._order_role_from_extras(envelope_extras, is_non_entry_flow=is_non_entry_flow)
        flow_type = self._flow_type_from_order_role(role)
        if bool(envelope_extras.get("protective_repair")):
            flow_type = FlowType.PROTECTIVE_REPAIR
        scope = TradabilityScope(
            account_id=str(envelope_extras.get("account_id") or ""),
            strategy_id=str(strategy_id or ""),
            strategy_instance_id=str(envelope_extras.get("strategy_instance_id") or strategy_id or ""),
            symbol=str(symbol or "").strip().upper(),
            asset_type=str(envelope_extras.get("instrument_type") or envelope_extras.get("asset_type") or "") or None,
            session=str(envelope_extras.get("session") or (metadata or {}).get("session") or "RTH").strip().upper() or "RTH",
            flow_type=flow_type,
            order_role=role.value,
            lot_id=str(envelope_extras.get("position_lot_id") or envelope_extras.get("lot_id") or "") or None,
        )
        inputs = self._tradability_inputs_for_execution(
            decision_id=decision_id,
            strategy_id=strategy_id,
            symbol=symbol,
            envelope_extras=envelope_extras,
            metadata=metadata,
            scope=scope,
        )
        return evaluate_tradability(scope, inputs)

    def _validate_live_entry_owner_contract(
        self,
        *,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        idempotency_key: str,
        envelope_extras: Dict[str, Any],
        is_non_entry_flow: bool,
    ) -> tuple[bool, str, Dict[str, Any]]:
        if not (self.enable_execution and self._is_live_broker_execution_path()):
            return True, "ok", {}
        try:
            order_role = self._order_role_from_extras(envelope_extras, is_non_entry_flow=is_non_entry_flow)
        except Exception:
            return False, "invalid_order_role", {"order_role": envelope_extras.get("order_role")}
        if is_non_entry_flow and order_role != OrderRole.ENTRY:
            return True, "ok", {}
        if order_role != OrderRole.ENTRY:
            return False, "live_entry_order_role_mismatch", {"order_role": str(order_role.value)}

        missing = [
            field_name
            for field_name in ("account_id", "strategy_instance_id")
            if not str(envelope_extras.get(field_name) or "").strip()
        ]
        if not str(idempotency_key or "").strip():
            missing.append("intent_id")
        if missing:
            return False, "live_entry_owner_identity_missing", {"missing_owner_fields": missing}
        if bool(envelope_extras.get("close_only") or envelope_extras.get("reduce_only")):
            return False, "entry_cannot_be_close_or_reduce_only", {
                "close_only": bool(envelope_extras.get("close_only")),
                "reduce_only": bool(envelope_extras.get("reduce_only")),
            }

        side = str(getattr(plan, "side", "") or "").strip().upper()
        explicit_policy = bool(
            envelope_extras.get("allow_reverse")
            or envelope_extras.get("allow_opposite_direction_same_symbol")
            or envelope_extras.get("execution_arbiter")
            or envelope_extras.get("virtual_hedge")
        )
        if (
            side == "SELL_SHORT"
            and bool(envelope_extras.get("same_symbol_multi_strategy_safe"))
            and not bool(envelope_extras.get("execution_arbiter") or envelope_extras.get("virtual_hedge"))
        ):
            return False, "same_symbol_multi_strategy_safe_requires_execution_arbiter", {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": side,
            }
        if side == "SELL_SHORT" and not explicit_policy:
            return False, "opposite_direction_same_symbol_requires_explicit_policy", {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": side,
            }
        return True, "ok", {}

    async def _enforce_flip_guard(
        self,
        *,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        decision_id: str,
        idempotency_key: str,
        envelope_extras: Dict[str, Any],
        trace_ctx: Optional[TraceContext],
    ) -> Optional[ExecutionResult]:
        if (not self.enable_execution) or (not self.flip_guard_enabled):
            return None
        if self._flip_guard_action(envelope_extras=envelope_extras) != "entry":
            return None
        try:
            await self.flip_guard.hydrate_from_pg(self.pg)
        except Exception as exc:
            logger.warning("flip_guard_pg_hydrate_failed", error=str(exc))
        verdict = self.flip_guard.check_open_allowed(
            strategy_id=strategy_id,
            symbol=symbol,
            side=plan.side,
            extras=envelope_extras,
        )
        if verdict.allowed:
            if verdict.reason == "exempt_strategy":
                exec_flip_guard_exemption_used_total.labels(
                    strategy_id=str(strategy_id or ""),
                    reason=str((verdict.metadata or {}).get("reason") or "configured_exemption"),
                ).inc()
                await self._emit_event(
                    event_type="intent_allowed_with_exemption",
                    trace_ctx=trace_ctx,
                    symbol=symbol,
                    intent_id=idempotency_key,
                    payload={"reason": verdict.reason, "source": "flip_guard", **dict(verdict.metadata or {})},
                )
                self._emit_local_audit(
                    stage="intent_allowed_with_exemption",
                    trace_ctx=trace_ctx,
                    symbol=symbol,
                    intent_id=idempotency_key,
                    status="allowed",
                    reason="flip_guard_exemption",
                    payload={"source": "flip_guard", **dict(verdict.metadata or {})},
                )
            return None
        exec_flip_guard_blocks_total.labels(reason=verdict.reason).inc()
        await self._update_order_rejected(
            idempotency_key=idempotency_key,
            reason=verdict.reason,
            envelope_extras=envelope_extras,
        )
        await self._emit_event(
            event_type="intent_blocked",
            trace_ctx=trace_ctx,
            symbol=symbol,
            intent_id=idempotency_key,
            payload={"reason": verdict.reason, "source": "flip_guard", **dict(verdict.metadata or {})},
        )
        self._emit_local_audit(
            stage="intent_blocked",
            trace_ctx=trace_ctx,
            symbol=symbol,
            intent_id=idempotency_key,
            status="blocked",
            reason=verdict.reason,
            payload={
                "source": "flip_guard",
                "order_type": plan.order_type,
                "side": plan.side,
                "qty": plan.qty,
                **dict(verdict.metadata or {}),
            },
        )
        await self.risk.release(idempotency_key)
        return ExecutionResult(
            success=False,
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            reason=verdict.reason,
            stage="validation",
            metadata=dict(verdict.metadata or {}),
        )

    @staticmethod
    def _effective_entry_isolation_mode(
        *,
        configured_mode: str,
        symbol: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
    ) -> str:
        mode = str(configured_mode or "cross_strategy").strip().lower()
        if mode not in {"cross_strategy", "strategy_only"}:
            mode = "cross_strategy"
        extras = dict(envelope_extras or {})
        symbol_u = str(symbol or "").strip().upper()
        is_option = bool(
            str(extras.get("instrument_type") or "").strip().upper() == "OPTION"
            or symbol_u.startswith("OPT:")
        )
        allow_same_symbol_cross_strategy = bool(
            extras.get("allow_same_option_symbol_cross_strategy")
            or extras.get("allow_cross_strategy_same_symbol")
        )
        if is_option and allow_same_symbol_cross_strategy:
            return "strategy_only"
        return mode

    async def execute(self, result: PipelineResult) -> ExecutionResult:
        """
        Execute approved trading decision

        Args:
            result: Pipeline result with APPROVED status

        Returns:
            ExecutionResult
        """
        start_time = time.time()
        self.stats["decisions_received"] += 1
        exec_decisions_total.labels(status=result.status).inc()

        decision_id = result.decision_id
        strategy_id = result.strategy_id
        symbol = result.symbol

        # Only execute APPROVED decisions
        if result.status != "APPROVED":
            logger.warning("Cannot execute non-approved decision",
                          decision_id=decision_id,
                          status=result.status)
            return ExecutionResult(
                success=False,
                decision_id=decision_id,
                reason=f"Status {result.status} not executable",
                stage="validation"
            )

        if not result.plan:
            logger.error("APPROVED decision missing execution plan",
                        decision_id=decision_id)
            return ExecutionResult(
                success=False,
                decision_id=decision_id,
                reason="Missing execution plan",
                stage="validation"
            )

        plan: ExecPlan = result.plan
        if float(plan.qty) <= 0:
            logger.info(
                "Skipping execution for non-positive qty",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                side=plan.side,
                qty=plan.qty,
            )
            return ExecutionResult(
                success=True,
                decision_id=decision_id,
                reason="No-op (qty<=0)",
                stage="validation",
            )

        envelope_extras: Dict[str, Any] = {}
        try:
            envelope_extras = dict((result.metadata or {}).get("envelope_extras") or {})
        except Exception:
            envelope_extras = {}
        envelope_extras = self._inject_runtime_identity(envelope_extras)
        identity_ok, identity_reason, identity_meta = self._validate_runtime_identity(envelope_extras)
        if identity_ok:
            exec_identity_validation_total.labels(result="ok", reason="ok").inc()
        else:
            exec_identity_validation_total.labels(result="rejected", reason=identity_reason).inc()
            logger.warning(
                "execution_runtime_identity_rejected",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                reason=identity_reason,
                **identity_meta,
            )
            return ExecutionResult(
                success=False,
                decision_id=decision_id,
                reason=identity_reason,
                stage="validation",
                metadata=identity_meta,
            )
        if bool(envelope_extras.get("protective_stop") or envelope_extras.get("uom_protection") or envelope_extras.get("protection_key")):
            try:
                self.kernel.ensure_non_oco_protection_metadata(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    side=plan.side,
                    qty=plan.qty,
                    desired_price=plan.stop_px if plan.stop_px is not None else plan.limit_px,
                    envelope_extras=envelope_extras,
                )
            except Exception as exc:
                logger.warning(
                    "non_oco_protection_metadata_prepare_failed",
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
        safety_action = self._classify_safety_action(envelope_extras)
        manual_intervention = bool(envelope_extras.get("manual_intervention"))
        manual_lock_ttl_sec = int(envelope_extras.get("manual_lock_ttl_sec") or 1800)
        is_non_entry_flow = bool(
            envelope_extras.get("flatten_action")
            or envelope_extras.get("protective_stop")
            or envelope_extras.get("close_only")
        )
        tradability_decision = self._evaluate_scoped_tradability_for_execution(
            decision_id=decision_id,
            strategy_id=strategy_id,
            symbol=symbol,
            plan=plan,
            envelope_extras=envelope_extras,
            metadata=dict(result.metadata or {}),
            is_non_entry_flow=is_non_entry_flow,
        )
        tradability_payload = decision_to_dict(tradability_decision)
        envelope_extras["tradability_decision"] = tradability_payload
        if not tradability_decision.allow:
            reason = f"scoped_tradability_denied:{tradability_decision.reason}"
            trace_for_tradability = build_trace_context(
                decision_id=decision_id,
                strategy_id=strategy_id,
                metadata=result.metadata,
                envelope_extras=envelope_extras,
            )
            if self.order_event_enabled:
                await self._emit_event(
                    event_type="intent_blocked",
                    trace_ctx=trace_for_tradability,
                    symbol=symbol,
                    intent_id=None,
                    payload={"reason": reason, "tradability_decision": tradability_payload},
                )
            self._emit_local_audit(
                stage="intent_blocked",
                trace_ctx=trace_for_tradability,
                symbol=symbol,
                status="blocked",
                reason=reason,
                payload={"source": "TradabilityService", "tradability_decision": tradability_payload},
            )
            return ExecutionResult(
                success=False,
                decision_id=decision_id,
                reason=reason,
                stage="validation",
                metadata={"tradability_decision": tradability_payload},
            )
        if (not is_non_entry_flow) and self.manual_intervention_lock_store is not None:
            active_lock = await self._get_manual_intervention_lock(
                strategy_id=strategy_id,
                symbol=symbol,
                strategy_instance_id=str(envelope_extras.get("strategy_instance_id") or ""),
            )
            if active_lock:
                reason = "manual_intervention_lock_active"
                lock_meta = {
                    "lock_reason": str(active_lock.get("reason") or "manual_flatten"),
                    "lock_created_ts": float(active_lock.get("created_ts") or 0.0),
                    "lock_expire_ts": float(active_lock.get("expire_ts") or 0.0),
                    "lock_context": dict(active_lock.get("context") or {}),
                }
                await self._emit_event(
                    event_type="intent_blocked",
                    trace_ctx=build_trace_context(
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        metadata=result.metadata,
                        envelope_extras=envelope_extras,
                    ),
                    symbol=symbol,
                    intent_id=None,
                    payload={"reason": reason, **lock_meta},
                )
                self._emit_local_audit(
                    stage="intent_blocked",
                    trace_ctx=build_trace_context(
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        metadata=result.metadata,
                        envelope_extras=envelope_extras,
                    ),
                    symbol=symbol,
                    status="blocked",
                    reason=reason,
                    payload=lock_meta,
                )
                return ExecutionResult(
                    success=False,
                    decision_id=decision_id,
                    reason=reason,
                    stage="validation",
                    metadata=lock_meta,
                )

        if not is_non_entry_flow:
            quarantine_active = await self._has_unresolved_symbol_quarantine(
                symbol=symbol,
                account_id=envelope_extras.get("account_id"),
                strategy_id=strategy_id,
                strategy_instance_id=envelope_extras.get("strategy_instance_id"),
                intent_id=envelope_extras.get("intent_id"),
                client_order_id=envelope_extras.get("client_order_id"),
                broker_order_id=envelope_extras.get("broker_order_id"),
                ib_perm_id=envelope_extras.get("ib_perm_id") or envelope_extras.get("perm_id"),
            )
            gate_ok, gate_reason, gate_meta = await self._evaluate_trading_gate(
                symbol=symbol,
                symbol_quarantine_active=quarantine_active,
            )
            if not gate_ok:
                trace_for_gate = build_trace_context(
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    metadata=result.metadata,
                    envelope_extras=envelope_extras,
                )
                await self._emit_event(
                    event_type="intent_blocked",
                    trace_ctx=trace_for_gate,
                    symbol=symbol,
                    intent_id=None,
                    payload={"reason": gate_reason, "gate": gate_meta},
                )
                self._emit_local_audit(
                    stage="intent_blocked",
                    trace_ctx=trace_for_gate,
                    symbol=symbol,
                    status="blocked",
                    reason=gate_reason,
                    payload={"gate": gate_meta},
                )
                return ExecutionResult(
                    success=False,
                    decision_id=decision_id,
                    reason=gate_reason,
                    stage="validation",
                    metadata={"gate": gate_meta},
                )

        trace_ctx = build_trace_context(
            decision_id=decision_id,
            strategy_id=strategy_id,
            metadata=result.metadata,
            envelope_extras=envelope_extras,
        )
        missing_trace = validate_trace_context(trace_ctx)
        synthetic_trace = list(trace_ctx.synthetic_fields or [])
        if self.trace_validation_enabled and (missing_trace or synthetic_trace):
            validation_result = "warn"
            if self.trace_validation_blocking:
                validation_result = "blocked"
            exec_trace_validation_total.labels(result=validation_result).inc()
            for field_name in (missing_trace + synthetic_trace):
                exec_trace_missing_total.labels(field=field_name).inc()
            logger.warning(
                "execution_trace_validation_missing",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                trace=trace_ctx.to_dict(),
                missing=missing_trace,
                synthetic=synthetic_trace,
                blocking=self.trace_validation_blocking,
            )
            if self.trace_validation_blocking and (missing_trace or synthetic_trace):
                return ExecutionResult(
                    success=False,
                    decision_id=decision_id,
                    reason=f"trace_validation_failed:{','.join(missing_trace + synthetic_trace)}",
                    stage="validation",
                )
        elif self.trace_validation_enabled:
            exec_trace_validation_total.labels(result="ok").inc()
            logger.info(
                "execution_trace_validation_ok",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                trace=trace_ctx.to_dict(),
            )
        else:
            exec_trace_validation_total.labels(result="disabled").inc()
        # Deterministic idempotency key: prevents duplicate live orders on retries.
        explicit_intent_id = str(envelope_extras.get("intent_id") or "").strip()
        idempotency_key = explicit_intent_id or build_idempotency_key(
            strategy_id=trace_ctx.strategy_id,
            strategy_run_id=trace_ctx.strategy_run_id,
            decision_id=decision_id,
            symbol=symbol,
            target_qty=plan.qty if plan.side == "BUY" else -plan.qty,
            ts_us=(result.metadata or {}).get("ts_bar_end_us"),
        )
        trace_ctx.intent_id = idempotency_key
        envelope_extras = self._ensure_owner_scoped_non_entry_order_ref(
            strategy_id=strategy_id,
            symbol=symbol,
            envelope_extras=envelope_extras,
            idempotency_key=idempotency_key,
            is_non_entry_flow=is_non_entry_flow,
        )

        owner_ok, owner_reason, owner_meta = self._validate_live_entry_owner_contract(
            strategy_id=strategy_id,
            symbol=symbol,
            plan=plan,
            idempotency_key=idempotency_key,
            envelope_extras=envelope_extras,
            is_non_entry_flow=is_non_entry_flow,
        )
        if not owner_ok:
            logger.warning(
                "live_entry_owner_identity_rejected",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                reason=owner_reason,
                owner_guard=owner_meta,
            )
            await self._emit_event(
                event_type="intent_blocked",
                trace_ctx=trace_ctx,
                symbol=symbol,
                intent_id=idempotency_key,
                payload={"reason": owner_reason, **owner_meta},
            )
            self._emit_local_audit(
                stage="intent_blocked",
                trace_ctx=trace_ctx,
                symbol=symbol,
                intent_id=idempotency_key,
                status="blocked",
                reason=owner_reason,
                payload={"source": "owner_identity_guard", **owner_meta},
            )
            return ExecutionResult(
                success=False,
                decision_id=decision_id,
                idempotency_key=idempotency_key,
                reason=owner_reason,
                stage="validation",
                metadata=owner_meta,
            )

        intent_payload = self._build_intent_payload(envelope_extras)
        effective_entry_isolation_mode = self._effective_entry_isolation_mode(
            configured_mode=self.entry_isolation_mode,
            symbol=str(symbol or ""),
            envelope_extras=envelope_extras,
        )
        self._emit_local_audit(
            stage="intent_requested",
            trace_ctx=trace_ctx,
            symbol=symbol,
            intent_id=idempotency_key,
            status="requested",
            payload={
                "order_type": plan.order_type,
                "side": plan.side,
                "qty": plan.qty,
                "intent": intent_payload,
            },
        )
        await self._emit_event(
            event_type="intent_received",
            trace_ctx=trace_ctx,
            symbol=symbol,
            intent_id=idempotency_key,
            payload={
                "status": result.status,
                "order_type": getattr(plan, "order_type", None),
                "side": getattr(plan, "side", None),
                "qty": getattr(plan, "qty", None),
                "intent": intent_payload,
            },
        )

        # Optional routing: direct (legacy default) vs netted mode.
        if self.execution_router is not None:
            try:
                requested_mode = envelope_extras.get("execution_mode")
                route = self.execution_router.decide(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    requested_mode=requested_mode,
                )
                # Safety guard: flatten/protective flows remain direct.
                if envelope_extras.get("flatten_action") or envelope_extras.get("protective_stop") or envelope_extras.get("close_only"):
                    route_mode = "direct"
                else:
                    route_mode = route.mode
                if route_mode == "netted" and self.netted_execution_service is not None:
                    outcome = await self.netted_execution_service.execute(
                        result=result,
                        envelope_extras=envelope_extras,
                        trace_ctx=trace_ctx,
                        order_event_enabled=self.order_event_enabled,
                    )
                    return ExecutionResult(
                        success=bool(outcome.success),
                        decision_id=outcome.decision_id,
                        order_id=outcome.order_id,
                        idempotency_key=outcome.idempotency_key,
                        reason=str(outcome.reason),
                        stage=str(outcome.stage),
                        metadata=dict(outcome.metadata or {}),
                    )
            except Exception as exc:
                logger.warning(
                    "execution_router_failed_fallback_direct",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
        # Entry isolation guard:
        # prevent multiple strategies from opening same symbol simultaneously on one account.
        if self.enable_execution and self.entry_isolation_enabled:
            symbol_u = str(symbol or "").strip().upper()
            symbol_guard_enabled = (not self.entry_isolation_symbols) or (symbol_u in self.entry_isolation_symbols)
            if (not is_non_entry_flow) and symbol_guard_enabled:
                ok, guard_reason, guard_meta = await self._enforce_entry_isolation(
                    strategy_id=strategy_id,
                    symbol=symbol_u,
                    trace_ctx=trace_ctx,
                    entry_isolation_mode_override=effective_entry_isolation_mode,
                )
                if not ok:
                    logger.warning(
                        "entry_isolation_blocked",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol_u,
                        reason=guard_reason,
                        **guard_meta,
                    )
                    await self._emit_event(
                        event_type="intent_blocked",
                        trace_ctx=trace_ctx,
                        symbol=symbol_u,
                        intent_id=idempotency_key,
                        payload={
                            "reason": guard_reason,
                            **guard_meta,
                        },
                    )
                    return ExecutionResult(
                        success=False,
                        decision_id=decision_id,
                        idempotency_key=idempotency_key,
                        reason=guard_reason,
                        stage="validation",
                        metadata=guard_meta,
                    )

        try:
            # Stage 1: Risk Approval
            with exec_latency_seconds.labels(stage="risk_approval").time():
                risk_decision = await self.risk.approve(
                    envelope=self._plan_to_envelope(
                        plan,
                        symbol,
                        strategy_id,
                        decision_id,
                        envelope_extras=envelope_extras,
                    ),
                    idempotency_key=idempotency_key
                )

            if not risk_decision.approved:
                self.stats["risk_rejections"] += 1
                exec_risk_rejections_total.inc()

                logger.warning("Risk service rejected decision",
                              decision_id=decision_id,
                              reason=risk_decision.reason,
                              hits=risk_decision.hits)
                logger.warning(
                    "exec_gate",
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    stage="risk",
                    reason=risk_decision.reason,
                    hits=risk_decision.hits,
                    trace_id=getattr(trace_ctx, "trace_id", None),
                    trigger_id=getattr(trace_ctx, "trigger_id", None),
                    intent_id=idempotency_key,
                )

                await self._emit_event(
                    event_type="risk_checked",
                    trace_ctx=trace_ctx,
                    symbol=symbol,
                    intent_id=idempotency_key,
                    payload={
                        "approved": False,
                        "reason": risk_decision.reason,
                        "hits": risk_decision.hits,
                        "intent": intent_payload,
                    },
                )
                return ExecutionResult(
                    success=False,
                    decision_id=decision_id,
                    idempotency_key=idempotency_key,
                    reason=risk_decision.reason,
                    stage="risk",
                    metadata={"hits": risk_decision.hits}
                )

            # Optional: cancel outstanding orders before flatten/protective operations.
            if self.enable_execution and envelope_extras.get("flatten_action"):
                await self._cancel_open_orders_for_symbol(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    reason=f"pre_flatten:{decision_id}",
                    account_id=envelope_extras.get("account_id"),
                    strategy_instance_id=envelope_extras.get("strategy_instance_id"),
                    intent_id=envelope_extras.get("intent_id"),
                    client_order_id=envelope_extras.get("client_order_id"),
                    lot_id=envelope_extras.get("position_lot_id") or envelope_extras.get("lot_id"),
                )
            if self.enable_execution and envelope_extras.get("protective_stop"):
                await self._cancel_open_orders_for_symbol(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    reason=f"pre_protective_stop:{decision_id}",
                    only_order_types={"STP", "STP_LMT"},
                    account_id=envelope_extras.get("account_id"),
                    strategy_instance_id=envelope_extras.get("strategy_instance_id"),
                    intent_id=envelope_extras.get("intent_id"),
                    client_order_id=envelope_extras.get("client_order_id"),
                    lot_id=envelope_extras.get("position_lot_id") or envelope_extras.get("lot_id"),
                )

            # Stage 2: Persist to Database (NEW state)
            with exec_latency_seconds.labels(stage="db_persist_new").time():
                # Second-line entry isolation: atomically guard + insert under an advisory xact lock.
                is_non_entry_flow = bool(
                    envelope_extras.get("flatten_action")
                    or envelope_extras.get("protective_stop")
                    or envelope_extras.get("close_only")
                )
                symbol_u = str(symbol or "").strip().upper()
                symbol_guard_enabled = (not self.entry_isolation_symbols) or (symbol_u in self.entry_isolation_symbols)
                can_atomic = hasattr(self.pg, "acquire_conn")
                if self.enable_execution and self.entry_isolation_enabled and (not is_non_entry_flow) and symbol_guard_enabled and can_atomic:
                    ok, guard_reason, guard_meta = await self._persist_order_new_with_entry_lock(
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol_u,
                        plan=plan,
                        idempotency_key=idempotency_key,
                        envelope_extras=envelope_extras,
                        trace_ctx=trace_ctx,
                        entry_isolation_mode_override=effective_entry_isolation_mode,
                    )
                    if not ok:
                        await self._emit_event(
                            event_type="intent_blocked",
                            trace_ctx=trace_ctx,
                            symbol=symbol_u,
                            intent_id=idempotency_key,
                            payload={"reason": guard_reason, **guard_meta},
                        )
                        await self.risk.release(idempotency_key)
                        return ExecutionResult(
                            success=False,
                            decision_id=decision_id,
                            idempotency_key=idempotency_key,
                            reason=guard_reason,
                            stage="validation",
                            metadata=guard_meta,
                        )
                else:
                    await self._persist_order_new(
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol_u if symbol_u else symbol,
                        plan=plan,
                        idempotency_key=idempotency_key,
                        envelope_extras=envelope_extras,
                        trace_ctx=trace_ctx,
                    )
            await self._emit_event(
                event_type="risk_checked",
                trace_ctx=trace_ctx,
                symbol=symbol,
                intent_id=idempotency_key,
                payload={
                    "approved": True,
                    "intent": intent_payload,
                },
            )
            self._emit_local_audit(
                stage="risk_checked",
                trace_ctx=trace_ctx,
                symbol=symbol,
                intent_id=idempotency_key,
                status="approved",
                payload={
                    "approved": True,
                    "order_type": plan.order_type,
                    "side": plan.side,
                    "qty": plan.qty,
                },
            )

            # Stage 2.5: Close-only safety guard (prevents accidental reverse positions).
            if self.enable_execution and bool(envelope_extras.get("close_only") or envelope_extras.get("flatten_action") or envelope_extras.get("protective_stop")):
                ok, guard_reason = await self._enforce_close_only_guard(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    plan=plan,
                    envelope_extras=envelope_extras,
                )
                if not ok:
                    await self._update_order_rejected(
                        idempotency_key=idempotency_key,
                        reason=guard_reason,
                        envelope_extras=envelope_extras,
                    )
                    await self._emit_event(
                        event_type="order_rejected",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        intent_id=idempotency_key,
                        payload={
                            "reason": guard_reason,
                            "safety_action": safety_action,
                            "order_type": plan.order_type,
                            "side": plan.side,
                            "qty": plan.qty,
                        },
                    )
                    self._emit_local_audit(
                        stage="order_rejected",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        intent_id=idempotency_key,
                        status="rejected",
                        reason=guard_reason,
                        payload={
                            "source": "close_only_guard",
                            "order_type": plan.order_type,
                            "side": plan.side,
                            "qty": plan.qty,
                        },
                    )
                    if safety_action is not None:
                        logger.warning(
                            "safety_order_rejected_pre_placement",
                            decision_id=decision_id,
                            strategy_id=strategy_id,
                            symbol=symbol,
                            safety_action=safety_action,
                            reason=guard_reason,
                            side=plan.side,
                            qty=plan.qty,
                        )
                    await self.risk.release(idempotency_key)
                    return ExecutionResult(
                        success=False,
                        decision_id=decision_id,
                        idempotency_key=idempotency_key,
                        reason=guard_reason,
                        stage="validation",
                    )
                ok, guard_reason = self._enforce_manual_flatten_limit_guard(
                    plan=plan,
                    envelope_extras=envelope_extras,
                )
                if not ok:
                    await self._update_order_rejected(
                        idempotency_key=idempotency_key,
                        reason=guard_reason,
                        envelope_extras=envelope_extras,
                    )
                    await self._emit_event(
                        event_type="order_rejected",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        intent_id=idempotency_key,
                        payload={
                            "reason": guard_reason,
                            "safety_action": safety_action,
                            "order_type": plan.order_type,
                            "side": plan.side,
                            "qty": plan.qty,
                        },
                    )
                    self._emit_local_audit(
                        stage="order_rejected",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        intent_id=idempotency_key,
                        status="rejected",
                        reason=guard_reason,
                        payload={
                            "source": "manual_flatten_limit_guard",
                            "order_type": plan.order_type,
                            "side": plan.side,
                            "qty": plan.qty,
                        },
                    )
                    logger.error(
                        "manual_flatten_limit_guard_rejected",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        reason=guard_reason,
                        side=plan.side,
                        qty=plan.qty,
                        order_type=plan.order_type,
                        limit_px=plan.limit_px,
                        extras=envelope_extras,
                    )
                    await self.risk.release(idempotency_key)
                    return ExecutionResult(
                        success=False,
                        decision_id=decision_id,
                        idempotency_key=idempotency_key,
                        reason=guard_reason,
                        stage="validation",
                    )

            # Stage 3: Place Order (if enabled)
            if self.enable_execution:
                flip_guard_extras = self._ensure_flip_guard_bar_id(
                    dict(envelope_extras or {}),
                    ts_us=(result.metadata or {}).get("ts_bar_end_us"),
                )
                flip_guard_result = await self._enforce_flip_guard(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    plan=plan,
                    decision_id=decision_id,
                    idempotency_key=idempotency_key,
                    envelope_extras=flip_guard_extras,
                    trace_ctx=trace_ctx,
                )
                if flip_guard_result is not None:
                    return flip_guard_result
                self._emit_local_audit(
                    stage="broker_submit_requested",
                    trace_ctx=trace_ctx,
                    symbol=symbol,
                    intent_id=idempotency_key,
                    status="requested",
                    payload={
                        "order_type": plan.order_type,
                        "side": plan.side,
                        "qty": plan.qty,
                    },
                )
                with exec_latency_seconds.labels(stage="order_placement").time():
                    placed_order = await self._place_order(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        plan=plan,
                        envelope_extras=envelope_extras,
                    )

                if not placed_order:
                    self.stats["placement_errors"] += 1
                    exec_placement_errors_total.inc()

                    # Mark order as REJECTED in database
                    await self._update_order_rejected(
                        idempotency_key=idempotency_key,
                        reason="Broker placement failed",
                        envelope_extras=envelope_extras,
                    )
                    await self._emit_event(
                        event_type="order_rejected",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        intent_id=idempotency_key,
                        payload={
                            "reason": "Broker placement failed",
                            "safety_action": safety_action,
                            "order_type": plan.order_type,
                            "side": plan.side,
                            "qty": plan.qty,
                        },
                    )
                    self._emit_local_audit(
                        stage="order_rejected",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        intent_id=idempotency_key,
                        status="rejected",
                        reason="Broker placement failed",
                        payload={
                            "source": "broker_submit",
                            "order_type": plan.order_type,
                            "side": plan.side,
                            "qty": plan.qty,
                        },
                    )
                    logger.error(
                        "exec_gate",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        stage="ib",
                        reason="broker_placement_failed",
                        trace_id=getattr(trace_ctx, "trace_id", None),
                        trigger_id=getattr(trace_ctx, "trigger_id", None),
                        intent_id=idempotency_key,
                    )
                    if safety_action is not None:
                        logger.error(
                            "safety_order_broker_submit_failed",
                            decision_id=decision_id,
                            strategy_id=strategy_id,
                            symbol=symbol,
                            safety_action=safety_action,
                            side=plan.side,
                            qty=plan.qty,
                        )

                    # Release capital reservation
                    await self.risk.release(idempotency_key)

                    return ExecutionResult(
                        success=False,
                        decision_id=decision_id,
                        idempotency_key=idempotency_key,
                        reason="Order placement failed",
                        stage="placement"
                    )
                try:
                    flip_guard_record = self.flip_guard.record_submission(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        side=plan.side,
                        extras=flip_guard_extras,
                    )
                    await self.flip_guard.persist_submission_to_pg(self.pg, flip_guard_record)
                except Exception:
                    logger.warning(
                        "flip_guard_record_submission_failed",
                        strategy_id=strategy_id,
                        symbol=symbol,
                    )

                # Stage 4: Update Database with order_id (SUBMITTED state)
                try:
                    with exec_latency_seconds.labels(stage="db_persist_submitted").time():
                        await self._update_order_submitted(
                            idempotency_key=idempotency_key,
                            order_id=placed_order.order_id,
                            broker_order_id=placed_order.broker_order_id,
                            perm_id=placed_order.perm_id,
                            order_ref=placed_order.order_ref,
                            envelope_extras=envelope_extras,
                        )
                        if bool(
                            envelope_extras.get("protective_stop")
                            or envelope_extras.get("uom_protection")
                            or envelope_extras.get("protection_key")
                        ):
                            await self.kernel.upsert_non_oco_protection_registry(
                                strategy_id=strategy_id,
                                symbol=symbol,
                                side=plan.side,
                                qty=plan.qty,
                                desired_price=plan.stop_px if plan.stop_px is not None else plan.limit_px,
                                order=placed_order,
                                envelope_extras=envelope_extras,
                            )
                        try:
                            self.kernel.register_idempotency_key(placed_order.order_id, idempotency_key)
                        except Exception:
                            pass
                    self._emit_local_audit(
                        stage="broker_submit_ack",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        intent_id=idempotency_key,
                        broker_order_id=str(getattr(placed_order, "order_id", "") or ""),
                        perm_id=getattr(placed_order, "perm_id", None),
                        status="submitted",
                        payload={"order_ref": getattr(placed_order, "order_ref", None)},
                    )
                except Exception as exc:
                    # CRITICAL: if we placed an order at broker but failed to persist broker IDs,
                    # reconciler will classify it as an orphan. Prefer immediate broker cancel over
                    # leaving an untracked working order.
                    logger.error(
                        "db_persist_submitted_failed_cancel_broker",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        broker_order_id=getattr(placed_order, "order_id", None),
                        error=str(exc),
                        exc_info=True,
                    )
                    broker = getattr(self.kernel, "broker", None)
                    if broker is not None and hasattr(broker, "cancel"):
                        try:
                            await broker.cancel(int(placed_order.order_id))
                        except Exception as cancel_exc:
                            logger.warning(
                                "db_persist_submitted_broker_cancel_failed",
                                decision_id=decision_id,
                                strategy_id=strategy_id,
                                symbol=symbol,
                                broker_order_id=getattr(placed_order, "order_id", None),
                                error=str(cancel_exc),
                            )
                    try:
                        await self._update_order_rejected(
                            idempotency_key=idempotency_key,
                            reason="db_persist_submitted_failed_cancelled",
                            envelope_extras=envelope_extras,
                        )
                    except Exception:
                        pass
                    self._emit_local_audit(
                        stage="db_persist_failed",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        intent_id=idempotency_key,
                        broker_order_id=str(getattr(placed_order, "order_id", "") or ""),
                        perm_id=getattr(placed_order, "perm_id", None),
                        status="failed",
                        reason="db_persist_submitted_failed",
                        payload={"error": str(exc)},
                    )
                    await self.risk.release(idempotency_key)
                    return ExecutionResult(
                        success=False,
                        decision_id=decision_id,
                        idempotency_key=idempotency_key,
                        reason="db_persist_submitted_failed",
                        stage="db",
                    )
                order_identity = await self._get_order_identity(idempotency_key=idempotency_key)
                await self._emit_event(
                    event_type="order_submitted",
                    trace_ctx=trace_ctx,
                    symbol=symbol,
                    intent_id=idempotency_key,
                    order_id=order_identity.get("order_id"),
                    broker_order_id=order_identity.get("broker_order_id") or str(placed_order.order_id),
                    payload={
                        "safety_action": safety_action,
                        "order_ref": placed_order.order_ref,
                        "perm_id": placed_order.perm_id,
                        "order_type": plan.order_type,
                        "side": plan.side,
                        "qty": plan.qty,
                    },
                )
                self._emit_local_audit(
                    stage="db_persisted",
                    trace_ctx=trace_ctx,
                    symbol=symbol,
                    intent_id=idempotency_key,
                    order_id=order_identity.get("order_id"),
                    broker_order_id=order_identity.get("broker_order_id") or str(placed_order.order_id),
                    perm_id=getattr(placed_order, "perm_id", None),
                    status="submitted",
                    payload={
                        "order_ref": placed_order.order_ref,
                        "order_type": plan.order_type,
                        "side": plan.side,
                        "qty": plan.qty,
                    },
                )
                if safety_action is not None:
                    logger.info(
                        "safety_order_submitted",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        safety_action=safety_action,
                        order_id=placed_order.order_id,
                        broker_order_id=placed_order.broker_order_id,
                        perm_id=placed_order.perm_id,
                        order_type=plan.order_type,
                        side=plan.side,
                        qty=plan.qty,
                    )
                if bool(envelope_extras.get("flatten_action")) and not bool(
                    envelope_extras.get("suppress_manual_intervention_lock")
                    or envelope_extras.get("suppress_manual_lock")
                ):
                    await self._set_manual_intervention_lock(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        ttl_sec=manual_lock_ttl_sec,
                        envelope_extras=envelope_extras,
                        reason="manual_flatten" if manual_intervention else "flatten_action",
                    )
                elif manual_intervention and is_non_entry_flow:
                    await self._set_manual_intervention_lock(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        ttl_sec=manual_lock_ttl_sec,
                        envelope_extras=envelope_extras,
                    )

                self.stats["orders_placed"] += 1
                exec_orders_placed_total.labels(order_type=plan.order_type).inc()

                total_latency = time.time() - start_time

                logger.info("Order executed successfully",
                           decision_id=decision_id,
                           strategy_id=strategy_id,
                           symbol=symbol,
                           order_id=placed_order.order_id,
                           order_type=plan.order_type,
                           side=plan.side,
                           qty=plan.qty,
                           trace_id=trace_ctx.trace_id,
                           trigger_id=trace_ctx.trigger_id,
                           latency_ms=total_latency * 1000)
                self._publish_order_notification(
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    plan=plan,
                    placed_order=placed_order,
                    envelope_extras=envelope_extras,
                )

                return ExecutionResult(
                    success=True,
                    decision_id=decision_id,
                    order_id=placed_order.order_id,
                    idempotency_key=idempotency_key,
                    reason="Order placed successfully",
                    stage="completed",
                    metadata={
                        "order_type": plan.order_type,
                        "latency_ms": total_latency * 1000
                    }
                )
            else:
                # Dry-run mode: mark as completed without actual placement
                logger.info("Dry-run execution (no order placed)",
                           decision_id=decision_id,
                           strategy_id=strategy_id,
                           symbol=symbol,
                           order_type=plan.order_type)

                return ExecutionResult(
                    success=True,
                    decision_id=decision_id,
                    idempotency_key=idempotency_key,
                    reason="Dry-run mode (order not placed)",
                    stage="dry_run"
                )

        except Exception as e:
            logger.error("Execution error",
                        decision_id=decision_id,
                        error=str(e),
                        trace_id=trace_ctx.trace_id,
                        exc_info=True)

            exec_placement_errors_total.inc()
            try:
                await self.risk.release(idempotency_key)
            except Exception:
                pass

            return ExecutionResult(
                success=False,
                decision_id=decision_id,
                idempotency_key=idempotency_key,
                reason=f"Exception: {str(e)}",
                stage="error"
            )

    def _plan_to_envelope(
        self,
        plan: ExecPlan,
        symbol: str,
        strategy_id: str,
        decision_id: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Convert execution plan to decision envelope for risk service

        Args:
            plan: Execution plan
            symbol: Symbol
            strategy_id: Strategy ID
            decision_id: Decision ID

        Returns:
            Decision envelope dict
        """
        extras: Dict[str, Any] = {
            "ref_px": plan.limit_px or 0.0,
            "slippage_buffer_bps": 10,
        }
        if envelope_extras:
            extras.update(dict(envelope_extras))
        return {
            "decision_id": decision_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "target_qty": plan.qty if plan.side == "BUY" else -plan.qty,
            "order_style": {
                "kind": plan.order_type,  # FIXED: reg_lite expects "kind" not "order_type"
                "limit_px": plan.limit_px,
                "stop_px": plan.stop_px,
                "trail_amount": getattr(plan, "trail_amount", None),
                "trail_percent": getattr(plan, "trail_percent", None),
                "trail_stop_px": getattr(plan, "trail_stop_px", None),
                "lmt_price_offset": getattr(plan, "lmt_price_offset", None),
                "tif": plan.tif
            },
            "extras": extras
        }

    async def _persist_order_new(
        self,
        decision_id: str,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        idempotency_key: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
        trace_ctx: Optional[TraceContext] = None,
    ) -> None:
        """
        Persist order to database in NEW state

        Args:
            decision_id: Decision ID
            strategy_id: Strategy ID
            symbol: Symbol
            plan: Execution plan
            idempotency_key: Idempotency key
        """
        try:
            # Ensure strategy FK exists to avoid order persistence failures during dynamic strategy loading.
            await self.pg.execute(
                """
                INSERT INTO core.core_strategy (strategy_id, name, capital_limit_usd)
                VALUES ($1, $2, 0)
                ON CONFLICT (strategy_id) DO UPDATE
                SET name = EXCLUDED.name
                """,
                strategy_id,
                strategy_id,
            )

            note_obj: Dict[str, Any] = {
                "decision_id": decision_id,
                "order_style": {
                    "kind": plan.order_type,
                    "limit_px": plan.limit_px,
                    "stop_px": plan.stop_px,
                    "trail_amount": plan.trail_amount,
                    "trail_percent": plan.trail_percent,
                    "trail_stop_px": plan.trail_stop_px,
                    "lmt_price_offset": plan.lmt_price_offset,
                },
            }
            if envelope_extras:
                note_obj["extras"] = envelope_extras
                note_obj = self.flatten_controller.inject_created_lifecycle(
                    note_obj=note_obj,
                    envelope_extras=envelope_extras,
                )
            if trace_ctx is not None:
                note_obj["trace"] = trace_ctx.to_dict()
            note_data = json.dumps(_json_safe(note_obj), ensure_ascii=False)
            identity = self._current_ib_order_identity()

            await self.pg.execute(
                """
                INSERT INTO core.core_order (
                    order_id, strategy_id, symbol, idempotency_key,
                    side, order_type, qty, limit_px, stop_px, tif,
                    state, ib_client_id, ib_session_role, ib_connection_epoch,
                    note, created_at, updated_at
                ) VALUES (
                    gen_random_uuid(), $1, $2, $3,
                    $4, $5, $6, $7, $8, $9,
                    'NEW', $10, $11, $12,
                    $13, now(), now()
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                strategy_id, symbol, idempotency_key,
                plan.side, plan.order_type, plan.qty,
                plan.limit_px, plan.stop_px, plan.tif,
                identity["ib_client_id"], identity["ib_session_role"], identity["ib_connection_epoch"],
                note_data,
            )

            logger.debug("Order persisted to database",
                        decision_id=decision_id,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        state="NEW")

        except Exception as e:
            logger.error("Failed to persist order to database",
                        decision_id=decision_id,
                        error=str(e))
            raise

    async def _persist_order_new_with_entry_lock(
        self,
        *,
        decision_id: str,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        idempotency_key: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
        trace_ctx: Optional[TraceContext] = None,
        entry_isolation_mode_override: Optional[str] = None,
    ) -> tuple[bool, str, Dict[str, Any]]:
        """
        Acquire a symbol-level advisory lock and persist the NEW row on the same connection.
        This eliminates the race where two strategies pass entry isolation concurrently.
        """
        if not hasattr(self.pg, "acquire_conn"):
            await self._persist_order_new(
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                plan=plan,
                idempotency_key=idempotency_key,
                envelope_extras=envelope_extras,
                trace_ctx=trace_ctx,
            )
            return True, "ok", {}

        async with self.pg.acquire_conn() as conn:
            async with conn.transaction():
                mode = str(entry_isolation_mode_override or self.entry_isolation_mode or "cross_strategy").strip().lower()
                if mode not in {"cross_strategy", "strategy_only"}:
                    mode = "cross_strategy"
                # Advisory lock scoped to this transaction.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", str(symbol))
                self._emit_local_audit(
                    stage="entry_isolation_lock_acquired",
                    trace_ctx=trace_ctx,
                    symbol=str(symbol or "").strip().upper() or None,
                    status="acquired",
                    reason="symbol_advisory_lock_acquired",
                    payload={"mode": mode, "configured_mode": self.entry_isolation_mode},
                )

                if mode == "strategy_only":
                    row = await conn.fetchrow(
                        """
                        SELECT strategy_id, count(*) AS n
                        FROM core.core_order
                        WHERE strategy_id = $2
                          AND symbol = $1
                          AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                        GROUP BY strategy_id
                        ORDER BY n DESC
                        LIMIT 1
                        """,
                        symbol,
                        strategy_id,
                    )
                    if row and row.get("strategy_id"):
                        return (
                            False,
                            "entry_isolation_blocked_same_strategy_pending_order",
                            {
                                "same_strategy_id": str(row.get("strategy_id")),
                                "same_strategy_pending_count": int(row.get("n") or 0),
                            },
                        )
                else:
                    row = await conn.fetchrow(
                        """
                        SELECT strategy_id, count(*) AS n
                        FROM core.core_order
                        WHERE strategy_id <> $2
                          AND symbol = $1
                          AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                        GROUP BY strategy_id
                        ORDER BY n DESC
                        LIMIT 1
                        """,
                        symbol,
                        strategy_id,
                    )
                    if row and row.get("strategy_id"):
                        return (
                            False,
                            "entry_isolation_blocked_pending_order",
                            {
                                "other_strategy_id": str(row.get("strategy_id")),
                                "other_pending_count": int(row.get("n") or 0),
                            },
                        )

                await self._persist_order_new_conn(
                    conn=conn,
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    plan=plan,
                    idempotency_key=idempotency_key,
                    envelope_extras=envelope_extras,
                    trace_ctx=trace_ctx,
                )
                return True, "ok", {}

    async def _persist_order_new_conn(
        self,
        *,
        conn: Any,
        decision_id: str,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        idempotency_key: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
        trace_ctx: Optional[TraceContext] = None,
    ) -> None:
        # Ensure strategy FK exists.
        await conn.execute(
            """
            INSERT INTO core.core_strategy (strategy_id, name, capital_limit_usd)
            VALUES ($1, $2, 0)
            ON CONFLICT (strategy_id) DO UPDATE
            SET name = EXCLUDED.name
            """,
            strategy_id,
            strategy_id,
        )

        note_obj: Dict[str, Any] = {
            "decision_id": decision_id,
            "order_style": {
                "kind": plan.order_type,
                "limit_px": plan.limit_px,
                "stop_px": plan.stop_px,
                "trail_amount": plan.trail_amount,
                "trail_percent": plan.trail_percent,
                "trail_stop_px": plan.trail_stop_px,
                "lmt_price_offset": plan.lmt_price_offset,
            },
        }
        if envelope_extras:
            note_obj["extras"] = envelope_extras
            note_obj = self.flatten_controller.inject_created_lifecycle(
                note_obj=note_obj,
                envelope_extras=envelope_extras,
            )
        if trace_ctx is not None:
            note_obj["trace"] = trace_ctx.to_dict()
        note_data = json.dumps(_json_safe(note_obj), ensure_ascii=False)
        identity = self._current_ib_order_identity()

        await conn.execute(
            """
            INSERT INTO core.core_order (
                order_id, strategy_id, symbol, idempotency_key,
                side, order_type, qty, limit_px, stop_px, tif,
                state, ib_client_id, ib_session_role, ib_connection_epoch,
                note, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), $1, $2, $3,
                $4, $5, $6, $7, $8, $9,
                'NEW', $10, $11, $12,
                $13, now(), now()
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            strategy_id,
            symbol,
            idempotency_key,
            plan.side,
            plan.order_type,
            plan.qty,
            plan.limit_px,
            plan.stop_px,
            plan.tif,
            identity["ib_client_id"],
            identity["ib_session_role"],
            identity["ib_connection_epoch"],
            note_data,
        )

    async def _place_order(
        self,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        envelope_extras: Optional[Dict[str, Any]] = None,
    ) -> Optional[PlacedOrder]:
        """
        Place order via SafetyKernel

        Args:
            strategy_id: Strategy ID
            symbol: Symbol
            plan: Execution plan

        Returns:
            PlacedOrder or None on error
        """
        try:
            extras = envelope_extras or {}
            expected_epoch = extras.get("origin_ib_epoch")
            outside_rth = extras.get("outside_rth")
            if outside_rth is not None:
                outside_rth = bool(outside_rth)
            exchange_override = str(extras.get("exchange_override") or "").strip() or None

            # For non-entry intents, do not create an entry Plan (prevents overwriting the entry plan and avoids OCO).
            is_option = bool(
                str(extras.get("instrument_type") or "").strip().upper() == "OPTION"
                or str(symbol or "").strip().upper().startswith("OPT:")
            )
            if extras.get("flatten_action") or extras.get("protective_stop") or extras.get("close_only"):
                replace_old_id = extras.get("replace_protective_order_id") or extras.get("old_protective_order_id")
                use_replace = replace_old_id is not None and hasattr(self.kernel, "replace_protective_order_atomicish")
                replace_fn = (
                    self.kernel.replace_protective_order_atomicish
                    if use_replace
                    else self.kernel.place_order
                )
                common_kwargs = dict(
                    symbol=symbol,
                    side=plan.side,
                    qty=plan.qty,
                    order_type=plan.order_type,
                    limit_px=plan.limit_px,
                    stop_px=plan.stop_px,
                    trail_amount=plan.trail_amount,
                    trail_percent=plan.trail_percent,
                    trail_stop_px=plan.trail_stop_px,
                    lmt_price_offset=plan.lmt_price_offset,
                    tif=plan.tif,
                    order_ref=str(extras.get("order_ref") or ""),
                    outside_rth=outside_rth,
                    exchange_override=exchange_override,
                    expected_epoch=(int(expected_epoch) if expected_epoch is not None else None),
                    close_only=bool(extras.get("close_only") or extras.get("protective_stop")),
                    reduce_only=bool(extras.get("reduce_only", True)),
                    position_qty_hint=extras.get("position_qty_hint"),
                    previous_stop_px=extras.get("previous_stop_px") or extras.get("current_stop_px"),
                    manual_override=bool(extras.get("manual_override")),
                    emergency=bool(extras.get("emergency_exit") or extras.get("flatten_action")),
                )
                if use_replace:
                    common_kwargs["old_order_id"] = int(replace_old_id)
                placed_order = await replace_fn(**common_kwargs)
            else:
                attach_oco = not bool(extras.get("disable_oco", False))
                entry_timeout_sec = None
                raw_timeout = extras.get("entry_timeout_sec")
                if raw_timeout is not None:
                    try:
                        entry_timeout_sec = float(raw_timeout)
                    except Exception:
                        entry_timeout_sec = None
                placed_order = await self.kernel.place_entry_order(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    side=plan.side,
                    qty=plan.qty,
                    order_type=plan.order_type,
                    limit_px=plan.limit_px,
                    stop_px=plan.stop_px,
                    trail_amount=plan.trail_amount,
                    trail_percent=plan.trail_percent,
                    trail_stop_px=plan.trail_stop_px,
                    lmt_price_offset=plan.lmt_price_offset,
                    tif=plan.tif,
                    order_ref=str(extras.get("order_ref") or "") or None,
                    outside_rth=outside_rth,
                    exchange_override=exchange_override,
                    attach_oco=attach_oco,
                    oco_protection=dict(extras.get("protective_oco") or {}),
                    entry_timeout_sec=entry_timeout_sec,
                    expected_epoch=(int(expected_epoch) if expected_epoch is not None else None),
                    account_id=extras.get("account_id"),
                    strategy_instance_id=extras.get("strategy_instance_id"),
                    lot_id=(
                        extras.get("position_lot_id")
                        or extras.get("lot_id")
                        or (dict(extras.get("protective_oco") or {}).get("position_lot_id"))
                    ),
                )

            return placed_order

        except Exception as e:
            logger.error("Failed to place order via kernel",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        error=str(e))
            return None

    async def _get_live_position_qty(
        self,
        *,
        strategy_id: str,
        symbol: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
    ) -> float:
        extras = dict(envelope_extras or {})
        close_only_action = bool(
            extras.get("close_only")
            and (extras.get("flatten_action") or extras.get("protective_stop"))
        )
        has_close_only_hint = extras.get("position_qty_hint") is not None
        manual_force_flatten = bool(
            extras.get("manual_intervention")
            and extras.get("flatten_action")
            and extras.get("close_only")
        )
        is_option = str(symbol).startswith("OPT:") or str(extras.get("instrument_type") or "").upper() == "OPTION"

        # For options we rely on the strategy-position ledger (internal OPT: symbols).
        # Broker-level position queries typically can't resolve internal symbols safely.
        if is_option:
            db_qty = None
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
                if row and row.get("pos_qty") is not None:
                    db_qty = float(row["pos_qty"])
            except Exception:
                db_qty = None

            if bool(extras.get("close_only")) and extras.get("position_qty_hint") is not None:
                try:
                    hint_qty = float(extras.get("position_qty_hint"))
                except Exception:
                    hint_qty = None
                if hint_qty is not None:
                    if db_qty is not None:
                        if abs(db_qty) <= 0:
                            return 0.0
                        if hint_qty * db_qty < 0:
                            logger.warning(
                                "option_close_only_hint_db_sign_mismatch",
                                strategy_id=strategy_id,
                                symbol=symbol,
                                hint_qty=hint_qty,
                                db_qty=db_qty,
                                flatten_action=bool(extras.get("flatten_action")),
                                protective_stop=bool(extras.get("protective_stop")),
                                manual_intervention=bool(extras.get("manual_intervention")),
                            )
                            return 0.0
                        capped_abs = min(abs(hint_qty), abs(db_qty))
                        if capped_abs <= 0:
                            return 0.0
                        return float(capped_abs if hint_qty > 0 else -capped_abs)
                    raise BrokerTruthUnavailableError("option_position_truth_unavailable")

            if db_qty is not None:
                return float(db_qty)
            return 0.0

        # Strategy-scoped runtime hint can bypass DB lag while keeping close-only scoped to the strategy intent.
        # We still cap it by broker inventory when broker data is available.
        if bool(extras.get("close_only")) and has_close_only_hint:
            hint_qty = None
            try:
                hint_qty = float(extras.get("position_qty_hint"))
            except Exception:
                hint_qty = None
            if hint_qty is not None and abs(hint_qty) > 0:
                broker = getattr(self.kernel, "broker", None)
                if broker and hasattr(broker, "position_qty"):
                    try:
                        broker_qty = float(await broker.position_qty(symbol))
                        if abs(broker_qty) <= 0:
                            return 0.0
                        if hint_qty * broker_qty < 0:
                            logger.warning(
                                "close_only_hint_broker_sign_mismatch",
                                strategy_id=strategy_id,
                                symbol=symbol,
                                hint_qty=hint_qty,
                                broker_qty=broker_qty,
                                flatten_action=bool(extras.get("flatten_action")),
                                protective_stop=bool(extras.get("protective_stop")),
                            )
                            return 0.0
                        capped_abs = min(abs(hint_qty), abs(broker_qty))
                        if capped_abs <= 0:
                            return 0.0
                        return float(capped_abs if hint_qty > 0 else -capped_abs)
                    except Exception as exc:
                        logger.warning(
                            "close_only_hint_broker_cap_failed",
                            strategy_id=strategy_id,
                            symbol=symbol,
                            error=str(exc),
                            flatten_action=bool(extras.get("flatten_action")),
                            protective_stop=bool(extras.get("protective_stop")),
                        )
                        raise BrokerTruthUnavailableError("broker_position_truth_unavailable") from exc
                raise BrokerTruthUnavailableError("broker_position_truth_unavailable")

        # Manual emergency flatten may intentionally flatten account inventory.
        # For non-manual actions, strategy ledger remains the default truth source.
        if manual_force_flatten:
            broker = getattr(self.kernel, "broker", None)
            if broker and hasattr(broker, "position_qty"):
                try:
                    return float(await broker.position_qty(symbol))
                except Exception as exc:
                    logger.warning(
                        "close_only_live_position_broker_query_failed",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        error=str(exc),
                        flatten_action=bool(extras.get("flatten_action")),
                        protective_stop=bool(extras.get("protective_stop")),
                        manual_intervention=bool(extras.get("manual_intervention")),
                    )
                    raise BrokerTruthUnavailableError("broker_position_truth_unavailable") from exc
            raise BrokerTruthUnavailableError("broker_position_truth_unavailable")

        # Strategy-context position is the correct truth source for close-only safety.
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
            if row and row.get("pos_qty") is not None:
                return float(row["pos_qty"])
        except Exception:
            pass

        # For non-manual close-only flatten/protective-stop without runtime hint, do not broker-fallback.
        # This keeps exits scoped to strategy state and avoids account-level cross-strategy bleed.
        if close_only_action and (not manual_force_flatten) and (not has_close_only_hint):
            return 0.0

        # Fallback: if strategy ledger is missing/unavailable, use broker position (equities/ETFs only).
        broker = getattr(self.kernel, "broker", None)
        if broker and hasattr(broker, "position_qty"):
            try:
                return float(await broker.position_qty(symbol))
            except Exception:
                return 0.0

        return 0.0

    async def _get_broker_position_qty_for_recovery(self, *, strategy_id: str, symbol: str) -> tuple[bool, float, str]:
        broker = getattr(self.kernel, "broker", None)
        if not broker or not hasattr(broker, "position_qty"):
            return False, 0.0, "broker_position_truth_unavailable"
        try:
            return True, float(await broker.position_qty(symbol)), "ok"
        except Exception as exc:
            logger.warning(
                "close_only_recovery_broker_truth_unavailable",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return False, 0.0, "broker_position_truth_unavailable"

    @staticmethod
    def _is_broker_truth_recovery_lane(envelope_extras: Dict[str, Any]) -> bool:
        extras = dict(envelope_extras or {})
        return bool(
            extras.get("runtime_recovery_action")
            or extras.get("recovery_requires_uom_safety_lane")
            or extras.get("broker_truth_recovery")
            or extras.get("position_safety_recovery")
        )

    async def _enforce_close_only_guard(
        self,
        *,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        envelope_extras: Dict[str, Any],
    ) -> tuple[bool, str]:
        side = str(plan.side or "").upper()
        qty_req = float(plan.qty or 0.0)
        if qty_req <= 0:
            return False, "close_only_invalid_qty"

        if side not in {"BUY", "SELL"}:
            return False, f"close_only_unsupported_side:{side}"

        if self._is_broker_truth_recovery_lane(envelope_extras):
            available, qty_live_raw, truth_reason = await self._get_broker_position_qty_for_recovery(
                strategy_id=strategy_id,
                symbol=symbol,
            )
            if not available:
                return False, f"close_only_manual_required:{truth_reason}"
            qty_live = float(qty_live_raw)
        else:
            try:
                qty_live = float(await self._get_live_position_qty(strategy_id=strategy_id, symbol=symbol, envelope_extras=envelope_extras))
            except BrokerTruthUnavailableError as exc:
                return False, f"close_only_manual_required:{exc}"

        if side == "SELL":
            if qty_live <= 0:
                return False, f"close_only_no_long_position:qty_live={qty_live}"
            qty_safe = min(qty_req, float(qty_live))
        else:  # BUY
            if qty_live >= 0:
                return False, f"close_only_no_short_position:qty_live={qty_live}"
            qty_safe = min(qty_req, abs(float(qty_live)))

        if qty_safe <= 0:
            return False, f"close_only_no_position:qty_live={qty_live}"

        if qty_safe < qty_req:
            logger.warning(
                "close_only_qty_clamped",
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                qty_req=qty_req,
                qty_live=qty_live,
                qty_safe=qty_safe,
                extras=envelope_extras,
            )
            plan.qty = float(qty_safe)

        return True, "ok"

    @staticmethod
    def _enforce_manual_flatten_limit_guard(
        *,
        plan: ExecPlan,
        envelope_extras: Dict[str, Any],
    ) -> tuple[bool, str]:
        extras = dict(envelope_extras or {})
        if not bool(extras.get("manual_intervention")):
            return True, "ok"
        if not bool(extras.get("flatten_action")):
            return True, "ok"
        if not bool(extras.get("close_only")):
            return True, "ok"
        if str(plan.order_type or "").upper() != "LMT":
            return True, "ok"

        limit_px = float(plan.limit_px or 0.0)
        if limit_px <= 0:
            return False, "manual_flatten_limit_invalid_px"

        quote = dict(extras.get("manual_quote_snapshot") or {})
        anchor_px = None
        try:
            anchor_px = float(extras.get("manual_anchor_px") or 0.0)
            if anchor_px <= 0:
                anchor_px = None
        except Exception:
            anchor_px = None
        if anchor_px is None:
            try:
                rp = float(extras.get("manual_ref_px") or 0.0)
                anchor_px = rp if rp > 0 else None
            except Exception:
                anchor_px = None

        def _pos(v: Any) -> Optional[float]:
            try:
                x = float(v)
                return x if x > 0 else None
            except Exception:
                return None

        side = str(plan.side or "").upper()
        if side == "SELL":
            ref_px = _pos(quote.get("bid")) or _pos(quote.get("mid")) or _pos(quote.get("last")) or _pos(quote.get("ask"))
        elif side == "BUY":
            ref_px = _pos(quote.get("ask")) or _pos(quote.get("mid")) or _pos(quote.get("last")) or _pos(quote.get("bid"))
        else:
            return False, f"manual_flatten_limit_unsupported_side:{side}"

        bid = _pos(quote.get("bid"))
        ask = _pos(quote.get("ask"))
        if bid is not None and ask is not None:
            if bid > ask:
                return False, f"manual_flatten_limit_bad_quote:crossed,bid={bid:.6f},ask={ask:.6f}"
            mid = (bid + ask) / 2.0
            spread_bps = ((ask - bid) / mid * 1e4) if mid > 0 else 0.0
            max_spread_bps = float(extras.get("manual_limit_guard_quote_max_spread_bps") or 1500.0)
            if spread_bps > max_spread_bps:
                return False, (
                    "manual_flatten_limit_bad_quote:"
                    f"spread_bps={spread_bps:.2f},max={max_spread_bps:.2f}"
                )

        # If quote and anchor disagree, reject the order instead of letting the
        # caller-selected anchor validate an outlier limit.
        if ref_px is not None and anchor_px is not None:
            try:
                dev_bps = abs(ref_px - anchor_px) / anchor_px * 1e4
            except Exception:
                dev_bps = 0.0
            anchor_max_dev_bps = float(extras.get("manual_limit_guard_anchor_max_dev_bps") or 2000.0)
            if dev_bps > anchor_max_dev_bps:
                return False, (
                    "manual_flatten_limit_anchor_quote_conflict:"
                    f"quote={ref_px:.6f},anchor={anchor_px:.6f},dev_bps={dev_bps:.2f}"
                )

        # Use anchor/ref only when no usable quote was supplied.
        ref_px = ref_px or anchor_px
        if ref_px is None:
            return False, "manual_flatten_limit_no_ref_px"

        max_dev_bps = float(extras.get("manual_limit_guard_max_dev_bps") or 500.0)
        max_dev = max(0.001, max_dev_bps / 10_000.0)
        try:
            min_tick = float(extras.get("manual_limit_guard_min_tick") or 0.01)
            min_tick = min_tick if min_tick > 0 else 0.01
        except Exception:
            min_tick = 0.01
        low = min(ref_px * (1.0 - max_dev), ref_px - min_tick)
        high = max(ref_px * (1.0 + max_dev), ref_px + min_tick)
        if limit_px < low or limit_px > high:
            return False, (
                "manual_flatten_limit_out_of_band:"
                f"ref={ref_px:.6f},limit={limit_px:.6f},band=[{low:.6f},{high:.6f}]"
            )
        return True, "ok"

    async def _cancel_open_orders_for_symbol(
        self,
        *,
        strategy_id: str,
        symbol: str,
        reason: str,
        only_order_types: Optional[set[str]] = None,
        account_id: Optional[str] = None,
        strategy_instance_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        lot_id: Optional[str] = None,
    ) -> None:
        """
        Cancel broker open orders only when a durable owner row proves scope.

        This is primarily used to avoid "ghost stops" when flattening, and to prevent stacking
        multiple protective stop orders when strategies update stop levels.
        """
        cancelled: set[int] = set()
        sym_u = str(symbol or "").strip().upper()
        cancel_request = {
            "account_id": str(account_id or "").strip(),
            "strategy_id": str(strategy_id or "").strip(),
            "strategy_instance_id": str(strategy_instance_id or "").strip(),
            "symbol": sym_u,
            "intent_id": str(intent_id or "").strip(),
            "client_order_id": str(client_order_id or "").strip(),
            "lot_id": str(lot_id or "").strip(),
        }

        broker = getattr(self.kernel, "broker", None)
        broker_open_by_id: Dict[int, Any] = {}
        broker_prefetch_failed = False
        if broker and hasattr(broker, "open_orders"):
            try:
                open_orders = await broker.open_orders()
                for o in open_orders or []:
                    try:
                        oid = int(getattr(o, "order_id"))
                        broker_open_by_id[oid] = o
                    except Exception:
                        continue
            except Exception as exc:
                broker_prefetch_failed = True
                logger.warning(
                    "cancel_broker_open_orders_prefetch_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    reason=reason,
                    error=str(exc),
                )

        try:
            rows = await self.pg.fetchall(
                """
                SELECT broker_order_id, order_type, state, order_ref, note, strategy_id, symbol
                FROM core.core_order
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                ORDER BY created_at DESC
                """,
                strategy_id,
                symbol,
            )
        except Exception as exc:
            logger.warning(
                "cancel_open_orders_query_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                reason=reason,
                error=str(exc),
            )
            return

        def _owner_row_from_cancel_row(source: Any) -> Dict[str, Any]:
            row = dict(source)
            note_payload = _note_payload_dict(row.get("note"))
            extras = note_payload.get("extras") if isinstance(note_payload.get("extras"), dict) else {}
            return {
                **row,
                "account_id": row.get("account_id") or extras.get("account_id") or note_payload.get("account_id"),
                "strategy_instance_id": (
                    row.get("strategy_instance_id")
                    or extras.get("strategy_instance_id")
                    or note_payload.get("strategy_instance_id")
                ),
                "intent_id": row.get("intent_id") or extras.get("intent_id") or note_payload.get("intent_id"),
                "client_order_id": (
                    row.get("client_order_id")
                    or extras.get("client_order_id")
                    or note_payload.get("client_order_id")
                ),
                "lot_id": (
                    row.get("lot_id")
                    or row.get("position_lot_id")
                    or extras.get("lot_id")
                    or extras.get("position_lot_id")
                    or note_payload.get("lot_id")
                    or note_payload.get("position_lot_id")
                ),
            }

        owner_rows = [_owner_row_from_cancel_row(row) for row in rows or []]
        if not cancel_request["strategy_instance_id"]:
            row_instances = {
                str(row.get("strategy_instance_id") or "").strip()
                for row in owner_rows
                if str(row.get("strategy_instance_id") or "").strip()
            }
            if len(row_instances) == 1:
                # Transitional compatibility only: old non-entry callers may
                # omit strategy_instance_id. Only infer from durable rows when
                # the candidate set has a single owner instance.
                cancel_request["strategy_instance_id"] = next(iter(row_instances))
        if not cancel_request["account_id"]:
            row_accounts = {
                str(row.get("account_id") or "").strip()
                for row in owner_rows
                if str(row.get("account_id") or "").strip()
            }
            if len(row_accounts) == 1:
                cancel_request["account_id"] = next(iter(row_accounts))

        def _same_strategy_flatten_cancel_scope(request: Dict[str, Any], row: Dict[str, Any]) -> bool:
            if not str(reason or "").startswith("pre_flatten:"):
                return False
            if str(request.get("strategy_id") or "").strip() != str(row.get("strategy_id") or "").strip():
                return False
            if str(request.get("symbol") or "").strip().upper() != str(row.get("symbol") or "").strip().upper():
                return False
            req_account = str(request.get("account_id") or "").strip()
            row_account = str(row.get("account_id") or "").strip()
            if req_account and row_account and req_account != row_account:
                return False
            req_lot = str(request.get("lot_id") or "").strip()
            row_lot = str(row.get("lot_id") or "").strip()
            if req_lot and row_lot and req_lot != row_lot:
                return False
            row_ref = str(row.get("order_ref") or "").strip()
            sid = str(request.get("strategy_id") or "").strip()
            return bool(row_ref and sid and (f"|{sid}|" in row_ref or f":{sid}:" in row_ref))

        for owner_row in owner_rows:
            row = owner_row
            if not cancel_request_matches_order_owner(cancel_request, owner_row) and not _same_strategy_flatten_cancel_scope(cancel_request, row):
                logger.warning(
                    "cancel_open_order_owner_mismatch",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    broker_order_id=str(row.get("broker_order_id") or ""),
                    request_strategy_instance_id=cancel_request.get("strategy_instance_id"),
                    row_strategy_instance_id=str(row.get("strategy_instance_id") or ""),
                    request_account_id=cancel_request.get("account_id"),
                    row_account_id=str(row.get("account_id") or ""),
                    reason=reason,
                )
                continue
            broker_order_id = str(row.get("broker_order_id") or "").strip()
            row_order_ref = str(row.get("order_ref") or "").strip()
            if not broker_order_id:
                continue
            try:
                order_type = str(row.get("order_type") or "").upper()
                if only_order_types and order_type not in only_order_types:
                    continue
                order_id_int = int(broker_order_id)
            except Exception:
                continue

            # Safety guard against orderId reuse across IB sessions:
            # only cancel if the broker currently reports this orderId as open
            # for the same symbol and exact row order_ref when one exists.
            if broker and hasattr(broker, "open_orders"):
                if broker_prefetch_failed:
                    # If we cannot verify open orders, avoid blind cancels by integer orderId (re-use hazard).
                    continue
                o = broker_open_by_id.get(order_id_int)
                if not o:
                    continue
                if str(getattr(o, "symbol", "") or "").strip().upper() != sym_u:
                    continue
                broker_order_ref = str(getattr(o, "order_ref", "") or "")
                if row_order_ref:
                    if broker_order_ref != row_order_ref:
                        continue

            try:
                ok = await self.kernel.broker.cancel(order_id_int)
                if ok:
                    cancelled.add(order_id_int)
                    logger.info(
                        "cancel_open_order_request_accepted",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        broker_order_id=broker_order_id,
                        reason=reason,
                    )
            except Exception as exc:
                logger.warning(
                    "cancel_open_order_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    reason=reason,
                    error=str(exc),
                )

        # Unknown broker-only orders are not cancelled here. They require a
        # durable owner row or a separate manual/adjudication flow.
        if not broker or not hasattr(broker, "open_orders"):
            return
        return

    async def _enforce_entry_isolation(
        self,
        *,
        strategy_id: str,
        symbol: str,
        trace_ctx: Optional[TraceContext] = None,
        entry_isolation_mode_override: Optional[str] = None,
    ) -> tuple[bool, str, Dict[str, Any]]:
        # cross_strategy: block if another strategy has pending/open order for symbol.
        # strategy_only: block only if this same strategy already has pending/open order for symbol.
        mode = str(entry_isolation_mode_override or self.entry_isolation_mode or "cross_strategy").strip().lower()
        if mode not in {"cross_strategy", "strategy_only"}:
            mode = "cross_strategy"
        try:
            if mode == "strategy_only":
                row = await self.pg.fetchone(
                    """
                    SELECT strategy_id, count(*) AS n
                    FROM core.core_order
                    WHERE strategy_id = $2
                      AND symbol = $1
                      AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                    GROUP BY strategy_id
                    ORDER BY n DESC
                    LIMIT 1
                    """,
                    symbol,
                    strategy_id,
                )
                if row and row.get("strategy_id"):
                    meta = {
                        "same_strategy_id": str(row.get("strategy_id")),
                        "same_strategy_pending_count": int(row.get("n") or 0),
                    }
                    self._emit_local_audit(
                        stage="entry_isolation_blocked_pending_order",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        severity="warning",
                        status="blocked",
                        reason="entry_isolation_blocked_same_strategy_pending_order",
                        payload=dict(meta),
                    )
                    return (
                        False,
                        "entry_isolation_blocked_same_strategy_pending_order",
                        meta,
                    )
            else:
                row = await self.pg.fetchone(
                    """
                    SELECT strategy_id, count(*) AS n
                    FROM core.core_order
                    WHERE strategy_id <> $2
                      AND symbol = $1
                      AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                    GROUP BY strategy_id
                    ORDER BY n DESC
                    LIMIT 1
                    """,
                    symbol,
                    strategy_id,
                )
                if row and row.get("strategy_id"):
                    meta = {
                        "other_strategy_id": str(row.get("strategy_id")),
                        "other_pending_count": int(row.get("n") or 0),
                    }
                    self._emit_local_audit(
                        stage="entry_isolation_blocked_pending_order",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        severity="warning",
                        status="blocked",
                        reason="entry_isolation_blocked_pending_order",
                        payload=dict(meta),
                    )
                    return (
                        False,
                        "entry_isolation_blocked_pending_order",
                        meta,
                    )
        except Exception as exc:
            logger.warning(
                "entry_isolation_pending_query_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            self._emit_local_audit(
                stage="entry_isolation_pending_query_failed",
                trace_ctx=trace_ctx,
                symbol=symbol,
                severity="warning",
                status="error",
                reason="entry_isolation_pending_query_failed",
                payload={"error": str(exc)},
            )

        # Account-level position check is only valid for cross-strategy isolation mode.
        if mode == "cross_strategy":
            broker = getattr(self.kernel, "broker", None)
            if broker and hasattr(broker, "position_qty"):
                try:
                    qty_live = float(await broker.position_qty(symbol))
                    if abs(qty_live) > 1e-9:
                        self._emit_local_audit(
                            stage="entry_isolation_blocked_live_position",
                            trace_ctx=trace_ctx,
                            symbol=symbol,
                            severity="warning",
                            status="blocked",
                            reason="entry_isolation_blocked_live_position",
                            payload={"broker_qty": qty_live},
                        )
                        return (
                            False,
                            "entry_isolation_blocked_live_position",
                            {"broker_qty": qty_live},
                        )
                except Exception as exc:
                    logger.warning(
                        "entry_isolation_broker_position_check_failed",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        error=str(exc),
                    )
                    self._emit_local_audit(
                        stage="entry_isolation_broker_position_check_failed",
                        trace_ctx=trace_ctx,
                        symbol=symbol,
                        severity="warning",
                        status="error",
                        reason="entry_isolation_broker_position_check_failed",
                        payload={"error": str(exc)},
                    )

        return True, "ok", {}

    async def _update_order_submitted(
        self,
        idempotency_key: str,
        order_id: int,
        broker_order_id: Optional[str],
        perm_id: Optional[int],
        order_ref: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Update order with broker order_id, order_ref and SUBMITTED state

        Args:
            idempotency_key: Idempotency key
            order_id: Broker order ID (IB orderId)
            broker_order_id: Broker's order ID string (same as order_id)
            perm_id: Permanent ID from broker
            order_ref: Order reference for reconciliation matching
        """
        try:
            note_data = None
            identity = self._current_ib_order_identity()
            if bool((envelope_extras or {}).get("flatten_action")):
                note_data = await self.flatten_controller.mark_submitted_by_idempotency(
                    idempotency_key=idempotency_key,
                    order_id=order_id,
                    close_only=bool((envelope_extras or {}).get("close_only")),
                )

            if note_data is None:
                await self.pg.execute(
                    """
                    UPDATE core.core_order
                    SET broker_order_id = $1,
                        ib_perm_id = $2,
                        order_ref = $3,
                        ib_client_id = $4,
                        ib_session_role = $5,
                        ib_connection_epoch = $6,
                        state = 'SUBMITTED',
                        submitted_at = now(),
                        updated_at = now()
                    WHERE idempotency_key = $7
                    """,
                    str(order_id),
                    perm_id,
                    order_ref,
                    identity["ib_client_id"],
                    identity["ib_session_role"],
                    identity["ib_connection_epoch"],
                    idempotency_key,
                )
            else:
                await self.pg.execute(
                    """
                    UPDATE core.core_order
                    SET broker_order_id = $1,
                        ib_perm_id = $2,
                        order_ref = $3,
                        ib_client_id = $4,
                        ib_session_role = $5,
                        ib_connection_epoch = $6,
                        state = 'SUBMITTED',
                        submitted_at = now(),
                        note = $7,
                        updated_at = now()
                    WHERE idempotency_key = $8
                    """,
                    str(order_id),
                    perm_id,
                    order_ref,
                    identity["ib_client_id"],
                    identity["ib_session_role"],
                    identity["ib_connection_epoch"],
                    note_data,
                    idempotency_key,
                )

            logger.debug("Order updated to SUBMITTED",
                        idempotency_key=idempotency_key,
                        order_id=order_id,
                        order_ref=order_ref)

        except Exception as e:
            logger.error("Failed to update order to SUBMITTED",
                        idempotency_key=idempotency_key,
                        error=str(e))
            raise

    async def _update_order_rejected(
        self,
        idempotency_key: str,
        reason: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Update order to REJECTED state

        Args:
            idempotency_key: Idempotency key
            reason: Rejection reason
        """
        try:
            note_data = None
            if bool((envelope_extras or {}).get("flatten_action")):
                note_data = await self.flatten_controller.mark_rejected_by_idempotency(
                    idempotency_key=idempotency_key,
                    reason=reason,
                    close_only=bool((envelope_extras or {}).get("close_only")),
                )
            if note_data is None:
                note_data = json.dumps({"reject_reason": reason})

            await self.pg.execute(
                """
                UPDATE core.core_order
                SET state = 'REJECTED',
                    note = $1,
                    updated_at = now()
                WHERE idempotency_key = $2
                """,
                note_data, idempotency_key
            )

            logger.debug("Order updated to REJECTED",
                        idempotency_key=idempotency_key,
                        reason=reason)

        except Exception as e:
            logger.error("Failed to update order to REJECTED",
                        idempotency_key=idempotency_key,
                        error=str(e))

    async def _get_order_identity(self, *, idempotency_key: str) -> Dict[str, Optional[str]]:
        try:
            row = await self.pg.fetchone(
                """
                SELECT order_id::text AS order_id, broker_order_id, parent_order_id::text AS parent_order_id
                FROM core.core_order
                WHERE idempotency_key = $1
                LIMIT 1
                """,
                idempotency_key,
            )
            if not row:
                return {"order_id": None, "broker_order_id": None, "parent_order_id": None}
            return {
                "order_id": row.get("order_id"),
                "broker_order_id": row.get("broker_order_id"),
                "parent_order_id": row.get("parent_order_id"),
            }
        except Exception:
            return {"order_id": None, "broker_order_id": None, "parent_order_id": None}

    async def _emit_event(
        self,
        *,
        event_type: str,
        trace_ctx: TraceContext,
        symbol: str,
        intent_id: Optional[str],
        payload: Optional[Dict[str, Any]] = None,
        order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        parent_order_id: Optional[str] = None,
    ) -> None:
        payload = dict(payload or {})
        identity = self._current_ib_order_identity()
        source_event_id = str(payload.get("source_event_id") or "").strip() or None
        if source_event_id is None:
            source_event_id = f"{event_type}:{intent_id or ''}:{identity.get('ib_connection_epoch')}"
            payload.setdefault("source_event_id", source_event_id)
        payload.setdefault("ib_connection_epoch", identity.get("ib_connection_epoch"))
        event = {
            "event_type": event_type,
            "trace_ctx": trace_ctx,
            "symbol": symbol,
            "intent_id": intent_id,
            "payload": payload,
            "order_id": order_id,
            "broker_order_id": broker_order_id,
            "parent_order_id": parent_order_id,
            "source_event_id": source_event_id,
            "ib_connection_epoch": identity.get("ib_connection_epoch"),
        }
        try:
            self._schedule_event_emit(event)
        except RuntimeError:
            await self._emit_event_now(event)

    def _emit_local_audit(
        self,
        *,
        stage: str,
        trace_ctx: Optional[TraceContext],
        symbol: Optional[str],
        severity: str = "info",
        status: Optional[str] = None,
        reason: Optional[str] = None,
        intent_id: Optional[str] = None,
        order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[Any] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.audit_writer is None:
            return
        try:
            self.audit_writer.emit(
                domain="exec",
                component="execution_service",
                stage=stage,
                severity=severity,
                strategy_id=getattr(trace_ctx, "strategy_id", None),
                strategy_run_id=getattr(trace_ctx, "strategy_run_id", None),
                trigger_id=getattr(trace_ctx, "trigger_id", None),
                decision_id=getattr(trace_ctx, "decision_id", None),
                trace_id=getattr(trace_ctx, "trace_id", None),
                causation_id=getattr(trace_ctx, "causation_id", None),
                intent_id=intent_id,
                order_id=order_id,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
                symbol=symbol,
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            logger.warning("local_exec_audit_emit_failed", stage=stage, error=str(exc))

    def _schedule_event_emit(self, event: Dict[str, Any]) -> None:
        if self._event_emit_stopping:
            trace_ctx = event.get("trace_ctx")
            logger.warning(
                "order_event_emit_dropped_during_shutdown",
                event_type=event.get("event_type"),
                strategy_id=getattr(trace_ctx, "strategy_id", None),
                decision_id=getattr(trace_ctx, "decision_id", None),
            )
            return
        if len(self._event_emit_tasks) >= self._event_emit_max_pending:
            dropped_oldest = None
            dropped_new = False
            critical_types = {"order_submitted", "order_rejected"}
            incoming_type = str(event.get("event_type") or "")

            if len(self._event_emit_overflow) >= self._event_emit_overflow.maxlen:
                if incoming_type in critical_types:
                    drop_idx = -1
                    for idx, candidate in enumerate(self._event_emit_overflow):
                        c_type = str(candidate.get("event_type") or "")
                        if c_type not in critical_types:
                            drop_idx = idx
                            break
                    if drop_idx >= 0:
                        dropped_oldest = self._event_emit_overflow[drop_idx]
                        del self._event_emit_overflow[drop_idx]
                    else:
                        dropped_oldest = self._event_emit_overflow.popleft()
                else:
                    dropped_new = True

            if not dropped_new:
                self._event_emit_overflow.append(event)
            trace_ctx = event.get("trace_ctx")
            logger.warning(
                "order_event_emit_overflow_queued",
                event_type=event.get("event_type"),
                strategy_id=getattr(trace_ctx, "strategy_id", None),
                decision_id=getattr(trace_ctx, "decision_id", None),
                pending=len(self._event_emit_tasks),
                overflow_size=len(self._event_emit_overflow),
                overflow_max=self._event_emit_overflow.maxlen,
                dropped_oldest=bool(dropped_oldest),
                dropped_new=dropped_new,
            )
            return

        task = asyncio.create_task(self._emit_event_now(event))
        self._event_emit_tasks.add(task)
        task.add_done_callback(self._on_emit_task_done)

    def _on_emit_task_done(self, task: asyncio.Task) -> None:
        self._event_emit_tasks.discard(task)
        if self._event_emit_stopping:
            return
        while self._event_emit_overflow and len(self._event_emit_tasks) < self._event_emit_max_pending:
            next_event = self._event_emit_overflow.popleft()
            self._schedule_event_emit(next_event)

    async def _emit_event_now(self, event: Dict[str, Any]) -> None:
        trace_ctx = event["trace_ctx"]
        timeout_sec = 0.2
        try:
            await asyncio.wait_for(
                emit_order_event(
                    pg=self.pg,
                    enabled=self.order_event_enabled,
                    event_type=event["event_type"],
                    trace_id=trace_ctx.trace_id,
                    causation_id=trace_ctx.causation_id,
                    strategy_id=trace_ctx.strategy_id,
                    strategy_run_id=trace_ctx.strategy_run_id,
                    trigger_id=trace_ctx.trigger_id,
                    decision_id=trace_ctx.decision_id,
                    intent_id=event.get("intent_id"),
                    order_id=event.get("order_id"),
                    broker_order_id=event.get("broker_order_id"),
                    parent_order_id=event.get("parent_order_id"),
                    source_event_id=event.get("source_event_id"),
                    ib_connection_epoch=event.get("ib_connection_epoch"),
                    symbol=event["symbol"],
                    payload=event.get("payload"),
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "order_event_emit_timeout",
                event_type=event.get("event_type"),
                strategy_id=trace_ctx.strategy_id,
                decision_id=trace_ctx.decision_id,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            logger.warning(
                "order_event_emit_failed",
                event_type=event.get("event_type"),
                strategy_id=trace_ctx.strategy_id,
                decision_id=trace_ctx.decision_id,
                error=str(exc),
            )

    async def _set_manual_intervention_lock(
        self,
        *,
        strategy_id: str,
        symbol: str,
        ttl_sec: int,
        envelope_extras: Dict[str, Any],
        reason: str = "manual_flatten",
    ) -> None:
        if self.manual_intervention_lock_store is None:
            return
        try:
            await self.manual_intervention_lock_store.lock(
                strategy_id=strategy_id,
                symbol=symbol,
                ttl_sec=max(1, int(ttl_sec)),
                reason=str(reason or "manual_flatten"),
                context={
                    "intervention_id": envelope_extras.get("manual_intervention_id"),
                    "trigger_id": envelope_extras.get("trigger_id"),
                    "source_trigger_id": envelope_extras.get("source_trigger_id"),
                    "source": envelope_extras.get("manual_source"),
                    "flatten_action": bool(envelope_extras.get("flatten_action")),
                    "close_only": bool(envelope_extras.get("close_only")),
                    "account_id": envelope_extras.get("account_id"),
                    "strategy_instance_id": envelope_extras.get("strategy_instance_id"),
                    "position_lot_id": envelope_extras.get("position_lot_id") or envelope_extras.get("lot_id"),
                    "position_owner_key": envelope_extras.get("position_owner_key"),
                },
            )
        except Exception as exc:
            logger.warning(
                "manual_intervention_lock_set_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )

    async def _get_manual_intervention_lock(
        self,
        *,
        strategy_id: str,
        symbol: str,
        strategy_instance_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if self.manual_intervention_lock_store is None:
            return None
        try:
            try:
                return await self.manual_intervention_lock_store.get_active(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    strategy_instance_id=strategy_instance_id,
                )
            except TypeError:
                return await self.manual_intervention_lock_store.get_active(
                    strategy_id=strategy_id,
                    symbol=symbol,
                )
        except Exception as exc:
            logger.warning(
                "manual_intervention_lock_get_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return {
                "reason": "manual_intervention_lock_lookup_failed",
                "created_ts": 0.0,
                "expire_ts": 0.0,
                "context": {"lookup_failed": True, "error": str(exc)},
            }

    async def _has_unresolved_symbol_quarantine(
        self,
        *,
        symbol: str,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        strategy_instance_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        ib_perm_id: Optional[Any] = None,
    ) -> bool:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return False
        intent = {
            "account_id": str(account_id or "").strip(),
            "strategy_id": str(strategy_id or "").strip(),
            "strategy_instance_id": str(strategy_instance_id or "").strip(),
            "intent_id": str(intent_id or "").strip(),
            "client_order_id": str(client_order_id or "").strip(),
            "broker_order_id": str(broker_order_id or "").strip(),
            "ib_perm_id": ib_perm_id,
            "symbol": sym,
        }

        async def _fetch_legacy_quarantine_rows() -> list:
            fetchall = getattr(self.pg, "fetchall", None)
            query = """
                SELECT NULL::text AS quarantine_scope, NULL::text AS account_id,
                       NULL::text AS strategy_id, NULL::text AS strategy_instance_id,
                       NULL::text AS intent_id, order_ref AS client_order_id,
                       broker_order_id, ib_perm_id, perm_id, symbol, status
                FROM core.core_order_quarantine
                WHERE status = 'active'
                  AND (
                      symbol = $1
                      OR broker_order_id = $2
                      OR ib_perm_id = $3
                      OR perm_id = $3
                      OR order_ref = $4
                  )
                LIMIT 50
            """
            if callable(fetchall):
                raw_rows = fetchall(
                    query,
                    sym,
                    intent["broker_order_id"] or None,
                    ib_perm_id,
                    intent["client_order_id"] or None,
                )
                if asyncio.iscoroutine(raw_rows):
                    raw_rows = await raw_rows
                return list(raw_rows or [])
            row = await self.pg.fetchone(query, sym, intent["broker_order_id"] or None, ib_perm_id, intent["client_order_id"] or None)
            return [row] if row else []

        def _is_legacy_schema_error(exc: Exception) -> bool:
            text = str(exc).lower()
            return "does not exist" in text and any(
                column in text
                for column in (
                    "quarantine_scope",
                    "account_id",
                    "strategy_id",
                    "strategy_instance_id",
                    "intent_id",
                    "client_order_id",
                )
            )

        try:
            fetchall = getattr(self.pg, "fetchall", None)
            rows = []
            if callable(fetchall):
                raw_rows = fetchall(
                    """
                    SELECT quarantine_scope, account_id, strategy_id, strategy_instance_id,
                           intent_id, client_order_id, broker_order_id, ib_perm_id, perm_id,
                           symbol, status
                    FROM core.core_order_quarantine
                    WHERE status = 'active'
                      AND (
                          quarantine_scope = 'GLOBAL'
                          OR account_id = $1
                          OR symbol = $2
                          OR strategy_id = $3
                          OR strategy_instance_id = $4
                          OR intent_id = $5
                          OR client_order_id = $6
                          OR broker_order_id = $7
                          OR ib_perm_id = $8
                          OR perm_id = $8
                          OR quarantine_scope IS NULL
                      )
                    LIMIT 50
                    """,
                    intent["account_id"] or None,
                    sym,
                    intent["strategy_id"] or None,
                    intent["strategy_instance_id"] or None,
                    intent["intent_id"] or None,
                    intent["client_order_id"] or None,
                    intent["broker_order_id"] or None,
                    ib_perm_id,
                )
                if asyncio.iscoroutine(raw_rows):
                    raw_rows = await raw_rows
                rows = list(raw_rows or [])
            if not rows:
                row = await self.pg.fetchone(
                    """
                    SELECT quarantine_scope, account_id, strategy_id, strategy_instance_id,
                           intent_id, client_order_id, broker_order_id, ib_perm_id, perm_id,
                           symbol, status, 1 AS has_active
                    FROM core.core_order_quarantine
                    WHERE status = 'active'
                      AND (symbol = $1 OR quarantine_scope = 'GLOBAL')
                    LIMIT 1
                    """,
                    sym,
                )
                rows = [row] if row else []
            normalized_rows = []
            for row in rows:
                if isinstance(row, dict) and row.get("has_active") and not row.get("symbol"):
                    legacy_row = dict(row)
                    legacy_row.setdefault("symbol", sym)
                    legacy_row.setdefault("status", "active")
                    normalized_rows.append(legacy_row)
                else:
                    normalized_rows.append(row)
            return any(
                quarantine_matches_intent(row, intent, live_mode=bool(self.enable_execution))
                for row in normalized_rows
            )
        except Exception as exc:
            if not _is_legacy_schema_error(exc):
                logger.warning("execution_quarantine_lookup_failed", symbol=sym, error=str(exc))
                return True
            try:
                rows = await _fetch_legacy_quarantine_rows()
                return any(
                    quarantine_matches_intent(row, intent, live_mode=bool(self.enable_execution))
                    for row in rows
                )
            except Exception:
                logger.warning("execution_quarantine_lookup_failed", symbol=sym, error=str(exc))
                return True

    async def _has_unresolved_quarantine_for_intent(self, intent: Any) -> bool:
        return await self._has_unresolved_symbol_quarantine(
            symbol=getattr(intent, "symbol", None) if not isinstance(intent, dict) else intent.get("symbol"),
            account_id=getattr(intent, "account_id", None) if not isinstance(intent, dict) else intent.get("account_id"),
            strategy_id=getattr(intent, "strategy_id", None) if not isinstance(intent, dict) else intent.get("strategy_id"),
            strategy_instance_id=getattr(intent, "strategy_instance_id", None) if not isinstance(intent, dict) else intent.get("strategy_instance_id"),
            intent_id=getattr(intent, "intent_id", None) if not isinstance(intent, dict) else intent.get("intent_id"),
            client_order_id=getattr(intent, "client_order_id", None) if not isinstance(intent, dict) else intent.get("client_order_id"),
            broker_order_id=getattr(intent, "broker_order_id", None) if not isinstance(intent, dict) else intent.get("broker_order_id"),
            ib_perm_id=(
                getattr(intent, "ib_perm_id", None)
                if not isinstance(intent, dict)
                else intent.get("ib_perm_id") or intent.get("perm_id")
            ),
        )

    async def ensure_startup_ready(self) -> None:
        await assert_ib_order_truth_schema(self.pg, source="execution_service")

    async def _evaluate_trading_gate(
        self,
        *,
        symbol: str,
        symbol_quarantine_active: bool,
    ) -> tuple[bool, str, Dict[str, Any]]:
        gate_meta: Dict[str, Any] = {
            "symbol": str(symbol or "").strip().upper(),
            "exec_ready": True,
            "market_ready": True,
            "master_truth_complete": True,
            "no_unresolved_quarantine_for_symbol": not bool(symbol_quarantine_active),
            "upstream_trading_enabled": True,
        }
        provider = self.trading_gate_provider
        if provider is not None:
            try:
                raw = provider(str(symbol or "").strip().upper())
                if asyncio.iscoroutine(raw):
                    raw = await raw
                if isinstance(raw, dict):
                    gate_meta["exec_ready"] = bool(raw.get("exec_ready", True))
                    gate_meta["market_ready"] = bool(raw.get("market_ready", True))
                    gate_meta["master_truth_complete"] = bool(raw.get("master_truth_complete", True))
                    gate_meta["upstream_trading_enabled"] = bool(
                        raw.get(
                            "trading_enabled",
                            gate_meta["exec_ready"] and gate_meta["market_ready"] and gate_meta["master_truth_complete"],
                        )
                    )
            except Exception as exc:
                gate_meta["exec_ready"] = False
                gate_meta["market_ready"] = False
                gate_meta["master_truth_complete"] = False
                gate_meta["upstream_trading_enabled"] = False
                gate_meta["provider_error"] = str(exc)
                return False, "trading_gate_provider_failed", gate_meta

        trading_enabled = bool(
            gate_meta["upstream_trading_enabled"]
            and gate_meta["no_unresolved_quarantine_for_symbol"]
        )
        gate_meta["trading_enabled"] = trading_enabled
        if trading_enabled:
            return True, "ok", gate_meta
        if symbol_quarantine_active:
            return False, "symbol_quarantined", gate_meta
        return False, "trading_gate_not_ready", gate_meta

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

    def get_stats(self) -> Dict[str, Any]:
        """Get execution statistics"""
        return self.stats.copy()

    @staticmethod
    def _classify_safety_action(extras: Dict[str, Any]) -> Optional[str]:
        if bool((extras or {}).get("flatten_action")):
            return "flatten_action"
        if bool((extras or {}).get("protective_stop")):
            return "protective_stop"
        if bool((extras or {}).get("close_only")):
            return "close_only"
        return None

    def _publish_order_notification(
        self,
        *,
        decision_id: str,
        strategy_id: str,
        symbol: str,
        plan: ExecPlan,
        placed_order: PlacedOrder,
        envelope_extras: Dict[str, Any],
    ) -> None:
        if self.notification_manager is None:
            return

        try:
            raw_reason = str(envelope_extras.get("reason") or "")
            price = plan.limit_px if plan.limit_px is not None else plan.stop_px
            amount_usd = float(plan.qty) * float(price) if (price is not None and plan.qty is not None) else None
            order_role = self._classify_order_role(envelope_extras, placed_order.order_ref, plan.order_type)
            if self._should_suppress_order_notification(order_role=order_role, reason=raw_reason):
                logger.info(
                    "order_notification_suppressed",
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    order_role=order_role,
                    reason=raw_reason,
                )
                return
            event = NotificationEvent(
                event_type="order_submitted",
                status="SUBMITTED",
                strategy_id=strategy_id,
                symbol=symbol,
                side=str(plan.side or ""),
                qty=float(plan.qty or 0.0),
                order_type=str(plan.order_type or ""),
                decision_id=decision_id,
                order_id=placed_order.order_id,
                broker_order_id=placed_order.broker_order_id or str(placed_order.order_id),
                perm_id=placed_order.perm_id,
                order_ref=str(placed_order.order_ref or ""),
                order_role=order_role,
                price=float(price) if price is not None else None,
                amount_usd=amount_usd,
                reason=raw_reason,
                metadata={
                    "tif": plan.tif,
                    "limit_px": plan.limit_px,
                    "stop_px": plan.stop_px,
                    "order_ref": placed_order.order_ref,
                },
            )
            self.notification_manager.publish(event)
        except Exception as exc:
            logger.warning(
                "order_notification_publish_failed",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )

    @staticmethod
    def _should_suppress_order_notification(*, order_role: str, reason: str) -> bool:
        rs = str(reason or "").strip().lower()
        role = str(order_role or "").strip().upper()
        # Keep self-healing protective stops internal to avoid noisy mobile pushes.
        if rs == "audit_self_heal" and role in {"PROTECTIVE_STOP", "STOP_LOSS"}:
            return True
        return False

    @staticmethod
    def _classify_order_role(extras: Dict[str, Any], order_ref: str, order_type: str) -> str:
        reason = str(extras.get("reason") or "").lower()
        ref = str(order_ref or "").upper()
        otype = str(order_type or "").upper()

        if bool(extras.get("flatten_action")) or ("flatten" in reason) or (":FLATTEN" in ref):
            return "FLATTEN"
        if bool(extras.get("protective_stop")) or ("stop" in reason and "protective" in reason):
            return "PROTECTIVE_STOP"
        if ":TP" in ref or "take_profit" in reason or "tp" in reason:
            return "TAKE_PROFIT"
        if ":STOP" in ref or otype in {"STP", "STP_LMT", "STOP", "STOP_LIMIT"}:
            return "STOP_LOSS"
        if ":ENTRY" in ref or "entry" in reason:
            return "ENTRY"
        return "ORDER"

    async def shutdown(self) -> None:
        """Shutdown execution service"""
        # Flush in-flight order-event tasks to reduce event-loss / pending-task warnings.
        self._event_emit_stopping = True
        self._event_emit_overflow.clear()
        while self._event_emit_tasks:
            pending = list(self._event_emit_tasks)
            done, still_pending = await asyncio.wait(pending, timeout=1.0)
            for task in still_pending:
                task.cancel()
            if still_pending:
                logger.warning(
                    "execution_event_emit_tasks_cancelled_on_shutdown",
                    pending=len(still_pending),
                    done=len(done),
                )
                await asyncio.gather(*still_pending, return_exceptions=True)

        if self.notification_manager is not None:
            try:
                await self.notification_manager.shutdown()
            except Exception as exc:
                logger.warning("notification_manager_shutdown_failed", error=str(exc))
        logger.info("ExecutionService shutting down")
