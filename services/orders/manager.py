from __future__ import annotations

import copy
import inspect
import os
import time
from typing import Any, Dict, Optional

from libs.logger import get_logger
from services.exec.execution_service import ExecutionResult
from .ledger import PositionLedger
from .protection import build_protection_metadata

logger = get_logger(__name__)


class UnifiedOrderManager:
    """
    Phase-2 outer shell around ExecutionService.

    Current scope:
    - normalize protection metadata before execution
    - no-op duplicate protective stop intents for same lot/qty band/price band
    - delegate everything else to ExecutionService
    """

    def __init__(
        self,
        *,
        execution_service: Any,
        system_health_controller: Any = None,
        position_ledger: Optional[PositionLedger] = None,
        trading_gate_provider: Any = None,
        strategy_admission_provider: Any = None,
        flip_guard: Any = None,
    ) -> None:
        self.execution_service = execution_service
        self.pg = getattr(execution_service, "pg", None)
        self.system_health_controller = system_health_controller
        self.position_ledger = position_ledger or (PositionLedger(pg=self.pg) if self.pg is not None else None)
        self.trading_gate_provider = (
            trading_gate_provider
            or getattr(execution_service, "trading_gate_provider", None)
            or getattr(execution_service, "_trading_gate_provider", None)
        )
        self.strategy_admission_provider = strategy_admission_provider
        self.flip_guard = flip_guard or getattr(execution_service, "flip_guard", None)
        self.manual_intervention_lock_store = getattr(execution_service, "manual_intervention_lock_store", None)

    async def ensure_startup_ready(self) -> None:
        ensure = getattr(self.execution_service, "ensure_startup_ready", None)
        if callable(ensure):
            await ensure()

    async def submit_entry(self, result: Any) -> ExecutionResult:
        return await self._execute_with_kind(result, intent_kind="entry")

    async def submit_exit(self, result: Any) -> ExecutionResult:
        return await self._execute_with_kind(result, intent_kind="exit")

    async def submit_flatten(self, result: Any) -> ExecutionResult:
        return await self._execute_with_kind(result, intent_kind="flatten")

    async def ensure_protection(self, result: Any) -> ExecutionResult:
        return await self._execute_with_kind(result, intent_kind="protection")

    async def cancel_order(self, result: Any) -> ExecutionResult:
        return await self._execute_with_kind(result, intent_kind="cancel")

    async def execute(self, result: Any) -> ExecutionResult:
        intent_kind = self._classify_result(result)
        if intent_kind == "entry":
            return await self.submit_entry(result)
        if intent_kind == "flatten":
            return await self.submit_flatten(result)
        if intent_kind == "protection":
            return await self.ensure_protection(result)
        if intent_kind == "exit":
            return await self.submit_exit(result)
        return await self._execute_with_kind(result, intent_kind=intent_kind)

    async def _execute_with_kind(self, result: Any, *, intent_kind: str) -> ExecutionResult:
        metadata = dict(getattr(result, "metadata", {}) or {})
        envelope_extras = dict(metadata.get("envelope_extras") or {})
        plan = getattr(result, "plan", None)
        if plan is not None:
            envelope_extras = self._normalize_entry_protection(result=result, envelope_extras=envelope_extras)
            envelope_extras.setdefault("uom_intent_kind", intent_kind)
            preflight_gate = await self._preflight_gates(
                result=result,
                envelope_extras=envelope_extras,
                intent_kind=intent_kind,
            )
            if preflight_gate is not None:
                return preflight_gate
            ledger_gate = await self._ledger_truth_gate(result=result, envelope_extras=envelope_extras, intent_kind=intent_kind)
            if ledger_gate is not None:
                return ledger_gate
            duplicate = await self._maybe_noop_duplicate_protection(result=result, envelope_extras=envelope_extras)
            if duplicate is not None:
                return duplicate
            metadata["envelope_extras"] = envelope_extras
            result.metadata = metadata
        outcome = await self.execution_service.execute(result)
        if intent_kind == "protection" and bool(getattr(outcome, "success", False)):
            await self._register_protection_order(
                result=result,
                envelope_extras=envelope_extras,
                outcome=outcome,
            )
        return outcome

    async def _preflight_gates(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        health_gate = self._system_health_gate(
            result=result,
            envelope_extras=envelope_extras,
            intent_kind=intent_kind,
        )
        if health_gate is not None:
            return health_gate
        strategy_gate = await self._strategy_admission_gate(
            result=result,
            envelope_extras=envelope_extras,
            intent_kind=intent_kind,
        )
        if strategy_gate is not None:
            return strategy_gate
        broker_gate = await self._broker_truth_gate(
            result=result,
            envelope_extras=envelope_extras,
            intent_kind=intent_kind,
        )
        if broker_gate is not None:
            return broker_gate
        quarantine_gate = await self._broker_quarantine_gate(
            result=result,
            envelope_extras=envelope_extras,
            intent_kind=intent_kind,
        )
        if quarantine_gate is not None:
            return quarantine_gate
        pending_recovery_gate = await self._pending_recovery_safety_gate(
            result=result,
            envelope_extras=envelope_extras,
            intent_kind=intent_kind,
        )
        if pending_recovery_gate is not None:
            return pending_recovery_gate
        flip_gate = await self._flip_guard_gate(
            result=result,
            envelope_extras=envelope_extras,
            intent_kind=intent_kind,
        )
        if flip_gate is not None:
            return flip_gate
        pending_gate = await self._pending_order_gate(
            result=result,
            envelope_extras=envelope_extras,
            intent_kind=intent_kind,
        )
        if pending_gate is not None:
            return pending_gate
        return None

    def _classify_result(self, result: Any) -> str:
        metadata = dict(getattr(result, "metadata", {}) or {})
        extras = dict(metadata.get("envelope_extras") or {})
        explicit_kind = str(extras.get("uom_intent_kind") or "").strip().lower()
        if explicit_kind in {"entry", "exit", "flatten", "protection", "cancel"}:
            return explicit_kind
        if bool(extras.get("flatten_action")):
            return "flatten"
        if self._is_protection_intent(extras):
            return "protection"
        intent_action = str(
            extras.get("intent_action")
            or extras.get("action_type")
            or extras.get("order_role")
            or ""
        ).strip().lower()
        if intent_action in {"entry", "add", "open", "buy_to_open", "sell_to_open"}:
            return "entry"
        if intent_action in {"exit", "reduce", "close", "buy_to_close", "sell_to_close"}:
            return "exit"
        plan = getattr(result, "plan", None)
        if bool(extras.get("close_only")):
            return "exit"
        if plan is not None:
            side = str(getattr(plan, "side", "") or "").strip().upper()
            if side in {"BUY", "SELL", "LONG", "SHORT", "CALL", "PUT"}:
                return "entry"
        return "entry"

    @staticmethod
    def _is_protection_intent(envelope_extras: Dict[str, Any]) -> bool:
        extras = dict(envelope_extras or {})
        if bool(extras.get("protective_stop") or extras.get("uom_protection")):
            return True
        kind = str(extras.get("protection_kind") or "").strip().lower()
        return kind in {"protective_stop", "stop_loss", "take_profit", "target"}

    @staticmethod
    def _is_safety_intent_kind(intent_kind: str) -> bool:
        return str(intent_kind or "").strip().lower() in {"cancel", "protection", "exit", "flatten"}

    @classmethod
    def _is_broker_safety_lane(cls, *, intent_kind: str, envelope_extras: Dict[str, Any]) -> bool:
        kind = str(intent_kind or "").strip().lower()
        if kind in {"cancel", "protection", "flatten"}:
            return True
        if kind != "exit":
            return False
        extras = dict(envelope_extras or {})
        return bool(
            extras.get("close_only")
            or extras.get("flatten_action")
            or cls._is_protection_intent(extras)
            or abs(cls._position_qty_hint(extras)) > 1e-9
        )

    @staticmethod
    def _position_qty_hint(envelope_extras: Dict[str, Any]) -> float:
        extras = dict(envelope_extras or {})
        for key in (
            "broker_position_qty_hint",
            "broker_qty",
            "ib_position_qty",
            "position_qty_hint",
        ):
            raw = extras.get(key)
            if raw is None:
                continue
            try:
                qty = float(raw or 0.0)
            except Exception:
                continue
            if abs(qty) > 1e-9:
                return qty
        return 0.0

    @staticmethod
    def _protection_price(plan: Any, envelope_extras: Dict[str, Any]) -> Any:
        if plan is not None:
            for attr in ("stop_px", "limit_px", "target_px"):
                value = getattr(plan, attr, None)
                if value is not None:
                    return value
        for key in ("stop_px", "limit_px", "target_px", "take_profit_px", "ref_px"):
            value = envelope_extras.get(key)
            if value is not None:
                return value
        return None

    def _system_health_gate(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        if intent_kind == "cancel":
            return None
        controller = self.system_health_controller
        if controller is None:
            return None
        state = str(getattr(controller, "state", "") or "").strip().lower()
        if state not in {"paused", "degraded", "recovering", "halted", "system_paused", "broker_degraded"}:
            return None
        smoke_degraded_bypass = (
            intent_kind == "entry"
            and state in {"degraded", "recovering"}
            and bool(envelope_extras.get("paper_smoke_only"))
            and bool(envelope_extras.get("allow_system_health_degraded_entry"))
            and str(os.getenv("TRADING_MODE", "")).strip().lower() == "paper"
        )
        if smoke_degraded_bypass:
            logger.warning(
                "uom_system_health_gate_bypassed_for_paper_smoke",
                decision_id=str(getattr(result, "decision_id", "") or ""),
                strategy_id=str(getattr(result, "strategy_id", "") or ""),
                state=state,
                intent_kind=intent_kind,
            )
            return None
        if self._is_broker_safety_lane(intent_kind=intent_kind, envelope_extras=envelope_extras):
            return None
        return ExecutionResult(
            success=False,
            decision_id=str(getattr(result, "decision_id", "") or ""),
            reason=f"system_health_gate_blocked:{state}",
            stage="uom_system_health_gate",
            metadata={"state": state, "intent_kind": intent_kind},
        )

    async def _strategy_admission_gate(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        if intent_kind == "cancel" or bool(envelope_extras.get("skip_uom_strategy_admission_gate")):
            return None
        if self._is_broker_safety_lane(intent_kind=intent_kind, envelope_extras=envelope_extras) and abs(self._position_qty_hint(envelope_extras)) > 1e-9:
            envelope_extras["uom_strategy_admission_bypassed_for_safety_repair"] = True
            return None
        strategy_id = str(getattr(result, "strategy_id", "") or "").strip()
        symbol = str(getattr(result, "symbol", "") or "").strip().upper()
        if not strategy_id or not symbol:
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="strategy_admission_missing_identity",
                stage="uom_strategy_admission_gate",
                metadata={"strategy_id": strategy_id, "symbol": symbol, "intent_kind": intent_kind},
            )
        provider = self.strategy_admission_provider
        if provider is None:
            return None
        try:
            raw = provider(
                strategy_id=strategy_id,
                symbol=symbol,
                intent_kind=intent_kind,
                result=result,
                envelope_extras=envelope_extras,
            )
            if inspect.isawaitable(raw):
                raw = await raw
        except TypeError:
            raw = provider(strategy_id, symbol, intent_kind)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="strategy_admission_provider_failed",
                stage="uom_strategy_admission_gate",
                metadata={"error": str(exc), "strategy_id": strategy_id, "symbol": symbol, "intent_kind": intent_kind},
            )
        allowed = True
        reason = "strategy_not_admitted"
        if isinstance(raw, dict):
            if "reason" in raw:
                reason = str(raw.get("reason") or reason)
            allowed = bool(raw.get("allowed", raw.get("admitted", raw.get("trading_enabled", True))))
        elif raw is not None:
            allowed = bool(raw)
        if allowed:
            return None
        return ExecutionResult(
            success=False,
            decision_id=str(getattr(result, "decision_id", "") or ""),
            reason=reason,
            stage="uom_strategy_admission_gate",
            metadata={"strategy_id": strategy_id, "symbol": symbol, "intent_kind": intent_kind},
        )

    async def _broker_quarantine_gate(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        if self.pg is None or intent_kind in {"cancel", "flatten"}:
            return None
        if bool(envelope_extras.get("skip_uom_broker_quarantine_gate")):
            return None
        symbol = str(getattr(result, "symbol", "") or "").strip().upper()
        if not symbol:
            return None
        blocking_reasons = {
            "unknown_broker_order_detected",
            "terminal_orphan_cancel_not_confirmed",
            "unattributed_broker_fill",
            "unattributed_broker_order_status",
            "broker_only_position_requires_manual_intervention",
        }
        try:
            row = await self.pg.fetchone(
                """
                SELECT reason, COUNT(*) AS active_count
                FROM core.core_order_quarantine
                WHERE symbol = $1
                  AND status = 'active'
                  AND reason = ANY($2::text[])
                GROUP BY reason
                ORDER BY COUNT(*) DESC, reason ASC
                LIMIT 1
                """,
                symbol,
                sorted(blocking_reasons),
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="broker_quarantine_gate_unreadable",
                stage="uom_broker_quarantine_gate",
                metadata={"symbol": symbol, "intent_kind": intent_kind, "error": str(exc)},
            )
        if not row:
            return None
        reason = str(row.get("reason") if hasattr(row, "get") else row[0])
        if reason not in blocking_reasons:
            return None
        try:
            active_count = int(row.get("active_count") if hasattr(row, "get") else row[1])
        except Exception:
            active_count = 1
        if active_count <= 0:
            return None
        return ExecutionResult(
            success=False,
            decision_id=str(getattr(result, "decision_id", "") or ""),
            reason="broker_truth_quarantine_active",
            stage="uom_broker_quarantine_gate",
            metadata={
                "symbol": symbol,
                "intent_kind": intent_kind,
                "quarantine_reason": reason,
                "active_count": active_count,
            },
        )

    async def _broker_truth_gate(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        if intent_kind == "cancel" or bool(envelope_extras.get("skip_uom_broker_truth_gate")):
            return None
        provider = self.trading_gate_provider
        if provider is None:
            return None
        symbol = str(getattr(result, "symbol", "") or "").strip().upper()
        try:
            raw = provider(symbol)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="broker_truth_provider_failed",
                stage="uom_broker_truth_gate",
                metadata={"error": str(exc), "symbol": symbol, "intent_kind": intent_kind},
            )
        if not isinstance(raw, dict):
            if raw is None or bool(raw):
                return None
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="broker_truth_not_ready",
                stage="uom_broker_truth_gate",
                metadata={"symbol": symbol, "intent_kind": intent_kind},
            )
        exec_ready = bool(raw.get("exec_ready", True))
        market_ready = bool(raw.get("market_ready", True))
        master_truth_complete = bool(raw.get("master_truth_complete", True))
        trading_enabled = bool(raw.get("trading_enabled", exec_ready and market_ready and master_truth_complete))
        if self._is_safety_intent_kind(intent_kind):
            if exec_ready and master_truth_complete:
                envelope_extras["uom_safety_lane"] = True
                envelope_extras["uom_safety_lane_market_ready"] = market_ready
                envelope_extras["uom_safety_lane_master_truth_complete"] = master_truth_complete
                return None
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="broker_truth_not_ready",
                stage="uom_broker_truth_gate",
                metadata={
                    "symbol": symbol,
                    "intent_kind": intent_kind,
                    "exec_ready": exec_ready,
                    "market_ready": market_ready,
                    "master_truth_complete": master_truth_complete,
                    "trading_enabled": trading_enabled,
                    "safety_lane": True,
                },
            )
        if trading_enabled and exec_ready and market_ready and master_truth_complete:
            return None
        return ExecutionResult(
            success=False,
            decision_id=str(getattr(result, "decision_id", "") or ""),
            reason="broker_truth_not_ready",
            stage="uom_broker_truth_gate",
            metadata={
                "symbol": symbol,
                "intent_kind": intent_kind,
                "exec_ready": exec_ready,
                "market_ready": market_ready,
                "master_truth_complete": master_truth_complete,
                "trading_enabled": trading_enabled,
            },
        )

    async def _flip_guard_gate(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        if intent_kind != "entry" or bool(envelope_extras.get("skip_uom_flip_guard")):
            return None
        if bool(getattr(self.execution_service, "flip_guard_enabled", True)) is False:
            return None
        if bool(getattr(self.execution_service, "enable_execution", True)) is False:
            return None
        guard = self.flip_guard
        if guard is None:
            return None
        classify = getattr(guard, "classify_action", None)
        try:
            action = classify(extras=envelope_extras) if callable(classify) else "entry"
        except Exception:
            action = "entry"
        if str(action or "entry") != "entry":
            return None
        try:
            hydrate = getattr(guard, "hydrate_from_pg", None)
            if callable(hydrate):
                hydrated = hydrate(self.pg)
                if inspect.isawaitable(hydrated):
                    await hydrated
        except Exception as exc:
            logger.warning("uom_flip_guard_pg_hydrate_failed", error=str(exc))
        plan = getattr(result, "plan", None)
        side = str(getattr(plan, "side", "") or "").strip().upper()
        if not side:
            return None
        ts_us = dict(getattr(result, "metadata", {}) or {}).get("ts_bar_end_us")
        envelope_extras.update(self._ensure_flip_guard_bar_id(envelope_extras, ts_us=ts_us))
        try:
            verdict = guard.check_open_allowed(
                strategy_id=str(getattr(result, "strategy_id", "") or ""),
                symbol=str(getattr(result, "symbol", "") or ""),
                side=side,
                extras=envelope_extras,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="flip_guard_unreadable",
                stage="uom_flip_guard_gate",
                metadata={"error": str(exc), "intent_kind": intent_kind},
            )
        if bool(getattr(verdict, "allowed", False)):
            return None
        reason = str(getattr(verdict, "reason", "") or "flip_guard_blocked")
        metadata = dict(getattr(verdict, "metadata", {}) or {})
        metadata.update({"intent_kind": intent_kind, "side": side})
        return ExecutionResult(
            success=False,
            decision_id=str(getattr(result, "decision_id", "") or ""),
            reason=reason,
            stage="uom_flip_guard_gate",
            metadata=metadata,
        )

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

    async def _pending_order_gate(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        if self.pg is None or intent_kind != "entry" or bool(envelope_extras.get("skip_uom_pending_order_gate")):
            return None
        strategy_id = str(getattr(result, "strategy_id", "") or "").strip()
        symbol = str(getattr(result, "symbol", "") or "").strip().upper()
        if not strategy_id or not symbol:
            return None
        try:
            row = await self.pg.fetchone(
                """
                SELECT order_id, broker_order_id, state, order_ref
                FROM core.core_order
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                strategy_id,
                symbol,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="pending_order_gate_unreadable",
                stage="uom_pending_order_gate",
                metadata={"error": str(exc), "strategy_id": strategy_id, "symbol": symbol, "intent_kind": intent_kind},
            )
        if not row:
            return None
        return ExecutionResult(
            success=False,
            decision_id=str(getattr(result, "decision_id", "") or ""),
            reason="pending_order_exists",
            stage="uom_pending_order_gate",
            metadata={
                "strategy_id": strategy_id,
                "symbol": symbol,
                "intent_kind": intent_kind,
                "existing_order_id": str(row.get("order_id") or ""),
                "existing_broker_order_id": str(row.get("broker_order_id") or ""),
                "existing_state": str(row.get("state") or ""),
            },
        )

    async def _pending_recovery_safety_gate(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        if self.pg is None or intent_kind not in {"flatten", "exit"}:
            return None
        if not bool(envelope_extras.get("runtime_recovery_action")):
            return None
        strategy_id = str(getattr(result, "strategy_id", "") or "").strip()
        symbol = str(getattr(result, "symbol", "") or "").strip().upper()
        if not strategy_id or not symbol:
            return None
        recovery_intent_id = str(
            envelope_extras.get("recovery_intent_id")
            or envelope_extras.get("intent_id")
            or getattr(result, "decision_id", "")
            or ""
        ).strip()
        try:
            row = await self.pg.fetchone(
                """
                SELECT order_id, broker_order_id, state, idempotency_key, order_ref
                FROM core.core_order
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                  AND $3::text <> ''
                  AND idempotency_key = $3
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                strategy_id,
                symbol,
                recovery_intent_id,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="pending_recovery_safety_gate_unreadable",
                stage="uom_pending_recovery_safety_gate",
                metadata={"error": str(exc), "strategy_id": strategy_id, "symbol": symbol, "intent_kind": intent_kind},
            )
        if not row:
            return None
        return ExecutionResult(
            success=True,
            decision_id=str(getattr(result, "decision_id", "") or ""),
            reason="uom_duplicate_recovery_safety_noop",
            stage="uom",
            order_id=row.get("order_id"),
            metadata={
                "strategy_id": strategy_id,
                "symbol": symbol,
                "intent_kind": intent_kind,
                "existing_order_id": str(row.get("order_id") or ""),
                "existing_broker_order_id": str(row.get("broker_order_id") or ""),
                "existing_state": str(row.get("state") or ""),
                "existing_idempotency_key": str(row.get("idempotency_key") or ""),
                "recovery_intent_id": recovery_intent_id,
            },
        )

    async def _ledger_truth_gate(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        intent_kind: str,
    ) -> Optional[ExecutionResult]:
        if self.position_ledger is None or intent_kind == "entry":
            return None
        if intent_kind == "protection":
            duplicate = await self._find_existing_protection(result=result, envelope_extras=envelope_extras)
            if duplicate is not None:
                return None
        if bool(envelope_extras.get("skip_uom_ledger_gate")):
            return None
        symbol = str(getattr(result, "symbol", "") or "").strip().upper()
        strategy_id = str(getattr(result, "strategy_id", "") or "").strip()
        if not symbol or not strategy_id:
            return None
        lot_id = envelope_extras.get("position_lot_id") or envelope_extras.get("position_id")
        try:
            current = await self.position_ledger.current_position(
                strategy_id=strategy_id,
                symbol=symbol,
                lot_id=lot_id,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="ledger_truth_unreadable",
                stage="uom_ledger_gate",
                metadata={"error": str(exc), "intent_kind": intent_kind},
            )
        plan = getattr(result, "plan", None)
        requested_qty = abs(float(getattr(plan, "qty", 0.0) or 0.0)) if plan is not None else 0.0
        ledger_qty = abs(float(current.get("qty") or 0.0))
        hinted_qty = abs(self._position_qty_hint(envelope_extras))
        if intent_kind in {"exit", "flatten", "protection"} and ledger_qty <= 1e-9:
            if self._is_broker_safety_lane(intent_kind=intent_kind, envelope_extras=envelope_extras):
                envelope_extras["uom_ledger_gate_bypassed_for_broker_truth_safety"] = True
                envelope_extras["uom_ledger_qty"] = ledger_qty
                if hinted_qty > 1e-9:
                    envelope_extras["uom_ledger_repair_from_position_hint"] = True
                    envelope_extras["uom_ledger_qty"] = hinted_qty
                    if requested_qty > hinted_qty + 1e-9:
                        envelope_extras["uom_ledger_qty_clamp_required"] = True
                return None
            if hinted_qty > 1e-9:
                envelope_extras["uom_ledger_repair_from_position_hint"] = True
                envelope_extras["uom_ledger_qty"] = hinted_qty
                if requested_qty > hinted_qty + 1e-9:
                    envelope_extras["uom_ledger_qty_clamp_required"] = True
                return None
            return ExecutionResult(
                success=False,
                decision_id=str(getattr(result, "decision_id", "") or ""),
                reason="ledger_truth_flat_no_close_allowed",
                stage="uom_ledger_gate",
                metadata={"ledger_position": current, "intent_kind": intent_kind},
            )
        if requested_qty > 0.0 and intent_kind in {"exit", "flatten", "protection"} and requested_qty > (ledger_qty + 1e-9):
            envelope_extras["uom_ledger_qty_clamp_required"] = True
            envelope_extras["uom_ledger_qty"] = ledger_qty
        return None

    async def _find_existing_protection(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if self.pg is None or not self._is_protection_intent(envelope_extras):
            return None
        plan = getattr(result, "plan", None)
        if plan is None:
            return None
        lot_id = str(envelope_extras.get("position_lot_id") or envelope_extras.get("position_id") or "").strip()
        if not lot_id:
            return None
        protection_kind = str(envelope_extras.get("protection_kind") or "protective_stop").strip().lower()
        price = self._protection_price(plan, envelope_extras)
        protection_meta = build_protection_metadata(
            account_id=str(os.getenv("IB_ACCOUNT", "") or "UNKNOWN_ACCOUNT"),
            strategy_id=str(getattr(result, "strategy_id", "") or ""),
            symbol=str(getattr(result, "symbol", "") or ""),
            lot_id=lot_id,
            protection_kind=protection_kind,
            side=str(getattr(plan, "side", "") or ""),
            qty=getattr(plan, "qty", 0.0),
            price=price,
            tick_size=envelope_extras.get("tick_size") or 0.01,
            configured_band_width=(envelope_extras.get("protection_band_width") or envelope_extras.get("price_band_width")),
        )
        envelope_extras.update(protection_meta)
        registry_row = await self.pg.fetchone(
            """
            SELECT protection_key, broker_order_id, order_ref, state
            FROM core.core_protection_order_registry
            WHERE protection_key = $1
              AND state IN ('NEW', 'SUBMITTED', 'PARTIAL', 'ACTIVE')
            LIMIT 1
            """,
            protection_meta["protection_key"],
        )
        if registry_row and registry_row.get("protection_key"):
            return {
                "order_id": registry_row.get("protection_key"),
                "idempotency_key": registry_row.get("protection_key"),
                "broker_order_id": registry_row.get("broker_order_id"),
            }
        return await self.pg.fetchone(
            """
            SELECT order_id, idempotency_key, broker_order_id
            FROM core.core_order
            WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL')
              AND note IS NOT NULL
              AND (note::jsonb -> 'extras' ->> 'protection_key') = $1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            protection_meta["protection_key"],
        )

    async def _register_protection_order(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
        outcome: ExecutionResult,
    ) -> None:
        if self.pg is None:
            return
        protection_key = str(envelope_extras.get("protection_key") or "").strip()
        if not protection_key:
            return
        plan = getattr(result, "plan", None)
        lot_id = str(envelope_extras.get("position_lot_id") or envelope_extras.get("position_id") or "").strip()
        desired_qty = abs(float(getattr(plan, "qty", 0.0) or 0.0)) if plan is not None else 0.0
        desired_price = self._protection_price(plan, envelope_extras)
        try:
            desired_price_v = float(desired_price) if desired_price is not None else None
        except Exception:
            desired_price_v = None
        try:
            await self.pg.execute(
                """
                INSERT INTO core.core_protection_order_registry (
                    protection_key, account_id, strategy_id, symbol, lot_id,
                    protection_kind, side, qty_bucket, price_band_id,
                    desired_qty, desired_price, broker_order_id, order_ref, state,
                    first_seen_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12, $13, 'SUBMITTED',
                    now(), now()
                )
                ON CONFLICT (protection_key) DO UPDATE
                SET broker_order_id = COALESCE(EXCLUDED.broker_order_id, core.core_protection_order_registry.broker_order_id),
                    order_ref = COALESCE(EXCLUDED.order_ref, core.core_protection_order_registry.order_ref),
                    state = EXCLUDED.state,
                    desired_qty = EXCLUDED.desired_qty,
                    desired_price = EXCLUDED.desired_price,
                    updated_at = now()
                """,
                protection_key,
                str(os.getenv("IB_ACCOUNT", "") or "UNKNOWN_ACCOUNT"),
                str(getattr(result, "strategy_id", "") or ""),
                str(getattr(result, "symbol", "") or "").strip().upper(),
                lot_id,
                str(envelope_extras.get("protection_kind") or "protective_stop"),
                str(getattr(plan, "side", "") or "").strip().upper() if plan is not None else "",
                str(envelope_extras.get("qty_bucket") or ""),
                str(envelope_extras.get("price_band_id") or ""),
                desired_qty,
                desired_price_v,
                int(getattr(outcome, "order_id", 0) or 0) or None,
                str(envelope_extras.get("order_ref") or protection_key)[:120],
            )
        except Exception as exc:
            logger.warning(
                "uom_protection_registry_write_failed",
                strategy_id=getattr(result, "strategy_id", None),
                symbol=getattr(result, "symbol", None),
                protection_key=protection_key,
                error=str(exc),
            )
            await self._lock_protection_registry_failure(
                result=result,
                protection_key=protection_key,
                error=str(exc),
            )

    async def _lock_protection_registry_failure(self, *, result: Any, protection_key: str, error: str) -> None:
        store = self.manual_intervention_lock_store
        strategy_id = str(getattr(result, "strategy_id", "") or "").strip()
        symbol = str(getattr(result, "symbol", "") or "").strip().upper()
        if store is None or not strategy_id or not symbol:
            return
        try:
            await store.lock(
                strategy_id=strategy_id,
                symbol=symbol,
                ttl_sec=24 * 60 * 60,
                reason="protection_registry_write_failed",
                context={
                    "protection_key": protection_key,
                    "error": error,
                    "source": "unified_order_manager",
                },
            )
        except Exception as lock_exc:
            logger.error(
                "uom_protection_registry_failure_lock_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                protection_key=protection_key,
                error=str(lock_exc),
            )

    def _normalize_entry_protection(self, *, result: Any, envelope_extras: Dict[str, Any]) -> Dict[str, Any]:
        extras = copy.deepcopy(dict(envelope_extras or {}))
        protective_oco = dict(extras.get("protective_oco") or {})
        lot_id = str(extras.get("position_lot_id") or extras.get("position_id") or "").strip()
        if lot_id and protective_oco:
            protective_oco.setdefault("position_lot_id", lot_id)
        if protective_oco:
            extras["protective_oco"] = protective_oco
        extras.setdefault("uom_managed", True)
        extras.setdefault("uom_version", "phase2_minimal")
        return extras

    async def _maybe_noop_duplicate_protection(
        self,
        *,
        result: Any,
        envelope_extras: Dict[str, Any],
    ) -> Optional[ExecutionResult]:
        if self.pg is None:
            return None
        if not self._is_protection_intent(envelope_extras):
            return None
        plan = getattr(result, "plan", None)
        if plan is None:
            return None
        lot_id = str(envelope_extras.get("position_lot_id") or envelope_extras.get("position_id") or "").strip()
        if not lot_id:
            return None
        existing = await self._find_existing_protection(result=result, envelope_extras=envelope_extras)
        if not existing:
            return None
        protection_key = str(envelope_extras.get("protection_key") or "")
        logger.info(
            "uom_duplicate_protection_noop",
            strategy_id=getattr(result, "strategy_id", None),
            symbol=getattr(result, "symbol", None),
            decision_id=getattr(result, "decision_id", None),
            protection_key=protection_key,
            existing_order_id=str(existing.get("order_id") or ""),
            existing_broker_order_id=str(existing.get("broker_order_id") or ""),
        )
        return ExecutionResult(
            success=True,
            decision_id=str(getattr(result, "decision_id", "") or ""),
            order_id=None,
            idempotency_key=str(existing.get("idempotency_key") or ""),
            reason="uom_duplicate_protection_noop",
            stage="uom",
            metadata={
                "protection_key": protection_key,
                "existing_order_id": str(existing.get("order_id") or ""),
                "existing_broker_order_id": str(existing.get("broker_order_id") or ""),
            },
        )
