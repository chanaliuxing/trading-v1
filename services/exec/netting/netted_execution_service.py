from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from libs.logger import get_logger
from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.models import PlacedOrder
from services.exec.order_event_writer import emit_order_event
from services.exec.trace import TraceContext, build_idempotency_key
from services.risk.risk_service import RiskService

from .coordinator import NettingCoordinator
from .models import NetChildIntent

logger = get_logger(__name__)


@dataclass
class NettedExecutionOutcome:
    success: bool
    decision_id: str
    order_id: Optional[int] = None
    idempotency_key: Optional[str] = None
    reason: str = ""
    stage: str = ""
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


@dataclass
class _PendingRequest:
    result: PipelineResult
    envelope_extras: Dict[str, Any]
    future: asyncio.Future
    idempotency_key: str
    trace_ctx: Optional[TraceContext] = None
    order_event_enabled: bool = True


class NettedExecutionService:
    """
    Symbol-level netting execution service.

    This service is runtime-safe by design:
    - It is only used when explicitly routed to "netted" mode.
    - Existing direct execution path remains unchanged.
    """

    def __init__(
        self,
        *,
        pg,
        risk_service: RiskService,
        safety_kernel,
        enable_execution: bool,
        notification_manager: Optional[Any] = None,
        batch_window_ms: int = 120,
        netting_strategy_id: str = "__netting__",
    ):
        self.pg = pg
        self.risk = risk_service
        self.kernel = safety_kernel
        self.enable_execution = bool(enable_execution)
        self.notification_manager = notification_manager
        self.batch_window_ms = max(0, int(batch_window_ms))
        self.netting_strategy_id = str(netting_strategy_id or "__netting__")

        self._coordinator = NettingCoordinator()
        self._pending: Dict[str, List[_PendingRequest]] = {}
        self._flush_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self,
        result: PipelineResult,
        envelope_extras: Optional[Dict[str, Any]] = None,
        trace_ctx: Optional[TraceContext] = None,
        order_event_enabled: bool = True,
    ) -> NettedExecutionOutcome:
        extras = dict(envelope_extras or {})
        idem = build_idempotency_key(
            strategy_id=str(result.strategy_id or ""),
            strategy_run_id=str((trace_ctx.strategy_run_id if trace_ctx else extras.get("strategy_run_id")) or ""),
            decision_id=str(result.decision_id or ""),
            symbol=str(result.symbol or ""),
            target_qty=((result.plan.qty if result.plan else 0.0)),
            ts_us=(result.metadata or {}).get("ts_bar_end_us"),
        )
        if trace_ctx is not None:
            trace_ctx.intent_id = idem
        req = _PendingRequest(
            result=result,
            envelope_extras=extras,
            future=asyncio.get_event_loop().create_future(),
            idempotency_key=idem,
            trace_ctx=trace_ctx,
            order_event_enabled=bool(order_event_enabled),
        )
        symbol = str(result.symbol or "").upper()
        if not symbol:
            return NettedExecutionOutcome(success=False, decision_id=result.decision_id, reason="missing_symbol", stage="validation")

        async with self._lock:
            bucket = self._pending.setdefault(symbol, [])
            bucket.append(req)
            if symbol not in self._flush_tasks or self._flush_tasks[symbol].done():
                self._flush_tasks[symbol] = asyncio.create_task(self._flush_symbol(symbol))

        return await req.future

    async def _flush_symbol(self, symbol: str) -> None:
        if self.batch_window_ms > 0:
            await asyncio.sleep(self.batch_window_ms / 1000.0)

        async with self._lock:
            reqs = self._pending.pop(symbol, [])
            self._flush_tasks.pop(symbol, None)

        if not reqs:
            return

        # Group by (symbol, side, order_type, tif) to avoid mixing incompatible plans in one parent order.
        groups: Dict[str, List[_PendingRequest]] = {}
        for req in reqs:
            plan = req.result.plan
            if plan is None:
                self._resolve_req(req, NettedExecutionOutcome(success=False, decision_id=req.result.decision_id, reason="missing_plan", stage="validation"))
                continue
            key = f"{req.result.symbol}|{plan.side}|{plan.order_type}|{plan.tif}"
            groups.setdefault(key, []).append(req)

        for grouped in groups.values():
            await self._process_group(grouped)

    async def _process_group(self, reqs: List[_PendingRequest]) -> None:
        if not reqs:
            return

        approved: List[_PendingRequest] = []
        intents: List[NetChildIntent] = []
        for req in reqs:
            plan = req.result.plan
            assert plan is not None
            risk_env = self._plan_to_envelope(
                plan=plan,
                symbol=req.result.symbol,
                strategy_id=req.result.strategy_id,
                decision_id=req.result.decision_id,
                envelope_extras=req.envelope_extras,
            )
            try:
                risk_decision = await self.risk.approve(envelope=risk_env, idempotency_key=req.idempotency_key)
            except Exception as exc:
                self._resolve_req(
                    req,
                    NettedExecutionOutcome(
                        success=False,
                        decision_id=req.result.decision_id,
                        idempotency_key=req.idempotency_key,
                        reason=f"risk_exception:{exc}",
                        stage="risk",
                    ),
                )
                continue

            if not risk_decision.approved:
                self._resolve_req(
                    req,
                    NettedExecutionOutcome(
                        success=False,
                        decision_id=req.result.decision_id,
                        idempotency_key=req.idempotency_key,
                        reason=risk_decision.reason,
                        stage="risk",
                        metadata={"hits": risk_decision.hits},
                    ),
                )
                continue

            approved.append(req)
            intents.append(
                NetChildIntent(
                    strategy_id=req.result.strategy_id,
                    decision_id=req.result.decision_id,
                    symbol=req.result.symbol,
                    side=plan.side,
                    qty=float(plan.qty),
                    submitted_at_us=int(time.time() * 1_000_000),
                    priority=int(req.envelope_extras.get("netting_priority", 50) or 50),
                    metadata={"idempotency_key": req.idempotency_key},
                )
            )

        if not approved:
            return

        decision = self._coordinator.build_group(intents)
        if decision.status != "NETTED" or decision.parent_plan is None:
            for req in approved:
                await self.risk.release(req.idempotency_key)
                self._resolve_req(
                    req,
                    NettedExecutionOutcome(
                        success=False,
                        decision_id=req.result.decision_id,
                        idempotency_key=req.idempotency_key,
                        reason=f"netting_rejected:{decision.reason}",
                        stage="netting",
                    ),
                )
            return

        sample_plan = approved[0].result.plan
        assert sample_plan is not None
        parent_idem = f"net:{decision.parent_plan.group_id}:{uuid.uuid4().hex[:6]}"

        try:
            parent_order_uuid = await self._persist_parent_new(
                symbol=decision.parent_plan.symbol,
                plan=sample_plan,
                group_id=decision.parent_plan.group_id,
                parent_idempotency_key=parent_idem,
            )
        except Exception as exc:
            for req in approved:
                await self.risk.release(req.idempotency_key)
                self._resolve_req(
                    req,
                    NettedExecutionOutcome(
                        success=False,
                        decision_id=req.result.decision_id,
                        idempotency_key=req.idempotency_key,
                        reason=f"parent_persist_failed:{exc}",
                        stage="database",
                    ),
                )
            return

        child_uuid_by_decision: Dict[str, str] = {}
        for req in approved:
            try:
                child_uuid = await self._persist_child_new(
                    parent_order_uuid=parent_order_uuid,
                    req=req,
                    group_id=decision.parent_plan.group_id,
                )
                child_uuid_by_decision[req.result.decision_id] = child_uuid
            except Exception as exc:
                await self.risk.release(req.idempotency_key)
                self._resolve_req(
                    req,
                    NettedExecutionOutcome(
                        success=False,
                        decision_id=req.result.decision_id,
                        idempotency_key=req.idempotency_key,
                        reason=f"child_persist_failed:{exc}",
                        stage="database",
                    ),
                )

        live_reqs = [r for r in approved if not r.future.done()]
        if not live_reqs:
            await self._mark_order_rejected(parent_idem, "all_children_failed")
            return

        if not self.enable_execution:
            for req in live_reqs:
                self._resolve_req(
                    req,
                    NettedExecutionOutcome(
                        success=True,
                        decision_id=req.result.decision_id,
                        idempotency_key=req.idempotency_key,
                        reason="Dry-run netted mode",
                        stage="dry_run",
                        metadata={
                            "netting_group_id": decision.parent_plan.group_id,
                            "parent_order_uuid": parent_order_uuid,
                            "child_order_uuid": child_uuid_by_decision.get(req.result.decision_id),
                        },
                    ),
                )
            return

        placed = await self._place_parent_order(plan=sample_plan, symbol=decision.parent_plan.symbol, group_id=decision.parent_plan.group_id)
        if placed is None:
            await self._mark_order_rejected(parent_idem, "Broker placement failed")
            for req in live_reqs:
                child_uuid = child_uuid_by_decision.get(req.result.decision_id)
                if child_uuid:
                    await self._mark_child_rejected(child_uuid, "Broker placement failed")
                await self.risk.release(req.idempotency_key)
                self._resolve_req(
                    req,
                    NettedExecutionOutcome(
                        success=False,
                        decision_id=req.result.decision_id,
                        idempotency_key=req.idempotency_key,
                        reason="Order placement failed",
                        stage="placement",
                    ),
                )
            return

        await self._update_parent_submitted(parent_idem, placed)
        try:
            self.kernel.register_idempotency_key(placed.order_id, parent_idem)
        except Exception:
            pass

        for req in live_reqs:
            child_uuid = child_uuid_by_decision.get(req.result.decision_id)
            if child_uuid:
                await self._update_child_submitted(child_uuid, group_id=decision.parent_plan.group_id)
            self._resolve_req(
                req,
                NettedExecutionOutcome(
                    success=True,
                    decision_id=req.result.decision_id,
                    order_id=placed.order_id,
                    idempotency_key=req.idempotency_key,
                    reason="Netted parent order placed",
                    stage="completed",
                    metadata={
                        "order_type": sample_plan.order_type,
                        "netting_group_id": decision.parent_plan.group_id,
                        "parent_order_id": placed.order_id,
                        "parent_order_uuid": parent_order_uuid,
                        "child_order_uuid": child_uuid,
                        "child_idempotency_key": req.idempotency_key,
                    },
                ),
            )
            if req.trace_ctx is not None:
                await self._emit_order_submitted_event_best_effort(
                    req=req,
                    child_uuid=child_uuid,
                    placed=placed,
                    parent_order_uuid=parent_order_uuid,
                    group_id=decision.parent_plan.group_id,
                )

    @staticmethod
    def _resolve_req(req: _PendingRequest, outcome: NettedExecutionOutcome) -> None:
        if not req.future.done():
            req.future.set_result(outcome)

    async def _emit_order_submitted_event_best_effort(
        self,
        *,
        req: _PendingRequest,
        child_uuid: Optional[str],
        placed: PlacedOrder,
        parent_order_uuid: Optional[str],
        group_id: str,
    ) -> None:
        if req.trace_ctx is None:
            return
        timeout_sec = 0.2
        try:
            await asyncio.wait_for(
                emit_order_event(
                    pg=self.pg,
                    enabled=bool(req.order_event_enabled),
                    event_type="order_submitted",
                    trace_id=req.trace_ctx.trace_id,
                    causation_id=req.trace_ctx.causation_id,
                    strategy_id=req.trace_ctx.strategy_id,
                    strategy_run_id=req.trace_ctx.strategy_run_id,
                    trigger_id=req.trace_ctx.trigger_id,
                    decision_id=req.trace_ctx.decision_id,
                    intent_id=req.idempotency_key,
                    order_id=str(child_uuid) if child_uuid else None,
                    broker_order_id=str(placed.order_id),
                    parent_order_id=parent_order_uuid,
                    symbol=str(req.result.symbol or ""),
                    payload={"mode": "netted", "group_id": group_id},
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "netted_order_event_emit_timeout",
                strategy_id=req.result.strategy_id,
                decision_id=req.result.decision_id,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            logger.warning(
                "netted_order_event_emit_failed",
                strategy_id=req.result.strategy_id,
                decision_id=req.result.decision_id,
                error=str(exc),
            )

    def _plan_to_envelope(
        self,
        plan: ExecPlan,
        symbol: str,
        strategy_id: str,
        decision_id: str,
        envelope_extras: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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
                "kind": plan.order_type,
                "limit_px": plan.limit_px,
                "stop_px": plan.stop_px,
                "tif": plan.tif,
            },
            "extras": extras,
        }

    async def _ensure_strategy(self, strategy_id: str) -> None:
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

    async def _persist_parent_new(self, *, symbol: str, plan: ExecPlan, group_id: str, parent_idempotency_key: str) -> str:
        await self._ensure_strategy(self.netting_strategy_id)
        note = json.dumps({"netting_parent": True, "netting_group_id": group_id}, ensure_ascii=True)
        row = await self.pg.fetchone(
            """
            INSERT INTO core.core_order (
                order_id, strategy_id, symbol, idempotency_key,
                side, order_type, qty, limit_px, stop_px, tif,
                state, note, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), $1, $2, $3,
                $4, $5, $6, $7, $8, $9,
                'NEW', $10, now(), now()
            )
            RETURNING order_id
            """,
            self.netting_strategy_id,
            symbol,
            parent_idempotency_key,
            plan.side,
            plan.order_type,
            float(plan.qty),
            plan.limit_px,
            plan.stop_px,
            plan.tif,
            note,
        )
        return str(row["order_id"])

    async def _persist_child_new(self, *, parent_order_uuid: str, req: _PendingRequest, group_id: str) -> str:
        await self._ensure_strategy(req.result.strategy_id)
        plan = req.result.plan
        assert plan is not None
        note = json.dumps(
            {
                "decision_id": req.result.decision_id,
                "netting_child": True,
                "netting_group_id": group_id,
                "extras": req.envelope_extras,
                "trace": req.trace_ctx.to_dict() if req.trace_ctx else {},
            },
            ensure_ascii=True,
        )
        row = await self.pg.fetchone(
            """
            INSERT INTO core.core_order (
                order_id, strategy_id, symbol, idempotency_key,
                side, order_type, qty, limit_px, stop_px, tif,
                state, parent_order_id, note, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), $1, $2, $3,
                $4, $5, $6, $7, $8, $9,
                'NEW', $10::uuid, $11, now(), now()
            )
            RETURNING order_id
            """,
            req.result.strategy_id,
            req.result.symbol,
            req.idempotency_key,
            plan.side,
            plan.order_type,
            float(plan.qty),
            plan.limit_px,
            plan.stop_px,
            plan.tif,
            parent_order_uuid,
            note,
        )
        return str(row["order_id"])

    async def _place_parent_order(self, *, plan: ExecPlan, symbol: str, group_id: str) -> Optional[PlacedOrder]:
        try:
            attach_oco = True
            order_ref = f"nt:{symbol}:{group_id[:8]}"
            return await self.kernel.place_entry_order(
                strategy_id=self.netting_strategy_id,
                symbol=symbol,
                side=plan.side,
                qty=float(plan.qty),
                order_type=plan.order_type,
                limit_px=plan.limit_px,
                stop_px=plan.stop_px,
                tif=plan.tif,
                order_ref=order_ref,
                attach_oco=attach_oco,
            )
        except Exception as exc:
            logger.error("netting_parent_place_failed", symbol=symbol, error=str(exc))
            return None

    async def _update_parent_submitted(self, parent_idempotency_key: str, placed: PlacedOrder) -> None:
        await self.pg.execute(
            """
            UPDATE core.core_order
            SET broker_order_id = $1,
                ib_perm_id = $2,
                order_ref = $3,
                state = 'SUBMITTED',
                submitted_at = now(),
                updated_at = now()
            WHERE idempotency_key = $4
            """,
            str(placed.order_id),
            placed.perm_id,
            placed.order_ref,
            parent_idempotency_key,
        )

    async def _update_child_submitted(self, child_order_uuid: str, *, group_id: str) -> None:
        await self.pg.execute(
            """
            UPDATE core.core_order
            SET state = 'SUBMITTED',
                order_ref = COALESCE(order_ref, $1),
                submitted_at = now(),
                updated_at = now()
            WHERE order_id = $2::uuid
            """,
            f"netchild:{group_id[:8]}",
            child_order_uuid,
        )

    async def _mark_order_rejected(self, idempotency_key: str, reason: str) -> None:
        note = json.dumps({"reject_reason": reason}, ensure_ascii=True)
        await self.pg.execute(
            """
            UPDATE core.core_order
            SET state = 'REJECTED',
                note = $1,
                updated_at = now()
            WHERE idempotency_key = $2
            """,
            note,
            idempotency_key,
        )

    async def _mark_child_rejected(self, child_order_uuid: str, reason: str) -> None:
        note = json.dumps({"reject_reason": reason}, ensure_ascii=True)
        await self.pg.execute(
            """
            UPDATE core.core_order
            SET state = 'REJECTED',
                note = $1,
                updated_at = now()
            WHERE order_id = $2::uuid
            """,
            note,
            child_order_uuid,
        )
