from __future__ import annotations

import json
from typing import Any

from strategies.stocks_kernel.execution.order_identity import parse_order_ref, parse_protection_order_ref
from strategies.stocks_kernel.types import ExitPlan, PositionLot


class RecoveryModule:
    """
    Startup/recovery module.

    Decouples startup-block handling from on_features main path.
    """

    def __init__(self, *, strategy) -> None:
        self._strategy = strategy

    async def ensure_runtime_ready_for_tick(self, *, symbol: str) -> bool:
        strategy = self._strategy
        if not strategy._startup_block_reason:
            await self.ensure_runtime_protection(symbol=symbol)
            return True
        if await strategy._maybe_recover_startup_block():
            strategy._startup_recovery_attempted_reason = None
            await self.ensure_runtime_protection(symbol=symbol)
            return True
        strategy._startup_recovery_attempted_reason = strategy._startup_block_reason
        strategy._audit(
            stage="startup_blocked_tick_ignored",
            symbol=str(symbol).strip().upper(),
            reason=strategy._startup_block_reason,
            payload=dict(strategy._startup_block_payload),
        )
        emit_tradability = getattr(strategy, "_emit_strategy_tradability_heartbeat", None)
        if callable(emit_tradability):
            try:
                emit_tradability(
                    symbol=str(symbol or "").strip().upper() or "SYSTEM",
                    session=str(getattr(strategy, "_current_strategy_session", lambda: "")() or ""),
                    blockers=[f"startup_blocked:{strategy._startup_block_reason}"],
                    reason="startup_blocked",
                )
            except Exception:
                pass
        return False

    async def ensure_runtime_protection(self, *, symbol: str) -> None:
        strategy = self._strategy
        if getattr(strategy, "execution", None) is None:
            return
        signal_symbol = str(symbol or "").strip().upper()
        instrument = strategy._instrument_for_signal_symbol(signal_symbol)
        if instrument is None:
            instrument = strategy._instrument_for_exec_symbol(signal_symbol)
        if instrument is None:
            return
        snapshot = strategy._resolve_runtime_snapshot(symbol=signal_symbol)
        if snapshot is None:
            return
        for lot in strategy._active_binding_lots(instrument=instrument):
            actions = self.build_lot_recovery_actions(lot=lot)
            for action in actions:
                action_type = str(action.get("action") or "").strip()
                reason = str(action.get("reason") or action_type or "restart_recovery").strip()
                if action_type == "rebuild_protection":
                    rebuilt = await strategy._attempt_rebuild_missing_protection_for_lot(
                        lot=lot,
                        snapshot=snapshot,
                        reason=reason,
                    )
                    if rebuilt:
                        strategy._audit(
                            stage="restart_protection_rebuilt",
                            symbol=str(getattr(lot, "symbol", "") or ""),
                            reason=reason,
                            payload={"position_lot_id": getattr(lot, "position_lot_id", None)},
                        )
                    continue
                if action_type == "flatten_position":
                    strategy._activate_symbol_kill_switch(
                        symbol=str(getattr(lot, "symbol", "") or ""),
                        reason=reason,
                    )
                    if bool(getattr(lot, "flatten_order_refs", None)):
                        continue
                    await strategy._submit_exit_intent(
                        exit_plan=ExitPlan(
                            symbol=str(getattr(lot, "symbol", "") or ""),
                            order_role="FLATTEN",
                            qty=float(getattr(lot, "live_qty", 0.0) or 0.0),
                            position_lot_id=getattr(lot, "position_lot_id", None),
                            lane_id=getattr(lot, "lane_id", None),
                            ownership_scope=str(getattr(lot, "ownership_scope", "strategy") or "strategy"),
                            payload={"reason": reason, "recovery": True, "restart_recovery": True},
                        ),
                        snapshot=snapshot,
                        lot=lot,
                    )
                    strategy._audit(
                        stage="restart_recovery_flatten_submitted",
                        symbol=str(getattr(lot, "symbol", "") or ""),
                        reason=reason,
                        payload={"position_lot_id": getattr(lot, "position_lot_id", None)},
                    )

    def build_lot_recovery_actions(self, *, lot: Any) -> tuple[dict[str, Any], ...]:
        if float(getattr(lot, "live_qty", 0.0) or 0.0) <= 0.0:
            return ()
        state = dict(getattr(lot, "extras", {}) or {}).get("protection_state") or {}
        protection_lifecycle = str(state.get("protection_lifecycle") or "").strip().lower()
        protective_oco = dict(getattr(lot, "extras", {}) or {}).get("protective_oco") or {}
        if bool(getattr(lot, "protection_order_refs", None)):
            return ()
        if protection_lifecycle == "flatten_only":
            return ({"action": "flatten_position", "reason": "unsafe_position_without_protection"},)
        if not protective_oco:
            return ()
        if protection_lifecycle in {"pending_attach", "repair_pending", "degraded", "protected"}:
            return ({"action": "rebuild_protection", "reason": "rebuild_stop_after_restart"},)
        return ()

    async def maybe_recover_startup_block(self) -> bool:
        strategy = self._strategy
        reason = str(strategy._startup_block_reason or "").strip()
        if not reason:
            return True
        recoverable_reasons = {
            "broker_adapter_unavailable",
            "broker_live_position_present",
            "broker_open_orders_present",
            "db_residual_present",
            "db_position_present",
            "db_open_orders_present",
            "broker_account_not_clean",
        }
        if reason not in recoverable_reasons:
            return False
        adapter = self.resolve_broker_adapter()
        if adapter is None:
            return False
        preflight = await self.run_startup_preflight()
        if not bool(preflight.get("ok")):
            strategy._startup_block_reason = str(preflight.get("reason") or reason)
            strategy._startup_block_payload = dict(preflight)
            return False
        previous_reason = strategy._startup_block_reason
        strategy._startup_block_reason = None
        strategy._startup_block_payload = {}
        strategy._clear_startup_blocked_gauge()
        strategy._audit(
            stage="startup_recovered",
            symbol="SYSTEM",
            reason="ready",
            payload={"previous_reason": previous_reason},
        )
        return True

    async def run_startup_preflight(self) -> dict[str, Any]:
        strategy = self._strategy
        config = getattr(strategy, "config", None)
        exec_symbols: set[str] = set()
        for instrument in (config.instruments if config is not None else []):
            for candidate in tuple(getattr(instrument, "exec_symbols", lambda: ())() or ()):
                symbol = str(candidate or "").strip().upper()
                if symbol:
                    exec_symbols.add(symbol)
            for candidate in (
                str(getattr(instrument, "long_exec_symbol", "") or "").strip().upper(),
                str(getattr(instrument, "short_exec_symbol", "") or "").strip().upper(),
            ):
                if candidate:
                    exec_symbols.add(candidate)
        ordered_exec_symbols = sorted(exec_symbols)
        broker_checks: list[dict[str, Any]] = []
        adopted_symbols: set[str] = set()
        for exec_symbol in ordered_exec_symbols:
            isolation = await self.broker_account_isolation(symbol=exec_symbol)
            broker_checks.append({"symbol": exec_symbol, **dict(isolation)})
            if not isolation.get("blocked") and str(isolation.get("reason") or "") == "foreign_owner_position_ignored":
                adopted_symbols.add(exec_symbol)
            if isolation.get("blocked"):
                if str(isolation.get("reason") or "") == "broker_live_position_present":
                    adoption = await self.adopt_same_strategy_broker_position(
                        symbol=exec_symbol,
                        broker_qty=float(isolation.get("broker_qty") or 0.0),
                        isolation=dict(isolation),
                    )
                    broker_checks[-1]["adoption"] = dict(adoption)
                    if bool(adoption.get("ok")):
                        adopted_symbols.add(exec_symbol)
                        continue
                return {
                    "ok": False,
                    "reason": str(isolation.get("reason") or "broker_account_not_clean"),
                    "broker_checks": broker_checks,
                    "db_check": {"ok": True, "reason": "skipped", "positions": [], "orders": []},
                    "runtime_clean": True,
                }
        db_check = await self.db_startup_isolation(
            exec_symbols=ordered_exec_symbols,
            allowed_owned_positions=adopted_symbols,
        )
        if not bool(db_check.get("ok")):
            return {
                "ok": False,
                "reason": str(db_check.get("reason") or "db_residual_present"),
                "broker_checks": broker_checks,
                "db_check": db_check,
                "runtime_clean": True,
            }
        runtime_clean = not bool(strategy.kernel_state)
        if not runtime_clean:
            return {
                "ok": False,
                "reason": "runtime_state_not_clean",
                "broker_checks": broker_checks,
                "db_check": db_check,
                "runtime_clean": False,
            }
        return {
            "ok": True,
            "reason": "ready",
            "broker_checks": broker_checks,
            "db_check": db_check,
            "runtime_clean": True,
            "adopted_symbols": sorted(adopted_symbols),
        }

    async def db_startup_isolation(
        self,
        *,
        exec_symbols: list[str],
        allowed_owned_positions: set[str] | None = None,
    ) -> dict[str, Any]:
        strategy = self._strategy
        pg = self.resolve_pg()
        if pg is None or not exec_symbols:
            return {"ok": True, "reason": "pg_unavailable", "positions": [], "orders": []}
        allowed_owned = {str(symbol or "").strip().upper() for symbol in (allowed_owned_positions or set())}
        positions = await pg.fetchall(
            """
            SELECT strategy_id, symbol, pos_qty
            FROM core.core_position
            WHERE symbol = ANY($1::text[])
              AND ABS(COALESCE(pos_qty, 0)) > 0
            ORDER BY strategy_id, symbol
            """,
            exec_symbols,
        )
        orders = await pg.fetchall(
            """
            SELECT strategy_id, symbol, side, qty, state, broker_order_id
            FROM core.core_order
            WHERE (symbol = ANY($1::text[]) OR strategy_id = $2)
              AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
            ORDER BY strategy_id, symbol
            """,
            exec_symbols,
            strategy.strategy_id,
        )
        blocking_positions = []
        for row in positions or []:
            payload = dict(row) if not isinstance(row, dict) else dict(row)
            row_strategy = str(payload.get("strategy_id") or "").strip()
            row_symbol = str(payload.get("symbol") or "").strip().upper()
            if row_symbol in allowed_owned:
                continue
            blocking_positions.append(payload)
        blocking_orders = []
        for row in orders or []:
            payload = dict(row) if not isinstance(row, dict) else dict(row)
            row_strategy = str(payload.get("strategy_id") or "").strip()
            row_symbol = str(payload.get("symbol") or "").strip().upper()
            if row_symbol in allowed_owned:
                continue
            blocking_orders.append(payload)
        ok = not blocking_positions and not blocking_orders
        return {
            "ok": ok,
            "reason": "ready" if ok else "db_residual_present",
            "positions": blocking_positions,
            "adopted_positions": [
                dict(row) if not isinstance(row, dict) else dict(row)
                for row in (positions or [])
                if str((dict(row) if not isinstance(row, dict) else row).get("strategy_id") or "").strip()
                == str(strategy.strategy_id or "").strip()
                and str((dict(row) if not isinstance(row, dict) else row).get("symbol") or "").strip().upper() in allowed_owned
            ],
            "orders": blocking_orders,
            "adopted_orders": [
                dict(row) if not isinstance(row, dict) else dict(row)
                for row in (orders or [])
                if str((dict(row) if not isinstance(row, dict) else row).get("strategy_id") or "").strip()
                == str(strategy.strategy_id or "").strip()
                and str((dict(row) if not isinstance(row, dict) else row).get("symbol") or "").strip().upper() in allowed_owned
            ],
        }

    async def adopt_same_strategy_broker_position(
        self,
        *,
        symbol: str,
        broker_qty: float,
        isolation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        strategy = self._strategy
        symbol_u = str(symbol or "").strip().upper()
        broker_qty_f = float(broker_qty or 0.0)
        if not symbol_u or abs(broker_qty_f) <= 1e-12:
            return {"ok": False, "reason": "no_broker_position"}
        if getattr(strategy, "execution", None) is None:
            return {"ok": False, "reason": "execution_kernel_unavailable"}
        open_order_refs = [str(ref or "").strip() for ref in (isolation or {}).get("open_order_refs") or [] if str(ref or "").strip()]
        foreign_order_refs = [
            str(ref or "").strip() for ref in (isolation or {}).get("foreign_order_refs") or [] if str(ref or "").strip()
        ]
        unknown_order_refs = [
            str(ref or "").strip() for ref in (isolation or {}).get("unknown_order_refs") or [] if str(ref or "").strip()
        ]
        if unknown_order_refs:
            return {
                "ok": False,
                "reason": "open_orders_present",
                "open_order_refs": open_order_refs,
                "foreign_order_refs": foreign_order_refs,
                "unknown_order_refs": unknown_order_refs,
            }
        existing = strategy.execution.ledger.list_lots(
            strategy_id=strategy.strategy_id,
            symbol=symbol_u,
            include_closed=False,
        )
        if existing:
            return {"ok": True, "reason": "runtime_lot_already_present", "position_lot_id": existing[0].position_lot_id}
        pg = self.resolve_pg()
        if pg is None:
            return {"ok": False, "reason": "pg_unavailable"}
        try:
            row = await pg.fetchrow(
                """
                WITH ledger AS (
                    SELECT
                        strategy_id,
                        symbol,
                        lot_id,
                        SUM(qty)::float8 AS ledger_qty,
                        COUNT(*) AS lot_count,
                        array_remove(array_agg(DISTINCT account_id), NULL) AS account_ids
                    FROM core.position_ledger_current
                    WHERE strategy_id = $1
                      AND symbol = $2
                      AND ABS(COALESCE(qty, 0)) > 0
                    GROUP BY strategy_id, symbol, lot_id
                ),
                entry_order AS (
                    SELECT order_id::text AS order_id,
                           broker_order_id,
                           ib_perm_id,
                           order_ref,
                           side,
                           qty::float8 AS order_qty,
                           limit_px::float8 AS limit_px,
                           note
                    FROM core.core_order
                    WHERE strategy_id = $1
                      AND symbol = $2
                      AND COALESCE(state, '') = 'FILLED'
                      AND UPPER(COALESCE(side, '')) IN ('BUY', 'SELL')
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                    LIMIT 1
                )
                SELECT cp.strategy_id,
                       cp.symbol,
                       cp.pos_qty::float8 AS pos_qty,
                       cp.avg_px::float8 AS avg_px,
                       l.lot_id,
                       l.ledger_qty,
                       l.lot_count,
                       l.account_ids,
                       eo.order_id,
                       eo.broker_order_id,
                       eo.ib_perm_id,
                       eo.order_ref,
                       eo.side AS order_side,
                       eo.order_qty,
                       eo.limit_px,
                       eo.note
                FROM core.core_position cp
                LEFT JOIN ledger l
                  ON l.strategy_id = cp.strategy_id
                 AND l.symbol = cp.symbol
                LEFT JOIN entry_order eo ON true
                WHERE cp.strategy_id = $1
                  AND cp.symbol = $2
                  AND ABS(COALESCE(cp.pos_qty, 0)) > 0
                LIMIT 1
                """,
                strategy.strategy_id,
                symbol_u,
            )
        except Exception as exc:
            strategy._audit(
                stage="startup_position_adoption_failed",
                symbol=symbol_u,
                reason="db_query_failed",
                payload={"error": str(exc)},
            )
            return {"ok": False, "reason": "db_query_failed", "error": str(exc)}
        if row is None:
            return {"ok": False, "reason": "db_position_missing"}
        payload = dict(row)
        db_qty = float(payload.get("pos_qty") or 0.0)
        ledger_qty = payload.get("ledger_qty")
        ledger_qty_f = float(ledger_qty) if ledger_qty is not None else None
        if abs(db_qty - broker_qty_f) > 1e-6:
            return {"ok": False, "reason": "db_broker_qty_mismatch", "db_qty": db_qty, "broker_qty": broker_qty_f}
        if ledger_qty_f is None or abs(ledger_qty_f - broker_qty_f) > 1e-6:
            return {
                "ok": False,
                "reason": "ledger_broker_qty_mismatch",
                "ledger_qty": ledger_qty_f,
                "broker_qty": broker_qty_f,
            }
        lot_id = str(payload.get("lot_id") or "").strip()
        if not lot_id:
            return {"ok": False, "reason": "ledger_lot_missing"}
        note = self._safe_json_obj(payload.get("note"))
        extras = dict(note.get("extras") or {}) if isinstance(note.get("extras"), dict) else {}
        entry_order_ref = str(payload.get("order_ref") or extras.get("order_ref") or "").strip()
        if not entry_order_ref:
            entry_order_ref = f"adopted:{strategy.strategy_id}:{symbol_u}:{lot_id}"
        side = "LONG" if broker_qty_f > 0 else "SHORT"
        signal_symbol = str(extras.get("signal_symbol") or "").strip().upper()
        if not signal_symbol:
            instrument = strategy._instrument_for_exec_symbol(symbol_u)
            signal_symbol = str(getattr(instrument, "signal_symbol", "") or "").strip().upper()
        lane_id = str(extras.get("lane_id") or "").strip() or None
        config_extras = dict(getattr(getattr(strategy, "config", None), "raw_extras", {}) or {})
        strategy_instance_id = str(
            extras.get("strategy_instance_id")
            or note.get("strategy_instance_id")
            or payload.get("strategy_instance_id")
            or config_extras.get("strategy_instance_id")
            or strategy.strategy_id
            or ""
        ).strip()
        account_id = str(
            extras.get("account_id")
            or note.get("account_id")
            or payload.get("account_id")
            or config_extras.get("account_id")
            or config_extras.get("account")
            or ""
        ).strip()
        position_owner_key = str(
            extras.get("position_owner_key")
            or (
                f"{strategy_instance_id}:{symbol_u}:{lot_id}"
                if strategy_instance_id and symbol_u and lot_id
                else ""
            )
        ).strip()
        authority = strategy._execution_lane_authorities(
            signal_symbol=signal_symbol or symbol_u,
            exec_symbol=symbol_u,
            lane_id=lane_id,
        )
        protective_oco = dict(extras.get("protective_oco") or {})
        if protective_oco and getattr(strategy, "protection_module", None) is not None:
            protective_oco = strategy.protection_module.with_exec_market_ref(
                payload=protective_oco,
                exec_symbol=symbol_u,
                snapshot=strategy._resolve_exec_snapshot(exec_symbol=symbol_u),
            )
        avg_px = payload.get("avg_px")
        avg_entry_price = None
        try:
            avg_entry_price = float(avg_px) if avg_px is not None else None
        except Exception:
            avg_entry_price = None
        lot = PositionLot(
            position_lot_id=lot_id,
            strategy_id=strategy.strategy_id,
            symbol=symbol_u,
            side=side,
            opened_by_decision_id=str(note.get("decision_id") or extras.get("decision_id") or "startup_adoption"),
            opened_by_trigger_id=str(extras.get("trigger_id") or "").strip() or None,
            entry_order_ref=entry_order_ref,
            entry_order_id=str(payload.get("broker_order_id") or "").strip() or None,
            entry_perm_id=int(payload["ib_perm_id"]) if payload.get("ib_perm_id") is not None else None,
            live_qty=abs(broker_qty_f),
            filled_qty=abs(broker_qty_f),
            avg_entry_price=avg_entry_price,
            state="live",
            lane_id=lane_id,
            ownership_scope=str(extras.get("ownership_scope") or "strategy"),
            flatten_authority=str(authority.get("flatten_authority") or "strategy"),
            protection_authority=str(authority.get("protection_authority") or "strategy"),
            adopt_authority=str(authority.get("adopt_authority") or "strategy"),
            extras={
                **extras,
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "position_lot_id": lot_id,
                "lot_id": lot_id,
                "position_owner_key": position_owner_key,
                "signal_symbol": signal_symbol,
                "protective_oco": protective_oco,
                "protection_state": self._adopted_protection_state(
                    strategy=strategy,
                    signal_symbol=signal_symbol or symbol_u,
                    exec_symbol=symbol_u,
                    protective_oco=protective_oco,
                    existing_state=extras.get("protection_state") if isinstance(extras.get("protection_state"), dict) else {},
                ),
                "startup_adopted": True,
                "startup_adoption_reason": "same_strategy_broker_position",
            },
        )
        try:
            strategy.execution.ledger.open_lot(lot)
        except ValueError:
            return {"ok": True, "reason": "runtime_lot_already_present", "position_lot_id": lot_id}
        strategy._audit(
            stage="startup_broker_position_adopted",
            symbol=symbol_u,
            reason="same_strategy_position_verified",
            payload={
                "position_lot_id": lot_id,
                "broker_qty": broker_qty_f,
                "db_qty": db_qty,
                "ledger_qty": ledger_qty_f,
                "entry_order_ref": entry_order_ref,
                "broker_order_id": payload.get("broker_order_id"),
                "ib_perm_id": payload.get("ib_perm_id"),
                "signal_symbol": signal_symbol,
            },
        )
        return {
            "ok": True,
            "reason": "same_strategy_position_adopted",
            "position_lot_id": lot_id,
            "symbol": symbol_u,
            "broker_qty": broker_qty_f,
        }

    def _adopted_protection_state(
        self,
        *,
        strategy,
        signal_symbol: str,
        exec_symbol: str,
        protective_oco: dict[str, Any],
        existing_state: dict[str, Any],
    ) -> dict[str, Any]:
        if not protective_oco:
            return {"protection_lifecycle": "flatten_only", "startup_adopted": True}
        if getattr(strategy, "protection_module", None) is None:
            return {"protection_lifecycle": "repair_pending", "startup_adopted": True}
        base = strategy.protection_module.build_initial_position_state(
            signal_symbol=signal_symbol,
            exec_symbol=exec_symbol,
            stop_plan=None,
            take_profit_plan=None,
            retreat_plan=None,
            protective_oco=protective_oco,
        )
        return {
            **dict(base or {}),
            **dict(existing_state or {}),
            "protection_lifecycle": "repair_pending",
            "startup_adopted": True,
        }

    @staticmethod
    def _safe_json_obj(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    async def broker_account_isolation(self, *, symbol: str) -> dict[str, Any]:
        strategy = self._strategy
        adapter = self.resolve_broker_adapter()
        if adapter is None:
            config = getattr(strategy, "config", None)
            fail_closed = bool(config is not None and (not config.dry_run) and config.startup_preflight_enabled)
            return {
                "blocked": fail_closed,
                "reason": "broker_adapter_unavailable",
                "broker_qty": 0.0,
                "open_order_count": 0,
                "open_order_refs": [],
            }
        symbol_u = str(symbol or "").strip().upper()
        broker_qty = 0.0
        open_order_refs: list[str] = []
        foreign_order_refs: list[str] = []
        unknown_order_refs: list[str] = []
        try:
            if hasattr(adapter, "position_qty"):
                broker_qty = float(await adapter.position_qty(symbol_u) or 0.0)
        except Exception as exc:
            strategy._audit(
                stage="broker_account_isolation_probe_failed",
                symbol=symbol_u,
                reason="position_probe_failed",
                payload={"error": str(exc)},
            )
        owner_qty = await self._ledger_position_qty(symbol=symbol_u, strategy_id=str(strategy.strategy_id or "").strip())
        symbol_ledger_qty = await self._ledger_position_qty(symbol=symbol_u, strategy_id="")
        try:
            if hasattr(adapter, "open_orders"):
                open_orders = await adapter.open_orders(force_refresh=True)
                for order in open_orders or []:
                    order_status = str(getattr(order, "status", "") or "").strip()
                    if order_status == "PendingCancel":
                        continue
                    order_symbol = str(getattr(order, "symbol", "") or "").strip().upper()
                    if order_symbol != symbol_u:
                        continue
                    order_ref = str(getattr(order, "order_ref", "") or "").strip()
                    if not order_ref:
                        unknown_order_refs.append(order_ref)
                        open_order_refs.append(order_ref)
                        continue
                    parsed = parse_order_ref(order_ref)
                    if parsed is None:
                        protection_ref = parse_protection_order_ref(order_ref)
                        if protection_ref is not None:
                            protection_symbol = str(protection_ref.symbol or "").strip().upper()
                            protection_strategy_id = str(protection_ref.strategy_id or "").strip()
                            if protection_symbol != order_symbol:
                                unknown_order_refs.append(order_ref)
                                open_order_refs.append(order_ref)
                            elif protection_strategy_id != str(strategy.strategy_id or "").strip():
                                foreign_order_refs.append(order_ref)
                            else:
                                open_order_refs.append(order_ref)
                        else:
                            owner_strategy_id = await self._durable_order_ref_owner_strategy(
                                order_ref=order_ref,
                                symbol=symbol_u,
                            )
                            if not owner_strategy_id:
                                unknown_order_refs.append(order_ref)
                                open_order_refs.append(order_ref)
                            elif owner_strategy_id != str(strategy.strategy_id or "").strip():
                                foreign_order_refs.append(order_ref)
                            else:
                                open_order_refs.append(order_ref)
                        continue
                    if str(parsed.strategy_id or "").strip() != str(strategy.strategy_id or "").strip():
                        foreign_order_refs.append(order_ref)
                        continue
                    open_order_refs.append(order_ref)
        except Exception as exc:
            strategy._audit(
                stage="broker_account_isolation_probe_failed",
                symbol=symbol_u,
                reason="open_orders_probe_failed",
                payload={"error": str(exc)},
            )
        if foreign_order_refs:
            strategy._audit(
                stage="broker_account_isolation_foreign_orders_ignored",
                symbol=symbol_u,
                reason="foreign_strategy_orders_ignored",
                payload={"foreign_order_refs": foreign_order_refs},
            )
        if abs(broker_qty) > 1e-12:
            if owner_qty is not None and abs(float(owner_qty or 0.0)) > 1e-12:
                return {
                    "blocked": True,
                    "reason": "broker_live_position_present",
                    "broker_qty": float(owner_qty),
                    "broker_account_qty": broker_qty,
                    "owner_lane_verified": True,
                    "open_order_count": len(open_order_refs),
                    "open_order_refs": open_order_refs,
                    "foreign_order_refs": foreign_order_refs,
                    "unknown_order_refs": unknown_order_refs,
                }
            if (
                symbol_ledger_qty is not None
                and abs(float(symbol_ledger_qty or 0.0)) > 1e-12
                and not open_order_refs
                and not unknown_order_refs
            ):
                return {
                    "blocked": False,
                    "reason": "foreign_owner_position_ignored",
                    "broker_qty": 0.0,
                    "broker_account_qty": broker_qty,
                    "open_order_count": 0,
                    "open_order_refs": [],
                    "foreign_order_refs": foreign_order_refs,
                    "unknown_order_refs": unknown_order_refs,
                }
            return {
                "blocked": True,
                "reason": "broker_live_position_present",
                "broker_qty": broker_qty,
                "open_order_count": len(open_order_refs),
                "open_order_refs": open_order_refs,
                "foreign_order_refs": foreign_order_refs,
                "unknown_order_refs": unknown_order_refs,
            }
        if open_order_refs:
            return {
                "blocked": True,
                "reason": "broker_open_orders_present",
                "broker_qty": broker_qty,
                "open_order_count": len(open_order_refs),
                "open_order_refs": open_order_refs,
                "foreign_order_refs": foreign_order_refs,
                "unknown_order_refs": unknown_order_refs,
            }
        return {
            "blocked": False,
            "reason": "ready",
            "broker_qty": broker_qty,
            "open_order_count": 0,
            "open_order_refs": [],
            "foreign_order_refs": foreign_order_refs,
            "unknown_order_refs": unknown_order_refs,
        }

    async def _durable_order_ref_owner_strategy(self, *, order_ref: str, symbol: str) -> str | None:
        order_ref_v = str(order_ref or "").strip()
        symbol_u = str(symbol or "").strip().upper()
        if not order_ref_v or not symbol_u:
            return None
        pg = self.resolve_pg()
        if pg is None:
            return None
        try:
            row = await pg.fetchrow(
                """
                SELECT strategy_id
                FROM core.core_order
                WHERE order_ref = $1
                  AND symbol = $2
                  AND COALESCE(state, '') NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED')
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
                """,
                order_ref_v,
                symbol_u,
            )
        except Exception:
            return None
        if not row:
            return None
        try:
            return str((dict(row) if not isinstance(row, dict) else row).get("strategy_id") or "").strip() or None
        except Exception:
            return None

    async def _ledger_position_qty(self, *, symbol: str, strategy_id: str) -> float | None:
        pg = self.resolve_pg()
        symbol_u = str(symbol or "").strip().upper()
        strategy_u = str(strategy_id or "").strip()
        if pg is None or not symbol_u:
            return None
        try:
            if strategy_u:
                row = await pg.fetchrow(
                    """
                    SELECT COALESCE(SUM(qty), 0)::float8 AS qty
                    FROM core.position_ledger_current
                    WHERE symbol = $1
                      AND (strategy_instance_id = $2 OR strategy_id = $2)
                    """,
                    symbol_u,
                    strategy_u,
                )
            else:
                row = await pg.fetchrow(
                    """
                    SELECT COALESCE(SUM(qty), 0)::float8 AS qty
                    FROM core.position_ledger_current
                    WHERE symbol = $1
                    """,
                    symbol_u,
                )
        except Exception:
            return None
        if not row:
            return 0.0
        try:
            return float((dict(row) if not isinstance(row, dict) else row).get("qty") or 0.0)
        except Exception:
            return None

    async def binding_broker_account_isolation(
        self,
        *,
        signal_symbol: str | None = None,
        exec_symbol: str | None = None,
    ) -> dict[str, Any]:
        strategy = self._strategy
        instrument = None
        signal_symbol_u = str(signal_symbol or "").strip().upper()
        exec_symbol_u = str(exec_symbol or "").strip().upper()
        if signal_symbol_u:
            instrument = strategy._instrument_for_signal_symbol(signal_symbol_u)
        if instrument is None and exec_symbol_u:
            instrument = strategy._instrument_for_exec_symbol(exec_symbol_u)

        if instrument is None:
            return await self.broker_account_isolation(symbol=exec_symbol_u or signal_symbol_u)

        blocked_symbols: list[dict[str, Any]] = []
        total_abs_broker_qty = 0.0
        total_open_orders = 0
        open_order_refs: list[str] = []
        foreign_order_refs: list[str] = []
        unknown_order_refs: list[str] = []
        for candidate in strategy._runtime_exec_symbols(symbol=instrument.signal_symbol, instrument=instrument):
            isolation = await self.broker_account_isolation(symbol=candidate)
            total_abs_broker_qty += abs(float(isolation.get("broker_qty") or 0.0))
            total_open_orders += int(isolation.get("open_order_count") or 0)
            open_order_refs.extend(list(isolation.get("open_order_refs") or []))
            foreign_order_refs.extend(list(isolation.get("foreign_order_refs") or []))
            unknown_order_refs.extend(list(isolation.get("unknown_order_refs") or []))
            if isolation.get("blocked"):
                blocked_symbols.append(
                    {
                        "symbol": candidate,
                        "reason": str(isolation.get("reason") or "blocked"),
                        "broker_qty": float(isolation.get("broker_qty") or 0.0),
                        "open_order_count": int(isolation.get("open_order_count") or 0),
                        "open_order_refs": list(isolation.get("open_order_refs") or []),
                    }
                )

        if blocked_symbols:
            primary_reason = str(blocked_symbols[0].get("reason") or "blocked")
            reason = {
                "broker_live_position_present": "broker_binding_live_position_present",
                "broker_open_orders_present": "broker_binding_open_orders_present",
            }.get(primary_reason, primary_reason)
            return {
                "blocked": True,
                "reason": reason,
                "broker_qty": total_abs_broker_qty,
                "open_order_count": total_open_orders,
                "open_order_refs": sorted({str(ref) for ref in open_order_refs if str(ref).strip()}),
                "foreign_order_refs": sorted({str(ref) for ref in foreign_order_refs if str(ref).strip()}),
                "unknown_order_refs": sorted({str(ref) for ref in unknown_order_refs if str(ref).strip()}),
                "blocked_symbols": blocked_symbols,
            }
        return {
            "blocked": False,
            "reason": "ready",
            "broker_qty": total_abs_broker_qty,
            "open_order_count": total_open_orders,
            "open_order_refs": sorted({str(ref) for ref in open_order_refs if str(ref).strip()}),
            "foreign_order_refs": sorted({str(ref) for ref in foreign_order_refs if str(ref).strip()}),
            "unknown_order_refs": sorted({str(ref) for ref in unknown_order_refs if str(ref).strip()}),
            "blocked_symbols": [],
        }

    def resolve_broker_adapter(self):
        strategy = self._strategy
        try:
            engine = getattr(strategy.ctx, "engine", None)
            adapter = getattr(engine, "broker_adapter", None)
            if adapter is not None:
                return adapter
        except Exception:
            pass
        try:
            engine = getattr(strategy.ctx, "engine", None)
            decision_pipeline = getattr(engine, "decision_pipeline", None)
            execution_service = getattr(decision_pipeline, "execution_service", None)
            safety_kernel = getattr(execution_service, "safety_kernel", None)
            adapter = getattr(safety_kernel, "broker", None)
            if adapter is not None:
                return adapter
        except Exception:
            pass
        try:
            engine = getattr(strategy.ctx, "engine", None)
            execution_service = getattr(engine, "execution_service", None)
            safety_kernel = getattr(execution_service, "safety_kernel", None)
            adapter = getattr(safety_kernel, "broker", None)
            if adapter is not None:
                return adapter
        except Exception:
            pass
        try:
            engine = getattr(strategy.ctx, "engine", None)
            market_data_service = getattr(engine, "market_data_service", None)
            adapter = getattr(market_data_service, "ib_adapter", None)
            if adapter is not None:
                return adapter
        except Exception:
            pass
        return None

    def resolve_pg(self):
        strategy = self._strategy
        direct = getattr(strategy.ctx, "pg", None)
        if direct is not None:
            return direct
        engine = getattr(strategy.ctx, "engine", None)
        if engine is not None:
            engine_pg = getattr(engine, "pg", None)
            if engine_pg is not None:
                return engine_pg
            nested_engine = getattr(engine, "_strategy_engine", None)
            if nested_engine is not None:
                nested_pg = getattr(nested_engine, "pg", None)
                if nested_pg is not None:
                    return nested_pg
        return None
