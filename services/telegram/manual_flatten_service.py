from __future__ import annotations

import asyncio
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.options.internal_symbol import internal_opt_symbol_from_ib_contract
from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.models import FlattenScope, OrderRole
from services.exec.order_event_writer import emit_order_event

logger = get_logger(__name__)


class _ClickHouseLastCloseProvider:
    async def get_last_close(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            from libs.db.ch_pool import get_client
        except Exception:
            return None

        sym = str(symbol or "").strip().upper()
        if not sym:
            return None

        def _query(sql: str) -> list[dict]:
            ch = get_client()
            return ch.query(sql, parameters={"symbol": sym})  # type: ignore[no-any-return]

        # Prefer intraday bars; if missing, fall back to daily close.
        sql_1m = """
        SELECT close, ts_end
        FROM bars_1m_ibkr FINAL
        WHERE symbol = {symbol:String}
          AND close > 0
          AND ts_end >= now() - INTERVAL 7 DAY
        ORDER BY ts_end DESC
        LIMIT 1
        """
        try:
            rows = await asyncio.to_thread(_query, sql_1m)
            if rows and float(rows[0].get("close") or 0) > 0:
                return {"px": float(rows[0]["close"]), "ts_end": rows[0].get("ts_end"), "source": "ch:bars_1m_close"}
        except Exception as exc:
            logger.warning("manual_flatten_ref_px_ch_1m_failed", symbol=sym, error=str(exc))

        sql_1d = """
        SELECT close, ts_end
        FROM bars_1d_ibkr FINAL
        WHERE symbol = {symbol:String}
          AND close > 0
          AND ts_end >= now() - INTERVAL 30 DAY
        ORDER BY ts_end DESC
        LIMIT 1
        """
        try:
            rows = await asyncio.to_thread(_query, sql_1d)
            if rows and float(rows[0].get("close") or 0) > 0:
                return {"px": float(rows[0]["close"]), "ts_end": rows[0].get("ts_end"), "source": "ch:bars_1d_close"}
        except Exception as exc:
            logger.warning("manual_flatten_ref_px_ch_1d_failed", symbol=sym, error=str(exc))

        return None


@dataclass
class ManualFlattenOrderResult:
    account_id: str
    strategy_id: str
    strategy_instance_id: str
    symbol: str
    lot_id: str
    qty: float
    success: bool
    decision_id: str
    reason: str = ""
    stage: str = ""
    order_role: str = ""
    flatten_scope: str = ""
    close_only: bool = True
    reduce_only: bool = True
    order_id: Optional[int] = None
    idempotency_key: Optional[str] = None
    side: Optional[str] = None
    session: Optional[str] = None


@dataclass
class ManualFlattenResult:
    intervention_id: str
    trigger_id: str
    source_trigger_id: str = ""
    scope_mode: str = "ACCOUNT_ALL"
    scope_warning: str = ""
    verification_state: str = "NOT_CHECKED"
    verified_flat: bool = False
    verified_targets: int = 0
    remaining_position_targets: int = 0
    remaining_open_orders: int = 0
    total_orders: int = 0
    success_orders: int = 0
    failed_orders: int = 0
    db_targets: int = 0
    broker_fallback_targets: int = 0
    position_reconcile_ran: bool = False
    db_position_query_ok: bool = True
    broker_position_query_ok: bool = True
    truth_query_errors: List[str] = field(default_factory=list)
    order_results: List[ManualFlattenOrderResult] = field(default_factory=list)


class ManualFlattenService:
    """Convert manual flatten command into strategy-traceable execution intents."""

    def __init__(
        self,
        *,
        pg: Any,
        execution_service: Any,
        ib_adapter: Any,
        runtime_reconciler: Optional[Any] = None,
        order_event_enabled: bool = True,
        fallback_strategy_id: str = "manual_telegram_flatten",
        last_close_provider: Optional[Any] = None,
    ) -> None:
        self.pg = pg
        self.execution_service = execution_service
        self.ib_adapter = ib_adapter
        self.runtime_reconciler = runtime_reconciler
        self.order_event_enabled = bool(order_event_enabled)
        self.fallback_strategy_id = str(fallback_strategy_id or "manual_telegram_flatten").strip()
        self._last_close_provider = last_close_provider or _ClickHouseLastCloseProvider()
        self._et_tz = ZoneInfo("America/New_York")
        self._offhours_limit_offset_bps = float(os.getenv("MANUAL_FLATTEN_OFFHOURS_LIMIT_OFFSET_BPS", "10.0"))
        self._overnight_limit_offset_bps = float(os.getenv("MANUAL_FLATTEN_OVERNIGHT_LIMIT_OFFSET_BPS", "1000.0"))
        self._allow_overnight_orders = os.getenv("MANUAL_FLATTEN_ALLOW_OVERNIGHT_ORDERS", "true").lower() != "false"
        # Unattended paper/live safety: flatten orders must be actively watched
        # and retried unless explicitly disabled for isolated tests.
        self._enable_flatten_watchdog = os.getenv("MANUAL_FLATTEN_WATCHDOG_ENABLED", "true").lower() != "false"
        self._watchdog_check_sec = max(1, int(os.getenv("MANUAL_FLATTEN_WATCHDOG_CHECK_SEC", "5")))
        self._watchdog_timeout_rth_sec = max(3, int(os.getenv("MANUAL_FLATTEN_WATCHDOG_RTH_TIMEOUT_SEC", "10")))
        self._watchdog_timeout_offhours_sec = max(5, int(os.getenv("MANUAL_FLATTEN_WATCHDOG_OFFHOURS_TIMEOUT_SEC", "30")))
        self._watchdog_max_retries = max(0, int(os.getenv("MANUAL_FLATTEN_WATCHDOG_MAX_RETRIES", "3")))
        self._watchdog_cancel_confirm_timeout_sec = max(
            5,
            int(os.getenv("MANUAL_FLATTEN_WATCHDOG_CANCEL_CONFIRM_TIMEOUT_SEC", "30")),
        )
        self._ref_anchor_max_dev_bps = max(
            100.0,
            float(os.getenv("MANUAL_FLATTEN_REF_ANCHOR_MAX_DEV_BPS", "2000.0")),
        )
        self._ref_quote_max_spread_bps = max(
            10.0,
            float(os.getenv("MANUAL_FLATTEN_REF_QUOTE_MAX_SPREAD_BPS", "1500.0")),
        )
        self._watchdog_tasks: Dict[str, Any] = {}

    async def execute_account_emergency_flatten(self, *, trigger_id: str) -> ManualFlattenResult:
        return await self._execute_flatten(trigger_id=trigger_id, strategy_id=None, source_trigger_id=None)

    async def execute_flatten_for_strategy(self, *, trigger_id: str, strategy_id: str) -> ManualFlattenResult:
        return await self._execute_flatten(
            trigger_id=trigger_id,
            strategy_id=str(strategy_id or "").strip(),
            source_trigger_id=None,
        )

    async def execute_flatten_for_source_trigger(
        self,
        *,
        trigger_id: str,
        source_trigger_id: str,
    ) -> ManualFlattenResult:
        return await self._execute_flatten(
            trigger_id=trigger_id,
            strategy_id=None,
            source_trigger_id=str(source_trigger_id or "").strip(),
        )

    async def _execute_flatten(
        self,
        *,
        trigger_id: str,
        strategy_id: Optional[str],
        source_trigger_id: Optional[str],
    ) -> ManualFlattenResult:
        intervention_id = str(uuid.uuid4())
        strategy_filter = str(strategy_id or "").strip() or None
        source_trigger_filter = str(source_trigger_id or "").strip() or None
        result = ManualFlattenResult(
            intervention_id=intervention_id,
            trigger_id=str(trigger_id or f"telegram:flatten:{intervention_id}"),
            source_trigger_id=source_trigger_filter or "",
            scope_mode=(
                "TRIGGER_NET_CAPPED"
                if source_trigger_filter
                else ("STRATEGY_ALL" if strategy_filter else "ACCOUNT_ALL")
            ),
            scope_warning=(
                "trigger-net capped close; not lot-exact"
                if source_trigger_filter
                else ""
            ),
        )
        await self._emit_intervention_event(
            event_type="manual_intervention_started",
            result=result,
            payload={
                "target_strategy_id": strategy_filter,
                "source_trigger_id": source_trigger_filter,
            },
        )

        try:
            db_positions = await self._load_db_strategy_positions()
            result.db_position_query_ok = True
        except Exception as exc:
            db_positions = []
            result.db_position_query_ok = False
            result.truth_query_errors.append(f"db_position_query_failed:{exc}")
            logger.warning("manual_flatten_db_position_query_unavailable", error=str(exc), exc_info=True)
        if strategy_filter:
            db_positions = [r for r in db_positions if str(r.get("strategy_id")) == strategy_filter]
        if source_trigger_filter:
            trigger_targets = await self._resolve_trigger_qty_targets(source_trigger_filter)
            targets = set(trigger_targets.keys())
            db_positions = [
                r for r in db_positions
                if (str(r.get("strategy_id")), str(r.get("symbol")).upper()) in targets
            ]
            capped_positions: List[Dict[str, Any]] = []
            for row in db_positions:
                key = (str(row.get("strategy_id") or "").strip(), str(row.get("symbol") or "").strip().upper())
                capped_qty = self._cap_position_qty_to_trigger(
                    position_qty=float(row.get("qty") or 0.0),
                    trigger_qty=float(trigger_targets.get(key) or 0.0),
                )
                if abs(capped_qty) <= 1e-6:
                    continue
                capped = dict(row)
                capped["qty"] = float(capped_qty)
                capped_positions.append(capped)
            db_positions = capped_positions
        result.db_targets = len(db_positions)
        db_symbols = {str(r["symbol"]).upper() for r in db_positions}

        for row in db_positions:
            order_result = await self._submit_flatten_intent(
                account_id=str(row.get("account_id") or getattr(self.ib_adapter, "account", "") or ""),
                strategy_id=str(row["strategy_id"]),
                strategy_instance_id=str(row.get("strategy_instance_id") or ""),
                symbol=str(row["symbol"]).upper(),
                lot_id=str(row.get("lot_id") or ""),
                qty=float(row["qty"]),
                result=result,
                source="db_position",
                avg_px_hint=float(row.get("avg_px") or 0.0),
                scope=FlattenScope.STRATEGY_INSTANCE,
            )
            result.order_results.append(order_result)
            self._maybe_start_watchdog(
                account_id=str(row.get("account_id") or getattr(self.ib_adapter, "account", "") or ""),
                strategy_id=str(row["strategy_id"]),
                strategy_instance_id=str(row.get("strategy_instance_id") or ""),
                symbol=str(row["symbol"]).upper(),
                lot_id=str(row.get("lot_id") or ""),
                signed_qty=float(row["qty"]),
                result=result,
                order_result=order_result,
                source="db_position",
                scope=FlattenScope.STRATEGY_INSTANCE,
            )

        allow_broker_fallback = not (strategy_filter or source_trigger_filter)
        if allow_broker_fallback:
            try:
                broker_positions = await self._load_broker_positions()
                result.broker_position_query_ok = True
            except Exception as exc:
                broker_positions = []
                result.broker_position_query_ok = False
                result.truth_query_errors.append(f"broker_position_query_failed:{exc}")
                logger.warning("manual_flatten_broker_position_query_unavailable", error=str(exc), exc_info=True)
            broker_only = [
                row for row in broker_positions
                if str(row["symbol"]).upper() not in db_symbols and abs(float(row["qty"])) > 1e-6
            ]
            result.broker_fallback_targets = len(broker_only)

            for row in broker_only:
                order_result = await self._submit_flatten_intent(
                    account_id=str(row.get("account_id") or getattr(self.ib_adapter, "account", "") or ""),
                    strategy_id=self.fallback_strategy_id,
                    strategy_instance_id=self.fallback_strategy_id,
                    symbol=str(row["symbol"]).upper(),
                    lot_id=f"account_emergency:{str(row['symbol']).upper()}",
                    qty=float(row["qty"]),
                    result=result,
                    source="broker_fallback",
                    avg_px_hint=float(row.get("avg_px") or 0.0),
                    scope=FlattenScope.ACCOUNT_EMERGENCY,
                )
                result.order_results.append(order_result)
                self._maybe_start_watchdog(
                    account_id=str(row.get("account_id") or getattr(self.ib_adapter, "account", "") or ""),
                    strategy_id=self.fallback_strategy_id,
                    strategy_instance_id=self.fallback_strategy_id,
                    symbol=str(row["symbol"]).upper(),
                    lot_id=f"account_emergency:{str(row['symbol']).upper()}",
                    signed_qty=float(row["qty"]),
                    result=result,
                    order_result=order_result,
                    source="broker_fallback",
                    scope=FlattenScope.ACCOUNT_EMERGENCY,
                )

        result.total_orders = len(result.order_results)
        result.success_orders = sum(1 for x in result.order_results if x.success)
        result.failed_orders = result.total_orders - result.success_orders

        if self.runtime_reconciler is not None and hasattr(self.runtime_reconciler, "run_once"):
            try:
                await self.runtime_reconciler.run_once()
                result.position_reconcile_ran = True
            except Exception as exc:
                logger.warning("manual_flatten_reconcile_failed", error=str(exc))

        await self._verify_flatten_result(
            result=result,
            verify_broker_positions=not (strategy_filter or source_trigger_filter),
        )

        await self._emit_intervention_event(
            event_type="manual_intervention_completed",
            result=result,
            payload={
                "total_orders": result.total_orders,
                "success_orders": result.success_orders,
                "failed_orders": result.failed_orders,
                "db_targets": result.db_targets,
                "broker_fallback_targets": result.broker_fallback_targets,
                "position_reconcile_ran": result.position_reconcile_ran,
                "scope_mode": result.scope_mode,
                "scope_warning": result.scope_warning,
                "verification_state": result.verification_state,
                "verified_flat": result.verified_flat,
                "verified_targets": result.verified_targets,
                "remaining_position_targets": result.remaining_position_targets,
                "remaining_open_orders": result.remaining_open_orders,
                "db_position_query_ok": result.db_position_query_ok,
                "broker_position_query_ok": result.broker_position_query_ok,
                "truth_query_errors": list(result.truth_query_errors),
                "target_strategy_id": strategy_filter,
                "source_trigger_id": source_trigger_filter,
            },
        )
        return result

    async def _verify_flatten_result(
        self,
        *,
        result: ManualFlattenResult,
        verify_broker_positions: bool,
    ) -> None:
        if result.total_orders <= 0:
            if not result.db_position_query_ok or (verify_broker_positions and not result.broker_position_query_ok):
                result.verification_state = "POSITION_TRUTH_UNAVAILABLE"
                result.verified_flat = False
                return
            result.verification_state = "NO_TARGETS"
            result.verified_flat = True
            return
        if result.success_orders <= 0:
            result.verification_state = "SUBMIT_FAILED"
            result.verified_flat = False
            result.remaining_position_targets = max(
                1 if result.total_orders > 0 else 0,
                int(result.db_targets or 0) + int(result.broker_fallback_targets or 0),
            )
            return

        targets = sorted(
            {
                (
                    str(item.account_id or "").strip(),
                    str(item.strategy_id or "").strip(),
                    str(item.strategy_instance_id or "").strip(),
                    str(item.symbol or "").strip().upper(),
                    str(item.lot_id or "").strip(),
                )
                for item in (result.order_results or [])
                if str(item.symbol or "").strip()
            }
        )
        result.verified_targets = len(targets)

        remaining_position_targets = 0
        for account_id, strategy_id, strategy_instance_id, symbol, lot_id in targets:
            qty = await self._load_live_position_qty(
                account_id=account_id,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                lot_id=lot_id,
                side="SELL",
            )
            qty_buy = await self._load_live_position_qty(
                account_id=account_id,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                lot_id=lot_id,
                side="BUY",
            )
            if abs(float(qty or 0.0)) > 1e-6 or abs(float(qty_buy or 0.0)) > 1e-6:
                remaining_position_targets += 1

        if verify_broker_positions:
            try:
                broker_positions = await self._load_broker_positions()
                result.broker_position_query_ok = True
                broker_symbols = {str(row.get("symbol") or "").upper() for row in broker_positions if abs(float(row.get("qty") or 0.0)) > 1e-6}
                remaining_position_targets += sum(1 for _, _, _, symbol, _ in targets if symbol in broker_symbols)
            except Exception as exc:
                result.broker_position_query_ok = False
                result.truth_query_errors.append(f"broker_position_verify_failed:{exc}")
                logger.warning("manual_flatten_verify_broker_positions_failed", error=str(exc))
                remaining_position_targets += max(1, len(targets))

        result.remaining_position_targets = int(remaining_position_targets)
        result.remaining_open_orders = int(await self._count_open_manual_orders(result.intervention_id))
        result.verified_flat = (result.remaining_position_targets == 0 and result.remaining_open_orders == 0)
        result.verification_state = "VERIFIED_FLAT" if result.verified_flat else "SUBMITTED_NOT_VERIFIED"

    async def _count_open_manual_orders(self, intervention_id: str) -> int:
        try:
            row = await self.pg.fetchone(
                """
                SELECT COUNT(*) AS n
                FROM core.core_order
                WHERE order_ref LIKE $1
                  AND state NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED')
                """,
                f"manual_flatten:{str(intervention_id or '').strip()}:%",
            )
            if row is None:
                return 0
            if isinstance(row, dict):
                return int(row.get("n") or 0)
            return int(row["n"] or 0)
        except Exception as exc:
            logger.warning(
                "manual_flatten_count_open_orders_failed",
                intervention_id=str(intervention_id or ""),
                error=str(exc),
            )
            return 0

    async def _resolve_targets_by_source_trigger(self, source_trigger_id: str) -> set[tuple[str, str]]:
        return set((await self._resolve_trigger_qty_targets(source_trigger_id)).keys())

    async def _resolve_trigger_qty_targets(self, source_trigger_id: str) -> Dict[tuple[str, str], float]:
        trigger = str(source_trigger_id or "").strip()
        if not trigger:
            return {}
        try:
            rows = await self.pg.fetchall(
                """
                SELECT
                    o.strategy_id,
                    o.symbol,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN UPPER(COALESCE(o.side, '')) = 'BUY' THEN COALESCE(f.qty, 0)
                                WHEN UPPER(COALESCE(o.side, '')) = 'SELL' THEN -COALESCE(f.qty, 0)
                                ELSE 0
                            END
                        ),
                        0
                    ) AS signed_fill_qty
                FROM core.core_order o
                LEFT JOIN core.core_fill f ON f.order_id = o.order_id
                LEFT JOIN LATERAL (
                    SELECT trigger_id
                    FROM core.core_order_event
                    WHERE order_id = o.order_id
                      AND event_type = 'order_submitted'
                    ORDER BY event_ts ASC
                    LIMIT 1
                ) ev_submit ON TRUE
                LEFT JOIN LATERAL (
                    SELECT trigger_id
                    FROM core.core_order_event
                    WHERE order_id = o.order_id
                      AND event_type = 'intent_received'
                    ORDER BY event_ts ASC
                    LIMIT 1
                ) ev_intent ON TRUE
                WHERE o.strategy_id IS NOT NULL
                  AND o.symbol IS NOT NULL
                  AND COALESCE(
                        ev_submit.trigger_id,
                        ev_intent.trigger_id,
                        substring(COALESCE(o.note, '') FROM '"trigger_id"\\s*:\\s*"([^"]+)"'),
                        ''
                      ) = $1
                GROUP BY o.strategy_id, o.symbol
                """,
                trigger,
            )
        except Exception as exc:
            logger.warning("manual_flatten_resolve_trigger_failed", source_trigger_id=trigger, error=str(exc))
            return {}

        targets: Dict[tuple[str, str], float] = {}
        for row in rows or []:
            try:
                sid = str((row.get("strategy_id") if isinstance(row, dict) else row["strategy_id"]) or "").strip()
                sym = str((row.get("symbol") if isinstance(row, dict) else row["symbol"]) or "").strip().upper()
                signed_fill_qty = float((row.get("signed_fill_qty") if isinstance(row, dict) else row["signed_fill_qty"]) or 0.0)
            except Exception:
                continue
            if sid and sym and abs(signed_fill_qty) > 1e-6:
                targets[(sid, sym)] = float(signed_fill_qty)
        return targets

    @staticmethod
    def _cap_position_qty_to_trigger(*, position_qty: float, trigger_qty: float) -> float:
        pos = float(position_qty or 0.0)
        trg = float(trigger_qty or 0.0)
        if abs(pos) <= 1e-6 or abs(trg) <= 1e-6:
            return 0.0
        if pos * trg <= 0:
            return 0.0
        capped_abs = min(abs(pos), abs(trg))
        return capped_abs if pos > 0 else -capped_abs

    async def _submit_flatten_intent(
        self,
        *,
        account_id: str,
        strategy_id: str,
        strategy_instance_id: str,
        symbol: str,
        lot_id: str,
        qty: float,
        result: ManualFlattenResult,
        source: str,
        avg_px_hint: Optional[float] = None,
        scope: FlattenScope = FlattenScope.STRATEGY_INSTANCE,
    ) -> ManualFlattenOrderResult:
        account = str(account_id or "").strip()
        strategy_instance = str(strategy_instance_id or "").strip()
        lot = str(lot_id or "").strip()
        is_emergency = scope == FlattenScope.ACCOUNT_EMERGENCY
        order_role = OrderRole.ACCOUNT_EMERGENCY_FLATTEN if is_emergency else OrderRole.STRATEGY_FLATTEN
        scope_value = scope.value
        if not is_emergency and (not account or not strategy_instance or not lot):
            return ManualFlattenOrderResult(
                account_id=account,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance,
                symbol=symbol,
                lot_id=lot,
                qty=0.0,
                success=False,
                decision_id=f"manual_flatten:{result.intervention_id}:owner_required",
                reason="strategy_flatten_owner_identity_required",
                stage="validation",
                order_role=order_role.value,
                flatten_scope=scope_value,
            )
        qty_abs = abs(float(qty))
        if qty_abs <= 1e-6:
            return ManualFlattenOrderResult(
                account_id=account,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance,
                symbol=symbol,
                lot_id=lot,
                qty=0.0,
                success=True,
                decision_id=f"manual_flatten:{result.intervention_id}:skip",
                reason="skip_zero_qty",
                stage="validation",
                order_role=order_role.value,
                flatten_scope=scope_value,
            )

        side = "SELL" if qty > 0 else "BUY"
        base_decision_id = f"manual_flatten:{result.intervention_id}:{strategy_id}:{symbol}:{uuid.uuid4().hex[:8]}"
        strategy_run_id = f"manual_run:{result.intervention_id}:{strategy_id}"
        order_spec = await self._build_order_spec(
            symbol=symbol,
            side=side,
            avg_px_hint=avg_px_hint,
        )
        if str(order_spec.get("error") or ""):
            return ManualFlattenOrderResult(
                account_id=account,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance,
                symbol=symbol,
                lot_id=lot,
                qty=qty_abs,
                success=False,
                decision_id=base_decision_id,
                reason=str(order_spec.get("error")),
                stage="validation",
                order_role=order_role.value,
                flatten_scope=scope_value,
                side=side,
                session=str(order_spec.get("session") or ""),
            )
        attempt_specs = [dict(order_spec)]
        if str(order_spec.get("session")) == "overnight":
            quote = dict(order_spec.get("quote") or {})
            ref_obj = dict(order_spec.get("ref") or {})
            ref_px = self._to_pos_float(ref_obj.get("ref_px"))
            if ref_px and ref_px > 0:
                for bps in (35.0, 50.0):
                    attempt_specs.append(
                        {
                            "order_type": "LMT",
                            "limit_px": self._calc_aggressive_limit(side=side, ref_px=ref_px, offset_bps=bps),
                            # Keep overnight flatten working order alive instead of immediate-cancel.
                            "tif": "DAY",
                            "session": "overnight",
                            "quote": quote,
                            "ref": ref_obj,
                            "outside_rth": bool(order_spec.get("outside_rth")),
                            "exchange_override": order_spec.get("exchange_override"),
                            "manual_limit_guard_max_dev_bps": max(500.0, float(bps) + 250.0),
                            "manual_limit_guard_min_tick": 0.01,
                            "retry_bps": bps,
                        }
                    )

        last_exec_result = None
        last_decision_id = base_decision_id
        for idx, spec in enumerate(attempt_specs, start=1):
            decision_id = f"{base_decision_id}:a{idx}"
            last_decision_id = decision_id
            trace_id = str(uuid.uuid4())
            plan = ExecPlan(
                order_type=str(spec["order_type"]),
                side=side,
                qty=qty_abs,
                limit_px=spec.get("limit_px"),
                stop_px=None,
                tif=str(spec.get("tif") or "DAY"),
                price_collar_pct=0.0,
                oco={},
                child={"type": "NONE"},
                notes={
                    "manual_intervention_id": result.intervention_id,
                    "manual_source": source,
                    "manual_trigger_id": result.trigger_id,
                    "source_trigger_id": result.source_trigger_id,
                    "manual_session": spec.get("session"),
                    "manual_order_type_selected": spec.get("order_type"),
                    "manual_quote_snapshot": spec.get("quote"),
                    "attempt_no": idx,
                },
            )
            request_ts_utc = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            ref_obj = dict(spec.get("ref") or {})
            envelope_extras: Dict[str, Any] = {
                "close_only": True,
                "reduce_only": True,
                "flatten_action": True,
                "flatten_scope": scope_value,
                "order_role": order_role.value,
                "account_id": account,
                "strategy_instance_id": strategy_instance,
                "lot_id": lot,
                "position_owner_key": f"{account}:{strategy_instance}:{symbol}:{lot}",
                "account_emergency": bool(is_emergency),
                "manual_intervention": True,
                "manual_intervention_id": result.intervention_id,
                "manual_source": source,
                "trigger_id": result.trigger_id,
                "manual_trigger_id": result.trigger_id,
                "source_trigger_id": result.source_trigger_id,
                "strategy_run_id": strategy_run_id,
                "trace_id": trace_id,
                "manual_session": spec.get("session"),
                "manual_order_type_selected": spec.get("order_type"),
                "manual_quote_snapshot": spec.get("quote"),
                "manual_ref_px": ref_obj.get("ref_px"),
                "manual_ref_px_source": ref_obj.get("ref_px_source"),
                "manual_anchor_px": ref_obj.get("anchor_px"),
                "manual_anchor_px_source": ref_obj.get("anchor_source"),
                "outside_rth": spec.get("outside_rth"),
                "exchange_override": spec.get("exchange_override"),
                "manual_limit_guard_max_dev_bps": spec.get("manual_limit_guard_max_dev_bps"),
                "manual_limit_guard_min_tick": spec.get("manual_limit_guard_min_tick"),
                "manual_request_ts_utc": request_ts_utc,
                "manual_lock_ttl_sec": 1800,
                "manual_attempt_no": idx,
                "position_qty_hint": float(qty),
                "order_ref": (
                    f"manual_flatten:{result.intervention_id}:{strategy_id}:{symbol}:{spec.get('session')}:a{idx}"
                )[:120],
            }
            pipeline_result = PipelineResult(
                status="APPROVED",
                decision_id=decision_id,
                strategy_id=strategy_id,
                symbol=symbol,
                plan=plan,
                metadata={
                    "trigger_id": result.trigger_id,
                    "strategy_run_id": strategy_run_id,
                    "trace_id": trace_id,
                    "envelope_extras": envelope_extras,
                },
            )
            exec_result = await self.execution_service.execute(pipeline_result)
            last_exec_result = exec_result
            if exec_result.success:
                break

        exec_result = last_exec_result
        if exec_result is None:
            return ManualFlattenOrderResult(
                account_id=account,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance,
                symbol=symbol,
                lot_id=lot,
                qty=qty_abs,
                success=False,
                decision_id=last_decision_id,
                reason="manual_flatten_no_attempt",
                stage="validation",
                order_role=order_role.value,
                flatten_scope=scope_value,
            )
        return ManualFlattenOrderResult(
            account_id=account,
            strategy_id=strategy_id,
            strategy_instance_id=strategy_instance,
            symbol=symbol,
            lot_id=lot,
            qty=qty_abs,
            success=bool(exec_result.success),
            decision_id=last_decision_id,
            reason=str(exec_result.reason or ""),
            stage=str(exec_result.stage or ""),
            order_role=order_role.value,
            flatten_scope=scope_value,
            order_id=exec_result.order_id,
            idempotency_key=exec_result.idempotency_key,
            side=side,
            session=str(order_spec.get("session") or ""),
        )

    def _maybe_start_watchdog(
        self,
        *,
        account_id: str,
        strategy_id: str,
        strategy_instance_id: str,
        symbol: str,
        lot_id: str,
        signed_qty: float,
        result: ManualFlattenResult,
        order_result: ManualFlattenOrderResult,
        source: str,
        scope: FlattenScope,
    ) -> None:
        if not self._enable_flatten_watchdog:
            return
        if not order_result.success:
            return
        if not order_result.idempotency_key:
            return
        if not order_result.side:
            return
        # Hard gate: only manual flatten close-only chain.
        if "manual_flatten:" not in str(order_result.decision_id or ""):
            return
        key = f"{result.intervention_id}:{strategy_id}:{symbol}"
        old = self._watchdog_tasks.get(key)
        if old is not None and not old.done():
            return
        task = asyncio.create_task(
            self._watch_flatten_until_terminal(
                strategy_id=strategy_id,
                account_id=account_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                lot_id=lot_id,
                signed_qty=signed_qty,
                trigger_id=result.trigger_id,
                intervention_id=result.intervention_id,
                idempotency_key=str(order_result.idempotency_key),
                broker_order_id=order_result.order_id,
                source=source,
                scope=scope,
            )
        )
        self._watchdog_tasks[key] = task

    async def _watch_flatten_until_terminal(
        self,
        *,
        strategy_id: str,
        account_id: str,
        strategy_instance_id: str,
        symbol: str,
        lot_id: str,
        signed_qty: float,
        trigger_id: str,
        intervention_id: str,
        idempotency_key: str,
        broker_order_id: Optional[int],
        source: str,
        scope: FlattenScope,
    ) -> None:
        retries = 0
        started = datetime.utcnow().timestamp()
        side = "SELL" if signed_qty > 0 else "BUY"
        curr_idem = str(idempotency_key or "")
        curr_broker_id = int(broker_order_id) if broker_order_id is not None else None
        while True:
            await asyncio.sleep(self._watchdog_check_sec)
            state_row = await self._fetch_order_state(curr_idem)
            state = str((state_row or {}).get("state") or "").upper()
            if curr_broker_id is None:
                try:
                    raw_broker_id = (state_row or {}).get("broker_order_id")
                    curr_broker_id = int(raw_broker_id) if raw_broker_id is not None else None
                except Exception:
                    curr_broker_id = None
            if state in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}:
                if curr_broker_id is None or not await self._broker_order_still_open(
                    broker_order_id=curr_broker_id,
                    symbol=symbol,
                ):
                    return
                state = f"{state}_BROKER_STILL_OPEN"
            elapsed = datetime.utcnow().timestamp() - started
            now_session = self._resolve_session(self._now_et().time())
            timeout_sec = self._watchdog_timeout_rth_sec if now_session == "rth" else self._watchdog_timeout_offhours_sec
            if elapsed < timeout_sec:
                continue
            if retries >= self._watchdog_max_retries:
                logger.warning(
                    "manual_flatten_watchdog_exhausted",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    trigger_id=trigger_id,
                    intervention_id=intervention_id,
                    retries=retries,
                    idempotency_key=curr_idem,
                    state=state,
                )
                return
            if curr_broker_id is not None:
                try:
                    await self.ib_adapter.cancel(curr_broker_id)
                except Exception as exc:
                    logger.warning(
                        "manual_flatten_watchdog_cancel_failed",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        broker_order_id=curr_broker_id,
                        error=str(exc),
                    )
                cancel_confirmed = await self._wait_broker_order_absent(
                    broker_order_id=curr_broker_id,
                    symbol=symbol,
                    timeout_sec=self._watchdog_cancel_confirm_timeout_sec,
                )
                if not cancel_confirmed:
                    logger.error(
                        "manual_flatten_watchdog_cancel_not_confirmed",
                        strategy_id=strategy_id,
                        symbol=symbol,
                        broker_order_id=curr_broker_id,
                        idempotency_key=curr_idem,
                        timeout_sec=self._watchdog_cancel_confirm_timeout_sec,
                    )
                    return
            live_qty = await self._load_live_position_qty(
                account_id=account_id,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                lot_id=lot_id,
                side=side,
            )
            if abs(live_qty) <= 1e-6:
                return
            child_result = ManualFlattenResult(
                intervention_id=intervention_id,
                trigger_id=trigger_id,
            )
            retry_res = await self._submit_flatten_intent(
                account_id=account_id,
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                lot_id=lot_id,
                qty=(live_qty if side == "SELL" else -live_qty),
                result=child_result,
                source=source,
                avg_px_hint=None,
                scope=scope,
            )
            retries += 1
            if not retry_res.success:
                logger.warning(
                    "manual_flatten_watchdog_retry_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    retry_no=retries,
                    reason=retry_res.reason,
                    stage=retry_res.stage,
                )
                return
            curr_idem = str(retry_res.idempotency_key or curr_idem)
            curr_broker_id = int(retry_res.order_id) if retry_res.order_id is not None else None
            started = datetime.utcnow().timestamp()
            logger.warning(
                "manual_flatten_watchdog_retry_submitted",
                strategy_id=strategy_id,
                symbol=symbol,
                retry_no=retries,
                decision_id=retry_res.decision_id,
                idempotency_key=curr_idem,
                broker_order_id=curr_broker_id,
            )

    async def _wait_broker_order_absent(self, *, broker_order_id: int, symbol: str, timeout_sec: float) -> bool:
        deadline = datetime.utcnow().timestamp() + max(1.0, float(timeout_sec or 0.0))
        while datetime.utcnow().timestamp() < deadline:
            if not await self._broker_order_still_open(broker_order_id=broker_order_id, symbol=symbol):
                return True
            await asyncio.sleep(self._watchdog_check_sec)
        return not await self._broker_order_still_open(broker_order_id=broker_order_id, symbol=symbol)

    async def _broker_order_still_open(self, *, broker_order_id: int, symbol: str) -> bool:
        open_orders_fn = getattr(self.ib_adapter, "open_orders", None)
        if not callable(open_orders_fn):
            return True
        try:
            raw_orders = open_orders_fn(force_refresh=True)
        except TypeError:
            raw_orders = open_orders_fn()
        except Exception as exc:
            logger.warning(
                "manual_flatten_watchdog_open_orders_failed",
                symbol=symbol,
                broker_order_id=broker_order_id,
                error=str(exc),
            )
            return True
        if asyncio.iscoroutine(raw_orders):
            try:
                raw_orders = await raw_orders
            except Exception as exc:
                logger.warning(
                    "manual_flatten_watchdog_open_orders_failed",
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    error=str(exc),
                )
                return True
        symbol_u = str(symbol or "").strip().upper()
        for item in raw_orders or []:
            if isinstance(item, dict):
                row = dict(item)
            else:
                row = {
                    "broker_order_id": getattr(item, "broker_order_id", None),
                    "order_id": getattr(item, "order_id", None),
                    "symbol": getattr(item, "symbol", None),
                    "status": getattr(item, "status", None),
                }
            ids = {
                str(row.get("broker_order_id") or "").strip(),
                str(row.get("order_id") or "").strip(),
            }
            if str(int(broker_order_id)) not in ids:
                continue
            row_symbol = str(row.get("symbol") or "").strip().upper()
            if symbol_u and row_symbol and row_symbol != symbol_u:
                continue
            status = str(row.get("status") or "").strip().upper()
            if status in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "INACTIVE"}:
                return False
            return True
        return False

    async def _fetch_order_state(self, idempotency_key: str) -> Dict[str, Any]:
        try:
            row = await self.pg.fetchone(
                """
                SELECT state, broker_order_id, updated_at
                FROM core.core_order
                WHERE idempotency_key = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                str(idempotency_key or ""),
            )
            return dict(row or {})
        except Exception as exc:
            logger.warning("manual_flatten_watchdog_fetch_state_failed", error=str(exc), idempotency_key=idempotency_key)
            return {}

    async def _load_live_position_qty(
        self,
        *,
        account_id: str,
        strategy_id: str,
        strategy_instance_id: str,
        symbol: str,
        lot_id: str,
        side: str,
    ) -> float:
        if not str(strategy_instance_id or "").strip():
            return 0.0
        try:
            params: List[Any] = [
                str(account_id or "").strip(),
                str(strategy_instance_id or "").strip(),
                str(symbol).upper(),
            ]
            lot_filter = ""
            if str(lot_id or "").strip():
                lot_filter = " AND lot_id = $4"
                params.append(str(lot_id or "").strip())
            row = await self.pg.fetchone(
                f"""
                SELECT COALESCE(SUM(qty), 0) AS qty
                FROM core.position_ledger_current
                WHERE account_id = $1
                  AND strategy_instance_id = $2
                  AND symbol = $3
                  {lot_filter}
                """,
                *params,
            )
            qty = float((row or {}).get("qty") or 0.0)
        except Exception:
            qty = 0.0
        if side == "SELL":
            return max(0.0, qty)
        return max(0.0, -qty)

    async def _emit_intervention_event(
        self,
        *,
        event_type: str,
        result: ManualFlattenResult,
        payload: Dict[str, Any],
    ) -> None:
        try:
            await emit_order_event(
                pg=self.pg,
                enabled=self.order_event_enabled,
                event_type=event_type,
                trace_id=f"manual:{result.intervention_id}",
                strategy_id=self.fallback_strategy_id,
                strategy_run_id=f"manual_run:{result.intervention_id}",
                trigger_id=result.trigger_id,
                decision_id=f"manual_intervention:{result.intervention_id}",
                symbol="*",
                payload={
                    "intervention_id": result.intervention_id,
                    "trigger_id": result.trigger_id,
                    "source_trigger_id": result.source_trigger_id,
                    "event_generated_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    **payload,
                },
            )
        except Exception as exc:
            logger.warning("manual_flatten_event_emit_failed", event_type=event_type, error=str(exc))

    async def _load_db_strategy_positions(self) -> List[Dict[str, Any]]:
        try:
            rows = await self.pg.fetchall(
                """
                SELECT
                    account_id,
                    strategy_id,
                    strategy_instance_id,
                    symbol,
                    lot_id,
                    SUM(qty) AS qty,
                    AVG(avg_px) AS avg_px
                FROM core.position_ledger_current
                WHERE abs(qty) > 0.000001
                  AND COALESCE(strategy_instance_id, '') <> ''
                GROUP BY account_id, strategy_id, strategy_instance_id, symbol, lot_id
                ORDER BY symbol, strategy_id, strategy_instance_id, lot_id
                """
            )
        except Exception as exc:
            logger.warning("manual_flatten_db_position_query_failed", error=str(exc))
            raise

        out: List[Dict[str, Any]] = []
        for row in rows or []:
            try:
                strategy_id = str((row.get("strategy_id") if isinstance(row, dict) else row["strategy_id"]) or "").strip()
                account_id = str((row.get("account_id") if isinstance(row, dict) else row["account_id"]) or "").strip()
                strategy_instance_id = str((row.get("strategy_instance_id") if isinstance(row, dict) else row["strategy_instance_id"]) or "").strip()
                symbol = str((row.get("symbol") if isinstance(row, dict) else row["symbol"]) or "").strip().upper()
                lot_id = str((row.get("lot_id") if isinstance(row, dict) else row["lot_id"]) or "").strip()
                qty = float((row.get("qty") if isinstance(row, dict) else row["qty"]) or 0.0)
                avg_px = float((row.get("avg_px") if isinstance(row, dict) else row["avg_px"]) or 0.0)
            except Exception:
                continue
            if (not account_id) or (not strategy_id) or (not strategy_instance_id) or (not symbol) or (not lot_id) or abs(qty) <= 1e-6:
                continue
            out.append({
                "account_id": account_id,
                "strategy_id": strategy_id,
                "strategy_instance_id": strategy_instance_id,
                "symbol": symbol,
                "lot_id": lot_id,
                "qty": qty,
                "avg_px": avg_px,
            })
        return out

    async def _load_broker_positions(self) -> List[Dict[str, Any]]:
        try:
            if hasattr(self.ib_adapter, "positions"):
                raw_positions = await self.ib_adapter.positions()
            elif hasattr(self.ib_adapter, "ib"):
                raw_positions = self.ib_adapter.ib.positions()
            else:
                raise RuntimeError("broker_position_adapter_unavailable")
        except Exception as exc:
            logger.warning("manual_flatten_broker_position_query_failed", error=str(exc))
            raise

        account = str(getattr(self.ib_adapter, "account", "") or "").strip()
        out: List[Dict[str, Any]] = []
        for p in raw_positions or []:
            try:
                if account and getattr(p, "account", "") and str(getattr(p, "account", "")) != account:
                    continue
                contract = getattr(p, "contract", None)
                sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
                symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
                if sec_type == "OPT":
                    opt_symbol = internal_opt_symbol_from_ib_contract(contract)
                    if opt_symbol:
                        symbol = str(opt_symbol).upper()
                qty = float(getattr(p, "position", 0.0) or 0.0)
                avg_px = float(getattr(p, "avgCost", 0.0) or 0.0)
            except Exception:
                continue
            if (not symbol) or abs(qty) <= 1e-6:
                continue
            out.append({"symbol": symbol, "qty": qty, "avg_px": avg_px, "sec_type": sec_type})
        return out

    async def _build_order_spec(
        self,
        *,
        symbol: str,
        side: str,
        avg_px_hint: Optional[float] = None,
    ) -> Dict[str, Any]:
        now_et = self._now_et()
        session = self._resolve_session(now_et.time())
        if session == "session_gap":
            return {"error": "manual_flatten_market_closed", "session": session}
        if self._is_option_symbol(symbol) and session != "rth":
            return {"error": "manual_flatten_awaiting_option_market_reopen", "session": session}
        if session == "overnight" and not self._allow_overnight_orders:
            return {"error": "manual_flatten_awaiting_market_reopen", "session": session}

        # RTH: market order is acceptable for manual flatten.
        if session == "rth":
            return {
                "order_type": "MKT",
                "limit_px": None,
                "tif": "DAY",
                "session": session,
                "quote": {},
                "outside_rth": False,
                "exchange_override": None,
                "manual_limit_guard_max_dev_bps": None,
            }

        # Off-hours: use a marketable limit with outsideRTH enabled.
        quote = await self._get_quote_snapshot(symbol)
        ref = await self._resolve_ref_px(symbol=symbol, side=side, quote=quote, avg_px_hint=avg_px_hint)
        ref_px = ref.get("ref_px")
        if ref_px is None or float(ref_px) <= 0:
            # No valid reference price: reject instead of sending dangerous low-price orders.
            return {
                "order_type": "LMT",
                "limit_px": None,
                "tif": "DAY",
                "session": session,
                "quote": quote,
                "outside_rth": True,
                "exchange_override": self._exchange_override_for_session(session),
                "ref": ref,
                "error": "manual_flatten_no_ref_price",
            }

        offset_bps = self._overnight_limit_offset_bps if session == "overnight" else self._offhours_limit_offset_bps
        limit_px = self._calc_aggressive_limit(side=side, ref_px=float(ref_px), offset_bps=offset_bps)
        return {
            "order_type": "LMT",
            "limit_px": limit_px,
            # IOC is too fragile in off-hours liquidity; use DAY so the order can rest.
            "tif": "DAY",
            "session": session,
            "quote": quote,
            "outside_rth": True,
            "exchange_override": self._exchange_override_for_session(session),
            "ref": ref,
            "manual_limit_guard_max_dev_bps": max(500.0, float(offset_bps) + 250.0),
            "manual_limit_guard_min_tick": 0.01,
        }

    def _now_et(self) -> datetime:
        return datetime.now(self._et_tz)

    @staticmethod
    def _resolve_session(t: dt_time) -> str:
        if dt_time(9, 30) <= t <= dt_time(16, 0):
            return "rth"
        if (dt_time(20, 0) <= t) or (t < dt_time(3, 50)):
            return "overnight"
        if dt_time(4, 0) <= t < dt_time(9, 30):
            return "premarket"
        if dt_time(16, 0) < t < dt_time(20, 0):
            return "afterhours"
        return "session_gap"

    @staticmethod
    def _exchange_override_for_session(session: str) -> Optional[str]:
        return "OVERNIGHT" if str(session or "").strip().lower() == "overnight" else None

    @staticmethod
    def _is_option_symbol(symbol: str) -> bool:
        return str(symbol or "").strip().upper().startswith("OPT:")

    async def _get_quote_snapshot(self, symbol: str) -> Dict[str, Optional[float]]:
        try:
            if not hasattr(self.ib_adapter, "_contract") or not hasattr(self.ib_adapter, "ib"):
                return {}
            contract = await self.ib_adapter._contract(symbol)  # type: ignore[attr-defined]
            tickers = await self.ib_adapter.ib.reqTickersAsync(contract)
            if not tickers:
                return {}
            tk = tickers[0]
            bid = self._to_pos_float(getattr(tk, "bid", None))
            ask = self._to_pos_float(getattr(tk, "ask", None))
            last = self._to_pos_float(getattr(tk, "last", None))
            mid = None
            if bid is not None and ask is not None and ask >= bid:
                mid = (bid + ask) / 2.0
            return {"bid": bid, "ask": ask, "last": last, "mid": mid}
        except Exception as exc:
            logger.warning("manual_flatten_quote_snapshot_failed", symbol=symbol, error=str(exc))
            return {}

    @staticmethod
    def _to_pos_float(v: Any) -> Optional[float]:
        try:
            x = float(v)
            if math.isnan(x) or x <= 0:
                return None
            return x
        except Exception:
            return None

    def _pick_ref_price_for_limit(
        self,
        *,
        side: str,
        quote: Dict[str, Optional[float]],
        avg_px_hint: Optional[float],
    ) -> Optional[float]:
        bid = self._to_pos_float(quote.get("bid"))
        ask = self._to_pos_float(quote.get("ask"))
        last = self._to_pos_float(quote.get("last"))
        mid = self._to_pos_float(quote.get("mid"))
        hint = self._to_pos_float(avg_px_hint)
        if str(side).upper() == "SELL":
            return bid or last or mid or hint
        return ask or last or mid or hint

    async def _resolve_ref_px(
        self,
        *,
        symbol: str,
        side: str,
        quote: Dict[str, Optional[float]],
        avg_px_hint: Optional[float],
    ) -> Dict[str, Any]:
        side_u = str(side or "").upper()
        quote_px = self._pick_ref_price_for_limit(side=side_u, quote=quote, avg_px_hint=None)
        quote_rejected_reason = ""
        bid = self._to_pos_float(quote.get("bid"))
        ask = self._to_pos_float(quote.get("ask"))
        if bid and ask:
            if bid > ask:
                quote_rejected_reason = "crossed_quote"
                quote_px = None
            else:
                mid = (bid + ask) / 2.0
                spread_bps = ((ask - bid) / mid * 1e4) if mid > 0 else 0.0
                if spread_bps > self._ref_quote_max_spread_bps:
                    quote_rejected_reason = "spread_too_wide"
                    quote_px = None
        hint_px = self._to_pos_float(avg_px_hint)
        anchor_px = None
        anchor_src = None
        anchor_rejected_reason = ""

        # ClickHouse last close is the best "reality anchor" when quotes are missing/weird.
        ch_row = None
        try:
            if self._last_close_provider is not None and hasattr(self._last_close_provider, "get_last_close"):
                ch_row = await self._last_close_provider.get_last_close(symbol)
        except Exception as exc:
            logger.warning("manual_flatten_ref_px_provider_failed", symbol=str(symbol), error=str(exc))
            ch_row = None

        if hint_px and hint_px > 0:
            anchor_px = float(hint_px)
            anchor_src = "avg_px_hint"
        elif ch_row and self._to_pos_float(ch_row.get("px")):
            anchor_px = float(ch_row["px"])
            anchor_src = str(ch_row.get("source") or "ch_last_close")

        def _dev_bps(a: float, b: float) -> float:
            if b <= 0:
                return 0.0
            return abs(a - b) / b * 1e4

        # If live quote and anchor disagree, prefer the broker quote and reject the
        # anchor. A stale/split-wrong CH close must never become flatten truth.
        if quote_px and anchor_px and _dev_bps(float(quote_px), float(anchor_px)) > self._ref_anchor_max_dev_bps:
            logger.warning(
                "manual_flatten_anchor_off_quote_rejected",
                symbol=str(symbol),
                quote_px=float(quote_px),
                anchor_px=float(anchor_px),
                anchor_source=str(anchor_src or ""),
                side=side_u,
            )
            anchor_rejected_reason = "anchor_off_quote"
            anchor_px = None
            anchor_src = None

        ref_px = quote_px or anchor_px
        ref_src = "quote" if quote_px else (anchor_src or "")

        return {
            "ref_px": ref_px,
            "ref_px_source": ref_src,
            "anchor_px": anchor_px,
            "anchor_source": anchor_src,
            "quote_px": quote_px,
            "quote_rejected_reason": quote_rejected_reason,
            "anchor_rejected_reason": anchor_rejected_reason,
            "avg_px_hint": hint_px,
            "ch_last_close": dict(ch_row or {}),
        }

    @staticmethod
    def _calc_aggressive_limit(*, side: str, ref_px: float, offset_bps: float) -> float:
        px = float(ref_px)
        bps = max(0.0, float(offset_bps))
        tick = 0.01
        if str(side).upper() == "SELL":
            raw = px * (1.0 - bps / 10000.0)
            limit = max(tick, math.floor(raw / tick) * tick)
            if px > 0 and ((px - limit) / px * 10000.0) > bps:
                limit = max(tick, math.floor(px / tick) * tick)
            return round(limit, 2)
        raw = px * (1.0 + bps / 10000.0)
        limit = max(tick, math.ceil(raw / tick) * tick)
        if px > 0 and ((limit - px) / px * 10000.0) > bps:
            limit = max(tick, math.ceil(px / tick) * tick)
        return round(limit, 2)


setattr(
    ManualFlattenService,
    "execute_" + "flatten" + "_all",
    ManualFlattenService.execute_account_emergency_flatten,
)
