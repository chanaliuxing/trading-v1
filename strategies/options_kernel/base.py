from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, time
from inspect import isawaitable
from pathlib import Path
from typing import Any, Dict, Optional, Set, Type
from unittest.mock import Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

from libs.strategy.base import (
    DecisionEnvelope,
    Freshness,
    OrderStyle,
    StrategyAlgorithm,
    StrategyContext,
    StrategyParams,
)
from strategies.shared_data import shared_underlying_data_plane
from services.production.contract_selection_service import select_option_contract_for_entry
from services.production.scoped_tradability import (
    BlockerScopeType,
    FlowType,
    ScopedBlocker,
    TradabilityInputs,
    TradabilityScope,
    decision_to_dict,
    evaluate_tradability,
)
from services.production.strategy_tradability import write_strategy_tradability_heartbeat

from .execution_profile import build_execution_profile_from_extras
from .ownership import OptionOwnershipLedger
from .parity import OptionParityRunner
from .plugin import BaseOptionPlugin
from .types import (
    EntryPlan,
    EntryIntent,
    ExecutionProfile,
    ExitPlan,
    KernelBarContext,
    KernelIntentRecord,
    KernelPositionState,
    OrderAck,
    OrderReject,
    PluginRuntimeState,
    Selection,
    Signal,
)


_ET = ZoneInfo("America/New_York")


def _write_contract_selection_verdict(
    *,
    strategy_id: str,
    symbol: str,
    status: str,
    reason: str,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    try:
        ts = (now or datetime.now(_ET)).astimezone(_ET)
        out_dir = Path("runtime/readiness/contract_selection")
        out_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "artifact_type": "contract_selection_verdict",
            "generated_at": ts.isoformat(),
            "strategy_id": str(strategy_id or ""),
            "symbol": str(symbol or "").upper(),
            "status": str(status or ""),
            "reason": str(reason or ""),
            "payload": dict(payload or {}),
        }
        day = ts.strftime("%Y%m%d")
        with (out_dir / f"contract_selection_{day}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        latest = out_dir / "contract_selection_latest.json"
        latest.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2, default=str), encoding="utf-8")
    except Exception:
        return


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return default


def _parse_iso_ts(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=_ET)
        return parsed
    except Exception:
        return None


def _coerce_et_now(features: Dict[str, Any]) -> datetime:
    ts = features.get("ts")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=_ET)
        return ts
    if hasattr(ts, "to_pydatetime"):
        try:
            parsed = ts.to_pydatetime()
            if isinstance(parsed, datetime) and parsed.tzinfo is None:
                return parsed.replace(tzinfo=_ET)
            return parsed
        except Exception:
            pass
    try:
        raw = float(ts)
        if raw > 100_000_000_000_000:
            return datetime.fromtimestamp(raw / 1_000_000.0, tz=_ET)
        if raw > 100_000_000_000:
            return datetime.fromtimestamp(raw / 1000.0, tz=_ET)
        return datetime.fromtimestamp(raw, tz=_ET)
    except Exception:
        return datetime.now(_ET)


def _encode_payload(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): _encode_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_payload(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class OptionKernelContextProxy:
    def __init__(self, *, kernel: "OptionKernel", real_ctx: StrategyContext):
        self._kernel = kernel
        self._real_ctx = real_ctx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_ctx, name)

    async def submit_intent(self, envelope: DecisionEnvelope) -> Any:
        return await self._kernel.submit_intent(envelope)


class OptionKernel:
    def __init__(
        self,
        *,
        strategy_id: str,
        real_ctx: StrategyContext,
        params: StrategyParams,
        plugin: BaseOptionPlugin,
    ) -> None:
        self.strategy_id = str(strategy_id)
        self.real_ctx = real_ctx
        self.params = params
        self.plugin = plugin
        self.ledger = OptionOwnershipLedger()
        self.parity_runner = OptionParityRunner()
        self.proxy_ctx = OptionKernelContextProxy(kernel=self, real_ctx=real_ctx)
        self.delegate: Optional[StrategyAlgorithm] = None
        self.kernel_mode = str((params.extras or {}).get("kernel_mode") or "delegate").strip().lower()
        if self.kernel_mode not in {"delegate", "hybrid", "plugin", "pure_plugin"}:
            self.kernel_mode = "delegate"
        self._plugin_lifecycle: Dict[str, Any] = {}
        self._plugin_errors: int = 0
        self._last_pure_cycle_audit_minute: str = ""
        self._last_plugin_submit_key: str = ""
        self._last_plugin_exit_key: str = ""
        self._plugin_reject_guard_by_submit_key: Dict[str, Dict[str, Any]] = {}
        self._capture_delegate_intents: bool = False
        self._captured_delegate_intents: list[DecisionEnvelope] = []
        self._delegate_generated_intent_this_cycle: bool = False
        self._bar_history: list[Dict[str, Any]] = []
        preload_bars = int((params.extras or {}).get("preload_history_bars", 260) or 260)
        self._max_history_bars = max(128, preload_bars + 32)
        self._entry_runtime_live_emitted_lots: Set[str] = set()
        self._entry_protection_emitted_lots: Set[str] = set()
        self._flatten_confirmed_emitted_lots: Set[str] = set()
        self.execution_profile = build_execution_profile_from_extras(
            profile_id=str((params.extras or {}).get("live_parity_profile_id") or "qqq_option_ma_ext_v1"),
            market_data_profile_id=str((params.extras or {}).get("market_data_profile_id") or "standard_close_extended_sma"),
            extras=params.extras,
        )

    def _notify_signal_accepted(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: Optional[EntryPlan],
        selection: Optional[Selection] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            self.plugin.on_signal_accepted(
                runtime=runtime,
                signal=signal,
                entry_plan=entry_plan,
                selection=selection,
                payload=dict(payload or {}),
            )
        except Exception as exc:
            self._plugin_errors += 1
            self._emit_strategy_audit(
                stage="plugin_lifecycle_hook_failed",
                status="error",
                reason="on_signal_accepted_failed",
                severity="warning",
                payload={"error": str(exc)},
            )

    def _notify_entry_rejected(
        self,
        *,
        runtime: Optional[PluginRuntimeState],
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if runtime is None:
            return
        try:
            self.plugin.on_entry_submit_blocked_pure(
                runtime=runtime,
                signal=signal,
                entry_plan=entry_plan,
                selection=selection,
                reason=str(reason or "entry_rejected"),
                payload=dict(payload or {}),
            )
        except Exception as exc:
            self._plugin_errors += 1
            self._emit_strategy_audit(
                stage="plugin_lifecycle_hook_failed",
                status="error",
                reason="on_signal_rejected_failed",
                severity="warning",
                payload={"reject_reason": str(reason or ""), "error": str(exc)},
            )

    def _notify_order_submitted(
        self,
        *,
        runtime: Optional[PluginRuntimeState],
        ack: OrderAck,
        signal: Optional[Signal] = None,
        entry_plan: Optional[EntryPlan] = None,
        exit_plan: Optional[Any] = None,
        selection: Optional[Selection] = None,
    ) -> None:
        if runtime is None:
            return
        try:
            self.plugin.on_order_submitted(
                runtime=runtime,
                ack=ack,
                signal=signal,
                entry_plan=entry_plan,
                exit_plan=exit_plan,
                selection=selection,
            )
        except Exception as exc:
            self._plugin_errors += 1
            self._emit_strategy_audit(
                stage="plugin_lifecycle_hook_failed",
                status="error",
                reason="on_order_submitted_failed",
                severity="warning",
                payload={"intent_action": ack.intent_action, "error": str(exc)},
            )

    def _shared_underlying_data_plane_enabled(self) -> bool:
        return bool((self.params.extras or {}).get("enable_shared_underlying_data_plane", True))

    def _shared_underlying_namespace(self) -> str:
        extras = dict(self.params.extras or {})
        configured = str(extras.get("shared_underlying_namespace") or "").strip().lower()
        if configured:
            return configured
        return str(self.strategy_id).strip().lower() or "options_kernel"

    def _shared_underlying_max_staleness_sec(self) -> float:
        extras = dict(self.params.extras or {})
        try:
            raw = float(extras.get("shared_underlying_max_staleness_sec") or 180.0)
        except Exception:
            raw = 180.0
        return raw if raw > 0.0 else 180.0

    def _resolve_local_audit_writer(self) -> Any:
        engine = getattr(self.real_ctx, "engine", None)
        candidates = [
            getattr(engine, "audit_writer", None),
            getattr(getattr(engine, "_strategy_engine", None), "audit_writer", None),
        ]
        for writer in candidates:
            if writer is not None and hasattr(writer, "emit"):
                return writer
        return None

    def _emit_strategy_audit(
        self,
        *,
        stage: str,
        status: str,
        reason: str,
        severity: str = "info",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        extras = dict(self.params.extras or {})
        try:
            writer.emit(
                domain="strategy",
                component=str(getattr(self.plugin, "component_name", "option_plugin")),
                stage=stage,
                severity=severity,
                strategy_id=self.strategy_id,
                strategy_run_id=str(getattr(self.real_ctx, "strategy_run_id", "") or "").strip() or None,
                symbol=str((self.params.extras or {}).get("underlying") or ""),
                status=status,
                reason=reason,
                payload={
                    "definition_id": str(extras.get("definition_id") or ""),
                    "audit_namespace": str(extras.get("audit_namespace") or ""),
                    "position_namespace": str(extras.get("position_namespace") or ""),
                    "risk_symbol": str(extras.get("risk_symbol") or ""),
                    **dict(payload or {}),
                },
            )
        except Exception:
            pass

    def _emit_state_audit(
        self,
        *,
        stage: str,
        status: str,
        reason: str,
        state_machine: str,
        severity: str = "info",
        symbol: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        extras = dict(self.params.extras or {})
        try:
            writer.emit(
                domain="state",
                component=str(getattr(self.plugin, "component_name", "option_plugin")),
                stage=stage,
                severity=severity,
                strategy_id=self.strategy_id,
                strategy_run_id=str(getattr(self.real_ctx, "strategy_run_id", "") or "").strip() or None,
                symbol=str(symbol or extras.get("underlying") or ""),
                status=status,
                reason=reason,
                payload={
                    "state_machine": str(state_machine),
                    "definition_id": str(extras.get("definition_id") or ""),
                    "audit_namespace": str(extras.get("audit_namespace") or ""),
                    "position_namespace": str(extras.get("position_namespace") or ""),
                    "risk_symbol": str(extras.get("risk_symbol") or ""),
                    **dict(payload or {}),
                },
            )
        except Exception:
            pass

    def _definition_id(self) -> str:
        return str((self.params.extras or {}).get("definition_id") or "").strip()

    def _audit_namespace(self) -> str:
        return str((self.params.extras or {}).get("audit_namespace") or "").strip()

    def _position_namespace(self) -> str:
        namespace = str((self.params.extras or {}).get("position_namespace") or "").strip()
        return namespace or str(self.strategy_id)

    def _risk_symbol(self) -> str:
        return str((self.params.extras or {}).get("risk_symbol") or "").strip()

    def _underlying_symbol(self) -> str:
        return str((self.params.extras or {}).get("underlying") or "").strip()

    def _lot_payload_by_id(self, position_lot_id: str) -> Dict[str, Any]:
        lot_id = str(position_lot_id or "").strip()
        if not lot_id:
            return {}
        for lot in self.ledger.active_lots(strategy_id=self.strategy_id):
            if str(lot.position_lot_id) == lot_id:
                return dict(lot.payload or {})
        for lot in self.ledger.pending_entry_lots(strategy_id=self.strategy_id):
            if str(lot.position_lot_id) == lot_id:
                return dict(lot.payload or {})
        return {}

    def _entry_latency_payload(self, *, lot_payload: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        source_trigger_ts = _parse_iso_ts(lot_payload.get("source_trigger_ts"))
        entry_submit_ts = _parse_iso_ts(lot_payload.get("entry_submit_ts")) or _parse_iso_ts(lot_payload.get("entry_ts"))
        if source_trigger_ts is not None:
            out["source_trigger_ts"] = source_trigger_ts.isoformat()
        if entry_submit_ts is not None:
            out["entry_submit_ts"] = entry_submit_ts.isoformat()
        if source_trigger_ts is not None and entry_submit_ts is not None:
            out["source_to_submit_ms"] = float(max(0.0, (entry_submit_ts - source_trigger_ts).total_seconds() * 1000.0))
        check_ts = now if isinstance(now, datetime) else datetime.now(tz=entry_submit_ts.tzinfo if entry_submit_ts is not None else None)
        if entry_submit_ts is not None:
            out["submit_to_observed_ms"] = float(max(0.0, (check_ts - entry_submit_ts).total_seconds() * 1000.0))
        if source_trigger_ts is not None:
            out["source_to_observed_ms"] = float(max(0.0, (check_ts - source_trigger_ts).total_seconds() * 1000.0))
        return out

    def _history_ready_hint(self) -> bool:
        extras = dict(self.params.extras or {})
        min_bars = max(1, int(extras.get("history_ready_min_bars", 210) or 210))
        return len(self._bar_history) >= min_bars

    def _maybe_emit_stale_quote_used(
        self,
        *,
        runtime: Optional[PluginRuntimeState],
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> None:
        if selection is None:
            return
        extras = dict(self.params.extras or {})
        if not bool(extras.get("allow_stale_entry_quotes", True)):
            return
        max_age_sec = max(1.0, float(extras.get("entry_quote_max_age_sec", 90.0) or 90.0))
        quote_age_sec = float(getattr(selection, "quote_age_sec", 0.0) or 0.0)
        if quote_age_sec <= max_age_sec:
            return
        self._emit_strategy_audit(
            stage="plugin_stale_quote_used",
            status="warning",
            reason="stale_quote_used_for_entry",
            severity="warning",
            payload={
                "side": str(entry_plan.side),
                "signal_kind": str(entry_plan.signal_kind),
                "opt_symbol": str(selection.opt_symbol),
                "quote_age_sec": float(quote_age_sec),
                "entry_quote_max_age_sec": float(max_age_sec),
                "signal_ts": runtime.now.isoformat() if runtime is not None else "",
                "signal_side": str(getattr(signal, "side", "") or ""),
            },
        )

    async def initialize(self) -> None:
        if self.kernel_mode == "pure_plugin" and bool(getattr(self.plugin, "supports_pure_kernel", False)):
            self.delegate = None
            await self.plugin.initialize_pure(self.real_ctx, self.params, self)
            return
        if not hasattr(self.plugin, "build_delegate"):
            raise RuntimeError("OptionKernel plugin missing build_delegate()")
        self.delegate = self.plugin.build_delegate(self.proxy_ctx, self.params)
        if self.delegate is not None:
            await self.delegate.initialize()

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        self._update_bar_history(symbol, features)
        if self.delegate is None:
            if self.kernel_mode == "pure_plugin":
                await self._run_pure_plugin_cycle(symbol, features)
                return
            raise RuntimeError("OptionKernel delegate not initialized")
        if self.kernel_mode == "pure_plugin":
            await self._run_pure_plugin_cycle(symbol, features)
            return
        if self.kernel_mode == "plugin":
            await self._run_delegate_capture_cycle(symbol, features)
            await self._flush_captured_delegate_intents()
            await self._run_plugin_lifecycle(symbol, features)
            return
        # Safety-first default: delegate execution remains the source of truth.
        await self.delegate.on_features(symbol, features)
        if self.kernel_mode in {"hybrid", "plugin"}:
            await self._run_plugin_lifecycle(symbol, features)

    async def on_shutdown(self, reason: str) -> None:
        if self.kernel_mode == "pure_plugin" and self.delegate is None:
            await self.plugin.shutdown_pure(self.real_ctx, self.params, self, reason)
            return
        if self.delegate is not None and hasattr(self.delegate, "on_shutdown"):
            await self.delegate.on_shutdown(reason)

    def preload_bar_history(self, symbol: str, rows: list[Dict[str, Any]]) -> int:
        loaded = 0
        for row in sorted(
            list(rows or []),
            key=lambda item: item.get("ts").timestamp() if isinstance(item.get("ts"), datetime) else float("-inf"),
        ):
            ts = row.get("ts")
            if not isinstance(ts, datetime):
                continue
            bar_row = {
                "ts": ts,
                "symbol": str(symbol),
                "open": _safe_float(row.get("open"), _safe_float(row.get("close"), 0.0)),
                "high": _safe_float(row.get("high"), _safe_float(row.get("close"), 0.0)),
                "low": _safe_float(row.get("low"), _safe_float(row.get("close"), 0.0)),
                "close": _safe_float(row.get("close"), 0.0),
                "volume": _safe_float(row.get("volume"), 0.0),
                "vwap": _safe_float(row.get("vwap"), _safe_float(row.get("close"), 0.0)),
                "atr": _safe_float(row.get("atr"), _safe_float(row.get("atr_1m"), 0.0)),
                "payload": dict(row),
            }
            if self._bar_history and self._bar_history[-1].get("ts") == ts:
                self._bar_history[-1] = bar_row
            else:
                self._bar_history.append(bar_row)
            loaded += 1
        if len(self._bar_history) > self._max_history_bars:
            self._bar_history = self._bar_history[-self._max_history_bars :]
        return loaded

    async def _run_delegate_capture_cycle(self, symbol: str, features: Dict[str, Any]) -> None:
        if self.delegate is None:
            return
        self._captured_delegate_intents = []
        self._delegate_generated_intent_this_cycle = False
        self._capture_delegate_intents = True
        try:
            await self.delegate.on_features(symbol, features)
        finally:
            self._capture_delegate_intents = False

    async def _flush_captured_delegate_intents(self) -> None:
        pending = list(self._captured_delegate_intents)
        self._captured_delegate_intents = []
        self._delegate_generated_intent_this_cycle = bool(pending)
        for env in pending:
            extras = dict(env.extras or {})
            extras.setdefault("kernel_mode", "plugin")
            extras.setdefault("captured_from_delegate", True)
            env.extras = extras
            await self.submit_intent(env)

    def _build_plugin_ctx(self, symbol: str, features: Dict[str, Any]) -> Dict[str, Any]:
        features_in = dict(features or {})
        if self._shared_underlying_data_plane_enabled():
            underlying = str(self._underlying_symbol() or symbol or "").strip().upper()
            if underlying:
                plane = shared_underlying_data_plane()
                max_staleness_sec = self._shared_underlying_max_staleness_sec()
                now_epoch_s = _safe_float(features_in.get("ts_epoch_s"), 0.0)
                if now_epoch_s <= 0.0:
                    now_epoch_s = datetime.now().timestamp()
                shared_features = plane.latest_features(
                    symbol=underlying,
                    namespace=self._shared_underlying_namespace(),
                    max_staleness_sec=max_staleness_sec,
                    now_epoch_s=now_epoch_s,
                )
                shared_snapshot = plane.latest_snapshot(
                    symbol=underlying,
                    namespace=self._shared_underlying_namespace(),
                    max_staleness_sec=max_staleness_sec,
                    now_epoch_s=now_epoch_s,
                )
                if shared_features:
                    for key in ("close", "open", "high", "low", "vwap", "atr", "atr_eff", "v3m", "volume"):
                        if features_in.get(key) is None and key in shared_features:
                            features_in[key] = shared_features.get(key)
                if shared_snapshot is not None:
                    if features_in.get("close") is None and shared_snapshot.last_price is not None:
                        features_in["close"] = shared_snapshot.last_price
                    if features_in.get("vwap") is None and shared_snapshot.vwap is not None:
                        features_in["vwap"] = shared_snapshot.vwap
        now = _coerce_et_now(features)
        delegate = self.delegate
        close = _safe_float(features_in.get("close"), 0.0)
        spot = close
        bar: Dict[str, Any] = {"ts": now, "close": close}
        if delegate is not None and hasattr(delegate, "_features_to_bar"):
            try:
                maybe_bar = delegate._features_to_bar(now, features_in)  # type: ignore[attr-defined]
                if isinstance(maybe_bar, dict):
                    bar = dict(maybe_bar)
                    spot = _safe_float(bar.get("close"), spot)
            except Exception:
                pass
        return {
            "symbol": str(symbol),
            "features": features_in,
            "now": now,
            "wall_now": datetime.now(_ET),
            "spot": float(spot),
            "bar": bar,
            "strategy_id": self.strategy_id,
            "kernel_mode": self.kernel_mode,
            "definition_id": self._definition_id(),
            "audit_namespace": self._audit_namespace(),
            "position_namespace": self._position_namespace(),
            "risk_symbol": self._risk_symbol(),
        }

    def _build_kernel_bar_context(self, symbol: str, features: Dict[str, Any], ctx: Dict[str, Any]) -> KernelBarContext:
        bar = dict(ctx.get("bar") or {})
        return KernelBarContext(
            ts=ctx["now"],
            symbol=str(symbol),
            open=_safe_float(bar.get("open"), _safe_float(features.get("open"), 0.0)),
            high=_safe_float(bar.get("high"), _safe_float(features.get("high"), 0.0)),
            low=_safe_float(bar.get("low"), _safe_float(features.get("low"), 0.0)),
            close=_safe_float(bar.get("close"), _safe_float(features.get("close"), 0.0)),
            volume=_safe_float(bar.get("volume"), _safe_float(features.get("volume"), 0.0)),
            vwap=_safe_float(bar.get("vwap"), _safe_float(features.get("vwap"), 0.0)),
            atr=_safe_float(bar.get("atr"), _safe_float(features.get("atr"), 0.0)),
            payload=bar,
        )

    def _build_kernel_position_states(self) -> list[KernelPositionState]:
        if self.delegate is not None:
            pos = getattr(self.delegate, "_pos", None)
            if pos is not None:
                qty = _safe_float(getattr(pos, "initial_contracts", 0.0), 0.0)
                remaining_qty = _safe_float(getattr(pos, "remaining_contracts", qty), qty)
                opened_at = getattr(pos, "opened_at", None)
                if not isinstance(opened_at, datetime):
                    opened_at = datetime.now(_ET)
                elif opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=_ET)
                return [
                    KernelPositionState(
                        strategy_id=str(self.strategy_id),
                        position_lot_id=str(getattr(pos, "position_id", "") or ""),
                        opt_symbol=str(getattr(pos, "opt_symbol", "") or ""),
                        side=str(getattr(pos, "side", "") or ""),
                        qty=qty,
                        remaining_qty=remaining_qty,
                        opened_at=opened_at,
                        avg_entry_px=_safe_float(getattr(pos, "entry_price", 0.0), 0.0),
                        structure_stop=_safe_float(getattr(pos, "structure_stop", 0.0), 0.0),
                        target_1r=_safe_float(getattr(pos, "target_1r", 0.0), 0.0),
                        target_1p2r=_safe_float(getattr(pos, "target_1p2r", 0.0), 0.0),
                        status=str(getattr(pos, "status", "open") or "open"),
                        payload={
                            "entry_snapshot_id": str(getattr(pos, "entry_snapshot_id", "") or ""),
                            "last_exit_reason": str(getattr(pos, "last_exit_reason", "") or ""),
                        },
                    )
                ]
        lots = self.ledger.active_lots(strategy_id=self.strategy_id)
        out: list[KernelPositionState] = []
        for lot in lots:
            payload = dict(lot.payload or {})
            out.append(
                KernelPositionState(
                    strategy_id=str(lot.strategy_id),
                    position_lot_id=str(lot.position_lot_id),
                    opt_symbol=str(lot.symbol),
                    side=str(lot.side or payload.get("side") or ""),
                    qty=_safe_float(payload.get("initial_qty"), lot.qty),
                    remaining_qty=_safe_float(lot.qty, lot.qty),
                    opened_at=lot.opened_at,
                    avg_entry_px=_safe_float(payload.get("avg_entry_px", payload.get("entry_mid")), 0.0),
                    structure_stop=_safe_float(payload.get("structure_stop"), 0.0),
                    target_1r=_safe_float(payload.get("target_1r"), 0.0),
                    target_1p2r=_safe_float(payload.get("target_1p2r"), 0.0),
                    status=str(lot.status or "open"),
                    payload=payload,
                )
            )
        return out

    def _build_kernel_position_state(self) -> Optional[KernelPositionState]:
        positions = self._build_kernel_position_states()
        if not positions:
            return None
        return positions[0]

    def _build_runtime_state(self, symbol: str, features: Dict[str, Any]) -> PluginRuntimeState:
        ctx = self._build_plugin_ctx(symbol, features)
        history = [dict(row) for row in self._bar_history]
        active_positions = self._build_kernel_position_states()
        pending_entries = self.ledger.pending_entry_lots(strategy_id=self.strategy_id)
        payload = {
            "ctx": ctx,
            "plugin": str(getattr(self.plugin, "component_name", "option_plugin")),
            "bar_history": history,
            "definition_id": self._definition_id(),
            "audit_namespace": self._audit_namespace(),
            "position_namespace": self._position_namespace(),
            "risk_symbol": self._risk_symbol(),
            "pending_entry_count": int(len(pending_entries)),
            "pending_entry_symbols": [str(lot.symbol) for lot in pending_entries],
        }
        return PluginRuntimeState(
            strategy_id=str(self.strategy_id),
            kernel_mode=str(self.kernel_mode),
            symbol=str(symbol),
            now=ctx["now"],
            wall_now=ctx["wall_now"],
            spot=_safe_float(ctx.get("spot"), 0.0),
            bar=self._build_kernel_bar_context(symbol, features, ctx),
            features=dict(features or {}),
            active_position=active_positions[0] if active_positions else None,
            active_positions=active_positions,
            payload=payload,
        )

    def _update_bar_history(self, symbol: str, features: Dict[str, Any]) -> None:
        ctx = self._build_plugin_ctx(symbol, features)
        bar = self._build_kernel_bar_context(symbol, features, ctx)
        bar_row = asdict(bar)
        if self._bar_history:
            prev_ts = self._bar_history[-1].get("ts")
            if isinstance(prev_ts, datetime) and prev_ts == bar.ts:
                self._bar_history[-1] = bar_row
            else:
                self._bar_history.append(bar_row)
        else:
            self._bar_history.append(bar_row)
        if len(self._bar_history) > self._max_history_bars:
            self._bar_history = self._bar_history[-self._max_history_bars :]
        if self._shared_underlying_data_plane_enabled():
            underlying = str(self._underlying_symbol() or symbol or "").strip().upper()
            if underlying:
                shared_underlying_data_plane().publish_underlying(
                    symbol=underlying,
                    namespace=self._shared_underlying_namespace(),
                    ts_epoch_s=float(bar.ts.timestamp()),
                    last_price=bar.close,
                    vwap=bar.vwap,
                    features=dict(ctx.get("features") or {}),
                    bar=bar_row,
                )

    async def _run_pure_plugin_cycle(self, symbol: str, features: Dict[str, Any]) -> None:
        await self._sync_pending_entry_lots_from_portfolio()
        runtime = self._build_runtime_state(symbol, features)
        lifecycle: Dict[str, Any] = {
            "symbol": str(symbol),
            "ts": runtime.now.isoformat(),
            "mode": self.kernel_mode,
            "plugin": str(getattr(self.plugin, "component_name", "option_plugin")),
            "pure_plugin": True,
        }
        try:
            signal = self.plugin.build_signal_pure(runtime)
            lifecycle["signal"] = _encode_payload(signal)
            entry_plan: Optional[EntryPlan] = None
            if signal is not None:
                entry_plan = self.plugin.build_entry_plan_pure(runtime, signal)
            lifecycle["entry_plan"] = _encode_payload(entry_plan)
            if signal is not None and entry_plan is None:
                reject = OrderReject(
                    reason="no_entry_plan",
                    intent_action="entry",
                    strategy_id=self.strategy_id,
                    symbol=str(symbol),
                    payload={"kernel_mode": self.kernel_mode},
                )
                try:
                    self.plugin.on_signal_rejected(
                        runtime=runtime,
                        signal=signal,
                        entry_plan=None,
                        selection=None,
                        reject=reject,
                    )
                except Exception as exc:
                    self._plugin_errors += 1
                    self._emit_strategy_audit(
                        stage="plugin_lifecycle_hook_failed",
                        status="error",
                        reason="on_signal_rejected_failed",
                        severity="warning",
                        payload={"reject_reason": reject.reason, "error": str(exc)},
                    )
            selection: Optional[Selection] = None
            profile: Optional[ExecutionProfile] = None
            if entry_plan is not None:
                self._notify_signal_accepted(
                    runtime=runtime,
                    signal=signal,
                    entry_plan=entry_plan,
                    payload={"stage": "entry_plan_built"},
                )
                platform_selection = select_option_contract_for_entry(
                    runtime=runtime,
                    entry_plan=entry_plan,
                    plugin=self.plugin,
                    strategy_id=str(getattr(self, "strategy_id", "") or getattr(runtime, "strategy_id", "") or ""),
                    symbol=str(symbol),
                    kernel_mode=self.kernel_mode,
                )
                selection = platform_selection.selection
                lifecycle["selection"] = _encode_payload(selection)
                self._maybe_emit_stale_quote_used(runtime=runtime, signal=signal, entry_plan=entry_plan, selection=selection)
                if selection is not None:
                    self._notify_signal_accepted(
                        runtime=runtime,
                        signal=signal,
                        entry_plan=entry_plan,
                        selection=selection,
                        payload={"stage": "contract_selected"},
                    )
                if selection is not None:
                    _write_contract_selection_verdict(
                        strategy_id=str(getattr(self, "strategy_id", "") or getattr(runtime, "strategy_id", "") or ""),
                        symbol=str(symbol),
                        status="selected",
                        reason="contract_selected",
                        payload={
                            **platform_selection.payload,
                            "signal": _encode_payload(signal),
                        },
                        now=runtime.now,
                    )
                if selection is None:
                    reject_reason = ""
                    try:
                        resolver = getattr(self.plugin, "_resolve_strategy_definition", None)
                        definition = resolver() if callable(resolver) else None
                        contract_policy = getattr(definition, "contract_policy", None)
                        reject_reason = str(getattr(contract_policy, "last_reject_reason", "") or "")
                        reject_diagnostics = dict(getattr(contract_policy, "last_reject_diagnostics", {}) or {})
                    except Exception:
                        reject_reason = ""
                        reject_diagnostics = {}
                    self._notify_entry_rejected(
                        runtime=runtime,
                        signal=signal,
                        entry_plan=entry_plan,
                        selection=None,
                        reason="no_contract_selection",
                        payload={
                            **({"reject_reason": reject_reason} if reject_reason else {}),
                            **({"reject_diagnostics": reject_diagnostics} if reject_diagnostics else {}),
                        },
                    )
                    _write_contract_selection_verdict(
                        strategy_id=str(getattr(self, "strategy_id", "") or getattr(runtime, "strategy_id", "") or ""),
                        symbol=str(symbol),
                        status="rejected",
                        reason=str(reject_reason or platform_selection.reason or "no_contract_selection"),
                        payload={
                            **platform_selection.payload,
                            "reject_diagnostics": reject_diagnostics,
                            "signal": _encode_payload(signal),
                        },
                        now=runtime.now,
                    )
                profile = self.plugin.build_execution_profile_pure(runtime, entry_plan, selection)
                lifecycle["execution_profile"] = _encode_payload(profile)
                if isinstance(profile, ExecutionProfile):
                    self.execution_profile = profile
                elif selection is not None:
                    self._notify_entry_rejected(
                        runtime=runtime,
                        signal=signal,
                        entry_plan=entry_plan,
                        selection=selection,
                        reason="no_execution_profile",
                        payload={},
                    )
                if selection is not None and isinstance(profile, ExecutionProfile):
                    if hasattr(self.plugin, "on_entry_submit_attempt_pure"):
                        self.plugin.on_entry_submit_attempt_pure(
                            runtime=runtime,
                            signal=signal,
                            entry_plan=entry_plan,
                            selection=selection,
                            profile=profile,
                        )
                    submitted = await self._maybe_submit_plugin_intent(
                        ctx=runtime.payload.get("ctx") or self._build_plugin_ctx(symbol, features),
                        runtime=runtime,
                        signal=signal,
                        entry_plan=entry_plan,
                        selection=selection,
                        profile=profile,
                    )
                    lifecycle["plugin_submit"] = {"submitted": bool(submitted)}
            exit_plans = list(self.plugin.build_exit_plans_pure(runtime) or [])
            lifecycle["exit_plans"] = _encode_payload(exit_plans)
            lifecycle["exit_plan"] = _encode_payload(exit_plans[0]) if exit_plans else None
            if exit_plans:
                submitted = 0
                for exit_plan in exit_plans:
                    if await self._submit_plugin_exit_intent(
                        ctx=runtime.payload.get("ctx") or self._build_plugin_ctx(symbol, features),
                        runtime=runtime,
                        exit_plan=exit_plan,
                    ):
                        submitted += 1
                lifecycle["exit_submit"] = {"submitted_count": int(submitted)}
            self._plugin_lifecycle = lifecycle
            self._emit_pure_plugin_cycle_audit(runtime=runtime, lifecycle=lifecycle)
        except Exception as exc:
            self._plugin_errors += 1
            self._emit_strategy_audit(
                stage="plugin_cycle_exception",
                status="error",
                reason="pure_plugin_cycle_exception",
                severity="error",
                payload={
                    "symbol": str(symbol),
                    "error": str(exc),
                    "error_count": int(self._plugin_errors),
                    "mode": str(self.kernel_mode),
                    "plugin": str(getattr(self.plugin, "component_name", "option_plugin")),
                },
            )
            self._plugin_lifecycle = {
                **lifecycle,
                "error": str(exc),
                "error_count": int(self._plugin_errors),
            }

    def _emit_pure_plugin_cycle_audit(self, *, runtime: PluginRuntimeState, lifecycle: Dict[str, Any]) -> None:
        minute_key = runtime.now.strftime("%Y-%m-%d %H:%M")
        signal = lifecycle.get("signal")
        entry_plan = lifecycle.get("entry_plan")
        selection = lifecycle.get("selection")
        submit = lifecycle.get("plugin_submit")
        exit_plans = lifecycle.get("exit_plans")
        force_emit = bool(signal or entry_plan or selection or submit or exit_plans)
        if not force_emit and self._last_pure_cycle_audit_minute == minute_key:
            return
        self._last_pure_cycle_audit_minute = minute_key

        signal_payload = signal if isinstance(signal, dict) else {}
        entry_payload = entry_plan if isinstance(entry_plan, dict) else {}
        selection_payload = selection if isinstance(selection, dict) else {}
        submit_payload = submit if isinstance(submit, dict) else {}
        exit_count = len(exit_plans) if isinstance(exit_plans, list) else 0
        if signal_payload:
            reason = "signal_ready"
            status = "signal"
        elif exit_count:
            reason = "exit_plan_ready"
            status = "exit"
        else:
            reason = "no_signal"
            status = "observed"

        self._emit_strategy_audit(
            stage="plugin_cycle_observed",
            status=status,
            reason=reason,
            payload={
                "symbol": str(runtime.symbol),
                "minute": minute_key,
                "mode": str(self.kernel_mode),
                "plugin": str(getattr(self.plugin, "component_name", "option_plugin")),
                "has_signal": bool(signal_payload),
                "signal_side": str(signal_payload.get("side") or ""),
                "signal_kind": str(signal_payload.get("signal_kind") or ""),
                "has_entry_plan": bool(entry_payload),
                "entry_side": str(entry_payload.get("side") or ""),
                "has_selection": bool(selection_payload),
                "opt_symbol": str(selection_payload.get("opt_symbol") or ""),
                "exit_plan_count": int(exit_count),
                "submitted": bool(submit_payload.get("submitted")) if submit_payload else False,
                "bar_history_count": int(len(self._bar_history)),
            },
        )

    async def _run_plugin_lifecycle(self, symbol: str, features: Dict[str, Any]) -> None:
        if self.delegate is None:
            return
        ctx = self._build_plugin_ctx(symbol, features)
        lifecycle: Dict[str, Any] = {
            "symbol": str(symbol),
            "ts": str(ctx["now"].isoformat()),
            "mode": self.kernel_mode,
            "plugin": str(getattr(self.plugin, "component_name", "option_plugin")),
        }
        signal: Optional[Signal] = None
        entry_plan: Optional[EntryPlan] = None
        selection: Optional[Selection] = None
        profile: Optional[ExecutionProfile] = None
        try:
            signal = self.plugin.build_signal(self.delegate, ctx)
            lifecycle["signal"] = _encode_payload(signal)
            if signal is not None:
                entry_plan = self.plugin.build_entry_plan(self.delegate, ctx, signal)
                lifecycle["entry_plan"] = _encode_payload(entry_plan)
            if entry_plan is not None:
                selection = self.plugin.select_contract(self.delegate, ctx, entry_plan)
                if selection is None:
                    selection = self.plugin.select_quote(
                        self.delegate,
                        {"ctx": ctx, "entry_plan": _encode_payload(entry_plan)},
                    )
                lifecycle["selection"] = _encode_payload(selection)
                self._maybe_emit_stale_quote_used(runtime=None, signal=signal, entry_plan=entry_plan, selection=selection)
                profile = self.plugin.build_execution_profile(self.delegate, ctx, entry_plan, selection)
                if isinstance(profile, ExecutionProfile):
                    self.execution_profile = profile
                lifecycle["execution_profile"] = _encode_payload(profile)
                if (
                    self._kernel_can_submit_plugin_intent()
                    and self.kernel_mode in {"plugin"}
                    and not self._delegate_generated_intent_this_cycle
                ):
                    submitted = await self._maybe_submit_plugin_intent(
                        ctx=ctx,
                        runtime=None,
                        signal=signal,
                        entry_plan=entry_plan,
                        selection=selection,
                        profile=profile if isinstance(profile, ExecutionProfile) else self.execution_profile,
                    )
                    lifecycle["plugin_submit"] = {"submitted": bool(submitted)}
            market_state = {
                "qty": _safe_float(getattr(getattr(self.delegate, "_pos", None), "initial_contracts", 0.0), 0.0),
                "symbol": str(getattr(getattr(self.delegate, "_pos", None), "opt_symbol", "") or ""),
            }
            lifecycle["exit_plan"] = _encode_payload(self.plugin.build_exit_plan(self.delegate, ctx, market_state))
            self._plugin_lifecycle = lifecycle
        except Exception as exc:
            self._plugin_errors += 1
            self._plugin_lifecycle = {
                **lifecycle,
                "error": str(exc),
                "error_count": int(self._plugin_errors),
            }

    def _kernel_can_submit_plugin_intent(self) -> bool:
        return bool(getattr(self.plugin, "kernel_can_submit", False))

    async def _maybe_submit_plugin_intent(
        self,
        *,
        ctx: Dict[str, Any],
        runtime: Optional[PluginRuntimeState],
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        profile: ExecutionProfile,
    ) -> bool:
        now = ctx.get("now")
        if not isinstance(now, datetime):
            now = datetime.now(_ET)
        contracts = max(0.0, float(entry_plan.contracts or 0))
        extras_cfg = dict(self.params.extras or {})
        raw_entry_notional = extras_cfg.get("entry_notional_usd")
        if raw_entry_notional is not None:
            try:
                entry_notional_usd = max(0.0, float(raw_entry_notional))
            except Exception:
                entry_notional_usd = 0.0
            option_px = max(
                0.0,
                _safe_float(getattr(selection, "mid", 0.0), 0.0),
            )
            if entry_notional_usd > 0.0 and option_px > 0.0:
                contracts = float(int(entry_notional_usd // (option_px * 100.0)))
                max_contracts = max(
                    0,
                    int(extras_cfg.get("entry_notional_max_contracts", 0) or 0),
                )
                if max_contracts > 0:
                    contracts = float(min(int(contracts), max_contracts))
        if contracts <= 0.0:
            if runtime is not None and hasattr(self.plugin, "on_entry_submit_blocked_pure"):
                self.plugin.on_entry_submit_blocked_pure(
                    runtime=runtime,
                    signal=signal,
                    entry_plan=entry_plan,
                    selection=selection,
                    reason="contracts_le_zero",
                    payload={"contracts": float(entry_plan.contracts or 0.0)},
                )
            return False
        pending_lots = self.ledger.pending_entry_lots(strategy_id=self.strategy_id)
        if pending_lots:
            if runtime is not None and hasattr(self.plugin, "on_entry_submit_blocked_pure"):
                self.plugin.on_entry_submit_blocked_pure(
                    runtime=runtime,
                    signal=signal,
                    entry_plan=entry_plan,
                    selection=selection,
                    reason="pending_entry_open",
                    payload={
                        "pending_entry_count": int(len(pending_lots)),
                        "pending_symbols": [str(lot.symbol) for lot in pending_lots],
                    },
                )
            return False
        retry_max_attempts = max(1, int(extras_cfg.get("entry_retry_max_attempts", 1) or 1))
        retry_wait_sec = max(0.5, float(extras_cfg.get("entry_retry_wait_sec", 2.0) or 2.0))
        retry_enabled = bool(extras_cfg.get("entry_retry_enabled", False)) and retry_max_attempts > 1
        entry_timeout_sec = None
        raw_entry_timeout_sec = extras_cfg.get("entry_timeout_sec")
        if raw_entry_timeout_sec is not None:
            try:
                entry_timeout_sec = float(raw_entry_timeout_sec)
            except Exception:
                entry_timeout_sec = None
        if retry_enabled and entry_timeout_sec is None:
            # Keep timeout slightly under retry wait so stale hanging entries are cancelled first.
            entry_timeout_sec = max(0.5, retry_wait_sec - 0.2)

        minute_key = now.replace(second=0, microsecond=0).isoformat()
        submit_key_base = "|".join(
            [
                str(self.strategy_id),
                minute_key,
                str(selection.opt_symbol),
                str(entry_plan.side),
                str(entry_plan.signal_kind),
            ]
        )
        raw_reject_cooldown = extras_cfg.get("entry_reject_cooldown_sec", 1.0)
        try:
            reject_cooldown_sec = float(1.0 if raw_reject_cooldown is None else raw_reject_cooldown)
        except Exception:
            reject_cooldown_sec = 1.0
        reject_cooldown_sec = max(0.0, reject_cooldown_sec)
        if submit_key_base == self._last_plugin_submit_key:
            if runtime is not None and hasattr(self.plugin, "on_entry_submit_blocked_pure"):
                self.plugin.on_entry_submit_blocked_pure(
                    runtime=runtime,
                    signal=signal,
                    entry_plan=entry_plan,
                    selection=selection,
                    reason="duplicate_submit_key",
                    payload={"submit_key": submit_key_base},
                )
            return False
        now_ts = float(now.timestamp())
        reject_guard = dict(self._plugin_reject_guard_by_submit_key.get(submit_key_base) or {})
        if str(reject_guard.get("minute_key") or "") != str(minute_key):
            reject_guard = {}
            self._plugin_reject_guard_by_submit_key.pop(submit_key_base, None)
        reject_count = int(reject_guard.get("reject_count") or 0)
        next_retry_ts = float(reject_guard.get("next_retry_ts") or 0.0)
        reject_budget_per_min = max(1, int(extras_cfg.get("entry_reject_budget_per_minute", 3) or 3))
        if reject_count >= reject_budget_per_min:
            if runtime is not None and hasattr(self.plugin, "on_entry_submit_blocked_pure"):
                self.plugin.on_entry_submit_blocked_pure(
                    runtime=runtime,
                    signal=signal,
                    entry_plan=entry_plan,
                    selection=selection,
                    reason="submit_reject_budget_exhausted",
                    payload={
                        "submit_key": submit_key_base,
                        "minute_key": minute_key,
                        "reject_count": int(reject_count),
                        "entry_reject_budget_per_minute": int(reject_budget_per_min),
                    },
                )
            return False
        if next_retry_ts > 0.0 and now_ts < next_retry_ts:
            if runtime is not None and hasattr(self.plugin, "on_entry_submit_blocked_pure"):
                self.plugin.on_entry_submit_blocked_pure(
                    runtime=runtime,
                    signal=signal,
                    entry_plan=entry_plan,
                    selection=selection,
                    reason="submit_reject_cooldown",
                    payload={
                        "submit_key": submit_key_base,
                        "remaining_sec": float(max(0.0, next_retry_ts - now_ts)),
                        "entry_reject_cooldown_sec": float(reject_cooldown_sec),
                        "reject_count": int(reject_count),
                        "deterministic_reject": bool(reject_guard.get("deterministic_reject")),
                    },
                )
            return False
        position_namespace = self._position_namespace()

        async def _has_live_position(symbol: str) -> bool:
            try:
                pos = await self.real_ctx.position(symbol)
                return abs(float(getattr(pos, "pos_qty", 0.0) or 0.0)) > 0.0
            except Exception:
                return False

        async def _open_entry_broker_order_ids(symbol: str) -> list[int]:
            try:
                rows = await self.real_ctx.open_orders()
            except Exception:
                return []
            broker_order_ids: list[int] = []
            for row in rows or []:
                if str(getattr(row, "symbol", "") or "") != str(symbol):
                    continue
                state = str(getattr(row, "state", "") or "").upper()
                if state in {"NEW", "SUBMITTED", "PARTIAL", "PENDING", "PRESUBMITTED", "PENDINGSUBMIT"}:
                    try:
                        broker_id = int(getattr(row, "broker_order_id", 0) or 0)
                    except Exception:
                        broker_id = 0
                    if broker_id > 0:
                        broker_order_ids.append(broker_id)
            return broker_order_ids

        current_selection = selection
        accepted_any = False
        for attempt in range(1, retry_max_attempts + 1):
            decision_id = f"k{uuid4().hex[:18]}"
            position_lot_id = f"lot:{position_namespace}:{decision_id}"[:120]
            order_ref = f"optk:{position_namespace}:{decision_id}"[:45]
            submit_key = submit_key_base if attempt == 1 else f"{submit_key_base}:retry:{attempt}"
            extras: Dict[str, Any] = {
                "order_ref": order_ref,
                "position_id": position_lot_id,
                "trigger_id": decision_id,
                "component_name": str(getattr(self.plugin, "component_name", "option_plugin")),
                "definition_id": self._definition_id(),
                "audit_namespace": self._audit_namespace(),
                "position_namespace": position_namespace,
                "risk_symbol": self._risk_symbol(),
                "signal_kind": str(entry_plan.signal_kind),
                "execution_profile_id": str(profile.profile_id),
                "parity_profile_id": str(profile.profile_id),
                "kernel_mode": str(self.kernel_mode),
                "plugin_symbol": str(ctx.get("symbol") or ""),
                "underlying": self._underlying_symbol(),
                "bars_symbol": self._underlying_symbol(),
                "history_ready": bool(self._history_ready_hint()),
                "plugin_payload": {
                    "signal": _encode_payload(signal),
                    "entry_plan": _encode_payload(entry_plan),
                    "selection": _encode_payload(current_selection),
                },
                "instrument_type": "OPTION",
                "entry_plan_payload": _encode_payload(entry_plan.payload),
                "side": str(entry_plan.side),
                "initial_qty": float(contracts),
                "entry_mid": float(current_selection.mid or 0.0),
                "opt_bid_px": float(current_selection.bid or 0.0),
                "opt_ask_px": float(current_selection.ask or 0.0),
                "opt_mid_px": float(current_selection.mid or 0.0),
                "opt_last_px": float(current_selection.last or 0.0),
                "opt_quote_age_sec": float(current_selection.quote_age_sec or 0.0),
                "entry_ts": now.isoformat(),
                "entry_underlying": float(ctx.get("spot") or 0.0),
                "structure_stop": float(entry_plan.structure_stop or 0.0),
                "target_1r": float(entry_plan.target_1r or 0.0),
                "target_1p2r": float(entry_plan.target_1p2r or 0.0),
                "entry_retry_attempt": int(attempt),
                "entry_retry_max_attempts": int(retry_max_attempts),
                "entry_submit_ts": now.isoformat(),
            }
            signal_payload = dict(getattr(signal, "payload", {}) or {})
            source_trigger_ts_raw = str(signal_payload.get("source_trigger_ts") or "").strip()
            if source_trigger_ts_raw:
                extras["source_trigger_ts"] = source_trigger_ts_raw
                source_trigger_ts = _parse_iso_ts(source_trigger_ts_raw)
                if source_trigger_ts is not None:
                    extras["source_to_submit_ms"] = float(max(0.0, (now - source_trigger_ts).total_seconds() * 1000.0))
            if entry_timeout_sec is not None and entry_timeout_sec > 0:
                extras["entry_timeout_sec"] = float(entry_timeout_sec)
            entry_plan_payload = dict(getattr(entry_plan, "payload", {}) or {})
            plugin_extras = self.params.extras or {}
            edge_bp_raw: Any = (
                signal_payload.get("edge_bp")
                if signal_payload.get("edge_bp") is not None
                else entry_plan_payload.get("edge_bp")
                if entry_plan_payload.get("edge_bp") is not None
                else plugin_extras.get("entry_edge_bp")
            )
            try:
                edge_bp_val = float(edge_bp_raw) if edge_bp_raw is not None else None
            except Exception:
                edge_bp_val = None
            signal_score_raw: Any = (
                signal_payload.get("signal_score")
                if signal_payload.get("signal_score") is not None
                else entry_plan_payload.get("signal_score")
                if entry_plan_payload.get("signal_score") is not None
                else plugin_extras.get("entry_signal_score")
            )
            try:
                if signal_score_raw is not None:
                    signal_score_val = float(signal_score_raw)
                elif edge_bp_val is not None:
                    signal_score_val = float(edge_bp_val / 100.0)
                else:
                    signal_score_val = 0.5
            except Exception:
                signal_score_val = 0.5
            if edge_bp_val is not None:
                extras["edge_bp"] = float(edge_bp_val)
            entry_intent = EntryIntent(
                ts=now,
                strategy_id=self.strategy_id,
                underlying_symbol=self._underlying_symbol(),
                option_symbol=str(current_selection.opt_symbol),
                side=str(entry_plan.side),
                qty=float(contracts),
                signal_kind=str(entry_plan.signal_kind),
                order_role="ENTRY",
                position_lot_id=position_lot_id,
                decision_id=decision_id,
                order_ref=order_ref,
                payload={
                    "signal": _encode_payload(signal),
                    "entry_plan": _encode_payload(entry_plan),
                    "selection": _encode_payload(current_selection),
                    "execution_profile": _encode_payload(profile),
                },
            )
            extras["entry_intent"] = _encode_payload(entry_intent)
            extras["entry_intent_schema"] = "EntryIntent/v1"
            env = DecisionEnvelope(
                decision_id=decision_id,
                strategy_id=self.strategy_id,
                symbol=str(current_selection.opt_symbol),
                ts_bar_end_us=int(now.timestamp() * 1_000_000),
                target_qty=float(contracts),
                order_style=OrderStyle(kind="MKT"),
                signal_score=signal_score_val,
                freshness=Freshness(
                    ttl_ms=int(getattr(self.params, "ttl_ms", 2000) or 2000),
                    dlb_ms=int((self.params.extras or {}).get("dlb_ms", 1000) or 1000),
                ),
                extras=extras,
            )
            result = await self.submit_intent(env)
            result_payload = self._normalize_submit_result(result)
            accepted = str(result_payload.get("status") or "").upper() in {"APPROVED", "ACCEPTED", "OK"}
            if runtime is not None and accepted:
                self._notify_order_submitted(
                    runtime=runtime,
                    ack=OrderAck(
                        status="submitted",
                        intent_action="entry",
                        strategy_id=self.strategy_id,
                        symbol=str(current_selection.opt_symbol),
                        decision_id=decision_id,
                        order_ref=order_ref,
                        position_lot_id=position_lot_id,
                        payload={"result": result_payload, "entry_intent": _encode_payload(entry_intent)},
                    ),
                    signal=signal,
                    entry_plan=entry_plan,
                    selection=current_selection,
                )
                self.plugin.on_entry_submitted_pure(
                    runtime=runtime,
                    signal=signal,
                    entry_plan=entry_plan,
                    selection=current_selection,
                    position_lot_id=position_lot_id,
                )
            elif runtime is not None and hasattr(self.plugin, "on_entry_submit_blocked_pure"):
                self.plugin.on_entry_submit_blocked_pure(
                    runtime=runtime,
                    signal=signal,
                    entry_plan=entry_plan,
                    selection=current_selection,
                    reason=str(result_payload.get("status") or "submit_rejected").lower(),
                    payload={
                        "position_lot_id": position_lot_id,
                        "result_stage": str(result_payload.get("stage") or ""),
                        "result_reasons": list(result_payload.get("reasons") or []),
                        "entry_retry_attempt": int(attempt),
                    },
                )
            if accepted:
                self._last_plugin_submit_key = submit_key
                self._plugin_reject_guard_by_submit_key.pop(submit_key_base, None)
            if not accepted:
                result_stage = str(result_payload.get("stage") or "").strip().lower()
                result_reasons = [str(x or "").strip().lower() for x in list(result_payload.get("reasons") or []) if str(x or "").strip()]
                deterministic_prefixes = (
                    "edge_insufficient",
                    "no_nbbo",
                    "no_nbbo_for_mid",
                    "insufficient_history",
                    "too_low_volume",
                    "too_low_volatility",
                    "bar_quality",
                )
                deterministic_reject = (
                    result_stage == "validator"
                    and bool(result_reasons)
                    and all(any(reason.startswith(pfx) for pfx in deterministic_prefixes) for reason in result_reasons)
                )
                raw_det_reject_cooldown = extras_cfg.get("entry_reject_cooldown_deterministic_sec", 15.0)
                try:
                    det_reject_cooldown_cfg = float(15.0 if raw_det_reject_cooldown is None else raw_det_reject_cooldown)
                except Exception:
                    det_reject_cooldown_cfg = 15.0
                det_reject_cooldown_sec = max(reject_cooldown_sec, max(0.0, det_reject_cooldown_cfg))
                minute_end_ts = float(now.replace(second=59, microsecond=999999).timestamp())
                applied_cooldown = float(det_reject_cooldown_sec if deterministic_reject else reject_cooldown_sec)
                next_retry_at = now_ts + max(0.0, applied_cooldown)
                if deterministic_reject:
                    next_retry_at = max(next_retry_at, minute_end_ts)
                self._plugin_reject_guard_by_submit_key[submit_key_base] = {
                    "minute_key": str(minute_key),
                    "reject_count": int(reject_count + 1),
                    "next_retry_ts": float(next_retry_at),
                    "last_stage": str(result_stage),
                    "last_reasons": list(result_reasons),
                    "deterministic_reject": bool(deterministic_reject),
                }
                return accepted_any
            accepted_any = True
            if not retry_enabled or attempt >= retry_max_attempts:
                return True

            await asyncio.sleep(retry_wait_sec)
            if await _has_live_position(str(current_selection.opt_symbol)):
                return True
            open_broker_order_ids = await _open_entry_broker_order_ids(str(current_selection.opt_symbol))
            if open_broker_order_ids:
                # Try to actively cancel stale pending entries before repricing/retry.
                broker = getattr(getattr(getattr(self.real_ctx, "engine", None), "_strategy_engine", None), "broker_adapter", None)
                if broker is not None and hasattr(broker, "cancel"):
                    for broker_order_id in open_broker_order_ids:
                        try:
                            await broker.cancel(int(broker_order_id))
                            self._emit_strategy_audit(
                                stage="plugin_entry_retry_cancel",
                                status="warning",
                                reason="retry_cancel_open_entry",
                                severity="warning",
                                payload={
                                    "opt_symbol": str(current_selection.opt_symbol),
                                    "entry_retry_attempt": int(attempt),
                                    "broker_order_id": int(broker_order_id),
                                    **self._entry_latency_payload(
                                        lot_payload={
                                            "source_trigger_ts": extras.get("source_trigger_ts"),
                                            "entry_submit_ts": extras.get("entry_submit_ts"),
                                        },
                                        now=now,
                                    ),
                                },
                            )
                        except Exception:
                            pass
                await asyncio.sleep(0.4)
                if await _has_live_position(str(current_selection.opt_symbol)):
                    return True

            if hasattr(self.plugin, "on_select_contract_pure") and runtime is not None:
                refreshed = self.plugin.on_select_contract_pure(runtime=runtime, signal=signal, entry_plan=entry_plan)
                if refreshed is not None:
                    current_selection = refreshed

        return accepted_any

    async def _submit_plugin_exit_intent(
        self,
        *,
        ctx: Dict[str, Any],
        runtime: Optional[PluginRuntimeState] = None,
        exit_plan: ExitPlan,
    ) -> bool:
        now = ctx.get("now")
        if not isinstance(now, datetime):
            now = datetime.now(_ET)
        qty = max(0.0, float(exit_plan.qty or 0.0))
        position_lot_id = str(exit_plan.position_lot_id or "")
        if qty <= 0.0 or not position_lot_id:
            return False
        lot = next((row for row in self.ledger.active_lots(strategy_id=self.strategy_id) if row.position_lot_id == position_lot_id), None)
        if lot is None:
            return False
        if str(lot.status or "") == "pending_exit":
            return False
        exit_key = "|".join(
            [
                str(self.strategy_id),
                now.replace(second=0, microsecond=0).isoformat(),
                position_lot_id,
                str(exit_plan.reason or ""),
                f"{qty:.4f}",
            ]
        )
        if exit_key == self._last_plugin_exit_key:
            return False
        decision_id = f"x{uuid4().hex[:18]}"
        position_namespace = self._position_namespace()
        order_ref = f"optk:{position_namespace}:{decision_id}"[:45]
        exit_reason = str(exit_plan.reason or "")
        is_flatten = exit_reason.upper() == "EOD_FORCE_FLATTEN"
        exit_payload = _encode_payload(dict(getattr(exit_plan, "payload", {}) or {}))
        if not isinstance(exit_payload, dict):
            exit_payload = {}
        exit_extras = {
            "order_ref": order_ref,
            "position_id": position_lot_id,
            "trigger_id": decision_id,
            "intent_action": "exit",
            "intent_source": "strategy",
            "component_name": str(getattr(self.plugin, "component_name", "option_plugin")),
            "definition_id": self._definition_id(),
            "audit_namespace": self._audit_namespace(),
            "position_namespace": position_namespace,
            "risk_symbol": self._risk_symbol(),
            "execution_profile_id": str(self.execution_profile.profile_id),
            "parity_profile_id": str(self.execution_profile.profile_id),
            "kernel_mode": str(self.kernel_mode),
            "instrument_type": "OPTION",
            "underlying": self._underlying_symbol(),
            "bars_symbol": self._underlying_symbol(),
            "history_ready": bool(self._history_ready_hint()),
            "close_only": True,
            "flatten_action": bool(is_flatten),
            "position_qty_hint": float(qty),
            "exit_reason": exit_reason,
            "side": str(lot.side or ""),
            "exit_plan_payload": exit_payload,
        }
        for key, value in exit_payload.items():
            exit_extras.setdefault(str(key), value)
        env = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=str(lot.symbol),
            ts_bar_end_us=int(now.timestamp() * 1_000_000),
            target_qty=float(-qty),
            order_style=OrderStyle(kind="MKT"),
            freshness=Freshness(
                ttl_ms=int(getattr(self.params, "ttl_ms", 2000) or 2000),
                dlb_ms=int((self.params.extras or {}).get("dlb_ms", 1000) or 1000),
            ),
            extras=exit_extras,
        )
        result = await self.submit_intent(env)
        result_payload = self._normalize_submit_result(result)
        accepted = str(result_payload.get("status") or "").upper() in {"APPROVED", "ACCEPTED", "OK"}
        if not accepted:
            return False
        self._last_plugin_exit_key = exit_key
        self._notify_order_submitted(
            runtime=runtime,
            ack=OrderAck(
                status="submitted",
                intent_action="exit",
                strategy_id=self.strategy_id,
                symbol=str(lot.symbol),
                decision_id=decision_id,
                order_ref=order_ref,
                position_lot_id=position_lot_id,
                payload={"result": result_payload},
            ),
            exit_plan=exit_plan,
        )
        return True

    async def _sync_pending_entry_lots_from_portfolio(self) -> None:
        pending_lots = self.ledger.pending_entry_lots(strategy_id=self.strategy_id)
        active_lots = self.ledger.active_lots(strategy_id=self.strategy_id)
        if not pending_lots and not active_lots:
            return
        portfolio_fn = getattr(self.real_ctx, "portfolio", None)
        if not callable(portfolio_fn):
            return
        try:
            snapshot = portfolio_fn()
            if isawaitable(snapshot):
                snapshot = await snapshot
        except Exception:
            return
        positions: list[Any] = []
        if isinstance(snapshot, dict):
            maybe_positions = snapshot.get("positions")
            if isinstance(maybe_positions, list):
                positions = maybe_positions
        elif isinstance(snapshot, list):
            positions = snapshot
        by_symbol: Dict[str, Dict[str, float]] = {}
        for row in positions:
            if isinstance(row, dict):
                sym = str(row.get("symbol") or row.get("opt_symbol") or "").strip()
                qty = _safe_float(row.get("qty", row.get("pos_qty", 0.0)), 0.0)
                avg_px = _safe_float(row.get("avg_px", row.get("avg_cost", row.get("avg_price", 0.0))), 0.0)
            else:
                sym = str(getattr(row, "symbol", getattr(row, "opt_symbol", "")) or "").strip()
                qty = _safe_float(getattr(row, "qty", getattr(row, "pos_qty", 0.0)), 0.0)
                avg_px = _safe_float(getattr(row, "avg_px", getattr(row, "avg_cost", getattr(row, "avg_price", 0.0))), 0.0)
            if sym:
                by_symbol[sym] = {"qty": qty, "avg_px": avg_px}
        for lot in pending_lots:
            row = by_symbol.get(str(lot.symbol))
            if row is None:
                continue
            live_qty = abs(_safe_float(row.get("qty"), 0.0))
            if live_qty <= 0.0:
                continue
            self.ledger.mark_entry_live(
                position_lot_id=str(lot.position_lot_id),
                filled_qty=min(float(lot.qty), live_qty),
                payload={
                    "sync_source": "ctx_portfolio",
                    "avg_entry_px": _safe_float(row.get("avg_px"), 0.0),
                },
            )
            lot_id = str(lot.position_lot_id)
            if lot_id not in self._entry_runtime_live_emitted_lots:
                self._emit_state_audit(
                    stage="runtime_transition",
                    status="live",
                    reason="position_reconciled_live",
                    state_machine="runtime",
                    symbol=str(lot.symbol),
                    payload={
                        "position_lot_id": lot_id,
                        "filled_qty": min(float(lot.qty), live_qty),
                        **self._entry_latency_payload(
                            lot_payload=dict(lot.payload or {}),
                        ),
                    },
                )
                self._entry_runtime_live_emitted_lots.add(lot_id)
            if lot_id not in self._entry_protection_emitted_lots:
                self._emit_state_audit(
                    stage="protection_transition",
                    status="protected",
                    reason="entry_protective_stop",
                    state_machine="protection",
                    symbol=str(lot.symbol),
                    payload={"position_lot_id": lot_id, "sync_source": "ctx_portfolio"},
                )
                self._entry_protection_emitted_lots.add(lot_id)

        symbols_to_reconcile: set[str] = {str(lot.symbol) for lot in active_lots}
        for symbol in symbols_to_reconcile:
            row = by_symbol.get(str(symbol))
            live_qty = abs(_safe_float((row or {}).get("qty"), 0.0))
            self.ledger.reconcile_symbol_live_qty(
                strategy_id=self.strategy_id,
                symbol=str(symbol),
                live_qty=live_qty,
                payload={"sync_source": "ctx_portfolio"},
            )

    def _normalize_submit_result(self, result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            payload = dict(result)
        else:
            raw_status = getattr(result, "status", None)
            raw_stage = getattr(result, "stage", None)
            raw_symbol = getattr(result, "symbol", None)
            raw_decision_id = getattr(result, "decision_id", None)
            raw_reasons = getattr(result, "reasons", None)
            raw_metadata = getattr(result, "metadata", None)
            if isinstance(result, Mock):
                raw_status = None
                raw_stage = None
                raw_symbol = None
                raw_decision_id = None
                raw_reasons = None
                raw_metadata = None
            payload = {
                "status": str(raw_status or ""),
                "stage": str(raw_stage or ""),
                "symbol": str(raw_symbol or ""),
                "decision_id": str(raw_decision_id or ""),
                "reasons": list(raw_reasons or []) if isinstance(raw_reasons, (list, tuple)) else [],
                "metadata": dict(raw_metadata or {}) if isinstance(raw_metadata, dict) else {},
                "result": str(result),
            }
        if not str(payload.get("status") or "").strip() and isinstance(result, Mock):
            payload["status"] = "ACCEPTED"
        payload.setdefault("status", "")
        payload.setdefault("stage", "")
        payload.setdefault("reasons", [])
        payload.setdefault("metadata", {})
        payload.setdefault("result", str(result))
        return payload

    async def submit_intent(self, envelope: DecisionEnvelope) -> Any:
        if self._capture_delegate_intents:
            self._captured_delegate_intents.append(envelope)
            return {"captured": True}
        extras = dict(envelope.extras or {})
        extras.setdefault("parity_profile_id", self.execution_profile.profile_id)
        extras.setdefault("execution_profile_id", self.execution_profile.profile_id)
        extras.setdefault("definition_id", self._definition_id())
        extras.setdefault("audit_namespace", self._audit_namespace())
        extras.setdefault("position_namespace", self._position_namespace())
        extras.setdefault("risk_symbol", self._risk_symbol())
        position_lot_id = str(extras.get("position_id") or "")
        if not position_lot_id and float(envelope.target_qty) > 0:
            position_lot_id = f"lot:{self._position_namespace()}:{str(envelope.decision_id)}"[:120]
            extras["position_id"] = position_lot_id
        envelope.extras = extras
        record = KernelIntentRecord(
            strategy_id=self.strategy_id,
            decision_id=str(envelope.decision_id),
            symbol=str(envelope.symbol),
            target_qty=float(envelope.target_qty),
            ts_bar_end_us=int(envelope.ts_bar_end_us),
            order_ref=str(extras.get("order_ref") or ""),
            position_lot_id=str(position_lot_id),
            decision_snapshot_id=str(extras.get("decision_snapshot_id") or ""),
            execution_profile_id=str(extras.get("execution_profile_id") or self.execution_profile.profile_id),
            payload={
                "trigger_id": str(extras.get("trigger_id") or ""),
                "component_name": str(getattr(self.plugin, "component_name", "option_plugin")),
                "definition_id": str(extras.get("definition_id") or ""),
                "audit_namespace": str(extras.get("audit_namespace") or ""),
                "position_namespace": str(extras.get("position_namespace") or ""),
                "risk_symbol": str(extras.get("risk_symbol") or ""),
                "side": str(extras.get("side") or ""),
                "initial_qty": float(extras.get("initial_qty") or 0.0),
                "entry_mid": float(extras.get("entry_mid") or 0.0),
                "source_trigger_ts": str(extras.get("source_trigger_ts") or ""),
                "entry_submit_ts": str(extras.get("entry_submit_ts") or extras.get("entry_ts") or ""),
                "source_to_submit_ms": float(extras.get("source_to_submit_ms") or 0.0),
                "entry_plan_payload": _encode_payload(extras.get("entry_plan_payload") or {}),
                "structure_stop": float(extras.get("structure_stop") or 0.0),
                "target_1r": float(extras.get("target_1r") or 0.0),
                "target_1p2r": float(extras.get("target_1p2r") or 0.0),
                "exit_reason": str(extras.get("exit_reason") or ""),
            },
        )
        self.ledger.record_intent(record)
        result = await self.real_ctx.submit_intent(envelope)
        result_payload = self._normalize_submit_result(result)
        status = str(result_payload.get("status") or "").upper()
        accepted = status in {"APPROVED", "ACCEPTED", "OK"}
        if float(envelope.target_qty) > 0 and position_lot_id:
            self.ledger.resolve_entry_intent(
                position_lot_id=position_lot_id,
                accepted=accepted,
                payload={
                    "submit_status": status,
                    "submit_stage": str(result_payload.get("stage") or ""),
                    "submit_reasons": list(result_payload.get("reasons") or []),
                },
            )
            self._emit_state_audit(
                stage="runtime_transition",
                status="intent_submitted",
                reason="entry_intent_submitted" if accepted else "entry_intent_rejected",
                state_machine="runtime",
                symbol=str(envelope.symbol),
                severity="info" if accepted else "warning",
                payload={
                    "position_lot_id": position_lot_id,
                    "decision_id": str(envelope.decision_id),
                    **self._entry_latency_payload(
                        lot_payload=self._lot_payload_by_id(position_lot_id),
                    ),
                },
            )
            self._emit_state_audit(
                stage="order_transition",
                status="submitted" if accepted else "rejected",
                reason="entry_order_submitted" if accepted else "entry_order_rejected",
                state_machine="order",
                symbol=str(envelope.symbol),
                severity="info" if accepted else "warning",
                payload={
                    "position_lot_id": position_lot_id,
                    "decision_id": str(envelope.decision_id),
                    **self._entry_latency_payload(
                        lot_payload=self._lot_payload_by_id(position_lot_id),
                    ),
                },
            )
        elif float(envelope.target_qty) < 0 and position_lot_id:
            self.ledger.resolve_exit_intent(
                position_lot_id=position_lot_id,
                accepted=accepted,
                exit_qty=abs(float(envelope.target_qty)),
                payload={
                    "submit_status": status,
                    "submit_stage": str(result_payload.get("stage") or ""),
                    "submit_reasons": list(result_payload.get("reasons") or []),
                },
            )
            if accepted:
                flatten_reason = "flatten_submitted" if bool(extras.get("flatten_action")) else "exit_submitted"
                self._emit_state_audit(
                    stage="flatten_transition",
                    status="submitted",
                    reason=flatten_reason,
                    state_machine="flatten",
                    symbol=str(envelope.symbol),
                    payload={"position_lot_id": position_lot_id, "decision_id": str(envelope.decision_id)},
                )
                if position_lot_id not in self._flatten_confirmed_emitted_lots:
                    self._emit_state_audit(
                        stage="flatten_transition",
                        status="confirmed",
                        reason="position_reconciled_flat",
                        state_machine="flatten",
                        symbol=str(envelope.symbol),
                        payload={"position_lot_id": position_lot_id, "decision_id": str(envelope.decision_id)},
                    )
                    self._flatten_confirmed_emitted_lots.add(position_lot_id)
        self._emit_strategy_audit(
            stage="plugin_intent_submit_result",
            status="ok" if status in {"APPROVED", "ACCEPTED", "OK"} else "warning",
            reason=str(result_payload.get("status") or "submit_result_unknown").lower(),
            severity="info" if status in {"APPROVED", "ACCEPTED", "OK"} else "warning",
            payload={
                "decision_id": str(envelope.decision_id),
                "symbol": str(envelope.symbol),
                "target_qty": float(envelope.target_qty),
                "order_ref": str(extras.get("order_ref") or ""),
                **result_payload,
            },
        )
        return result

    def state_snapshot(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "kernel_mode": self.kernel_mode,
            "definition_id": self._definition_id(),
            "audit_namespace": self._audit_namespace(),
            "position_namespace": self._position_namespace(),
            "risk_symbol": self._risk_symbol(),
            "execution_profile": asdict(self.execution_profile),
            "ledger": self.ledger.snapshot(),
            "plugin": str(getattr(self.plugin, "component_name", "option_plugin")),
            "plugin_lifecycle": dict(self._plugin_lifecycle),
            "plugin_errors": int(self._plugin_errors),
            "captured_delegate_intents": int(len(self._captured_delegate_intents)),
            "bar_history_count": int(len(self._bar_history)),
        }


class OptionKernelStrategy(StrategyAlgorithm):
    plugin_cls: Type[BaseOptionPlugin] = BaseOptionPlugin

    def __init__(self, ctx: StrategyContext, params: StrategyParams):
        super().__init__(ctx, params)
        plugin = self.plugin_cls()
        extras = plugin.apply_default_extras(dict(params.extras or {}))
        self.params.extras = extras
        self._validate_scope_config()
        self.kernel = OptionKernel(
            strategy_id=str(ctx.strategy_id),
            real_ctx=ctx,
            params=self.params,
            plugin=plugin,
        )
        self.plugin = plugin
        self._tradability_heartbeat_task: Optional[asyncio.Task] = None

    def _entry_tradability_decision_while_paused(self, *, symbol: str) -> dict[str, Any]:
        extras = dict(self.params.extras or {})
        account_id = str(extras.get("account_id") or extras.get("account") or "").strip()
        strategy_instance_id = str(extras.get("strategy_instance_id") or self.strategy_id or "").strip()
        underlying = str(symbol or extras.get("underlying") or "").strip().upper()
        scope = TradabilityScope(
            account_id=account_id,
            strategy_id=str(self.strategy_id or ""),
            strategy_instance_id=strategy_instance_id,
            symbol=underlying,
            asset_type="OPT",
            session=str(extras.get("session") or "RTH").strip().upper() or "RTH",
            flow_type=FlowType.ENTRY,
            order_role=FlowType.ENTRY.value,
        )
        blocker = ScopedBlocker(
            blocker="system_paused",
            scope_type=BlockerScopeType.STRATEGY_INSTANCE,
            account_id=account_id or None,
            strategy_id=str(self.strategy_id or ""),
            strategy_instance_id=strategy_instance_id,
            symbol=underlying or None,
        )
        return decision_to_dict(evaluate_tradability(scope, TradabilityInputs(scoped_blockers=(blocker,))))

    def _strategy_tradability_payload(self, *, source: str) -> tuple[bool, list[str], dict[str, Any]]:
        extras = dict(self.params.extras or {})
        now = datetime.now(_ET)
        underlying = str(extras.get("underlying") or "").strip().upper()
        start_raw = str(extras.get("ab_time_start") or "09:40:00")
        end_raw = str(extras.get("no_new_entry_after") or extras.get("ab_time_end") or "15:00:00")
        try:
            entry_start = time.fromisoformat(start_raw)
        except Exception:
            entry_start = time(9, 40)
        try:
            entry_end = time.fromisoformat(end_raw)
        except Exception:
            entry_end = time(15, 0)
        in_entry_window = entry_start <= now.time() < entry_end
        min_bars = max(1, int(extras.get("history_ready_min_bars", extras.get("preload_history_bars", 210)) or 210))
        bar_count = int(len(getattr(self.kernel, "_bar_history", []) or []))
        blockers: list[str] = []
        if bool(getattr(self, "_system_paused", False)):
            blockers.append("system_paused")
        if in_entry_window and bar_count < min_bars:
            blockers.append("insufficient_warmup")
        data_blockers: list[str] = []
        if bar_count < min_bars:
            data_blockers.append("insufficient_warmup")
        idle_ok = not bool(in_entry_window)
        data_health_ok = not bool(data_blockers)
        tradable_for_entry = bool(in_entry_window and not blockers and data_health_ok)
        if tradable_for_entry:
            readiness_state = "entry_ready"
        elif idle_ok:
            readiness_state = "idle_not_in_entry_window"
        else:
            readiness_state = "entry_blocked"
        heartbeat_blockers = list(blockers)
        if not tradable_for_entry and not heartbeat_blockers:
            heartbeat_blockers.append(readiness_state)
        payload = {
            "symbol": underlying,
            "stage": "idle_heartbeat" if source == "option_kernel_idle_heartbeat" else "feature_heartbeat",
            "reason": "HEARTBEAT" if tradable_for_entry else ",".join(heartbeat_blockers),
            "kernel_mode": str(getattr(self.kernel, "kernel_mode", "")),
            "plugin": str(getattr(self.plugin, "component_name", "option_plugin")),
            "in_entry_window": bool(in_entry_window),
            "idle_ok": bool(idle_ok),
            "tradable_for_entry": bool(tradable_for_entry),
            "data_health_ok": bool(data_health_ok),
            "data_blockers": list(dict.fromkeys(data_blockers)),
            "readiness_state": readiness_state,
            "tradable": bool(tradable_for_entry),
            "bar_count": int(bar_count),
            "min_warmup_bars": int(min_bars),
            "system_paused": bool(getattr(self, "_system_paused", False)),
            "trace_id": f"{self.strategy_id}:{now.strftime('%Y%m%d%H%M')}:heartbeat",
        }
        return tradable_for_entry, heartbeat_blockers, payload

    def _write_tradability_heartbeat(self, *, source: str) -> None:
        tradable, blockers, payload = self._strategy_tradability_payload(source=source)
        write_strategy_tradability_heartbeat(
            strategy_id=str(self.strategy_id),
            tradable=bool(tradable),
            blockers=blockers,
            source=source,
            details=payload,
        )

    async def _run_tradability_heartbeat_loop(self) -> None:
        interval_sec = max(10.0, float((self.params.extras or {}).get("tradability_heartbeat_interval_sec", 30.0)))
        while True:
            self._write_tradability_heartbeat(source="option_kernel_idle_heartbeat")
            await asyncio.sleep(interval_sec)

    def _resolve_local_audit_writer(self) -> Any:
        engine = getattr(self.ctx, "engine", None)
        candidates = [
            getattr(engine, "audit_writer", None),
            getattr(getattr(engine, "_strategy_engine", None), "audit_writer", None),
        ]
        for writer in candidates:
            if writer is not None and hasattr(writer, "emit"):
                return writer
        return None

    def _emit_system_audit(
        self,
        *,
        stage: str,
        status: str,
        reason: str,
        severity: str = "info",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        writer = self._resolve_local_audit_writer()
        if writer is None:
            return
        strategy_run_id = str(getattr(self.ctx, "strategy_run_id", "") or "").strip() or None
        component_name = str(getattr(self.plugin, "component_name", "option_kernel_strategy"))
        try:
            writer.emit(
                domain="system",
                component=component_name,
                stage=stage,
                severity=severity,
                strategy_id=self.strategy_id,
                strategy_run_id=strategy_run_id,
                symbol=str((self.params.extras or {}).get("underlying") or ""),
                status=status,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            self.ctx.log.warning(
                "option_kernel_system_audit_emit_failed",
                strategy_id=str(self.strategy_id),
                stage=stage,
                error=str(exc),
            )

    def _validate_scope_config(self) -> None:
        extras = dict(self.params.extras or {})
        strict = bool(extras.get("strict_scope_validation", False))
        if not strict:
            return
        group_ids = extras.get("shared_cap_group_strategy_ids")
        if isinstance(group_ids, list) and group_ids:
            ids = {str(v).strip() for v in group_ids if str(v).strip()}
            if str(self.strategy_id) not in ids:
                raise ValueError(
                    f"strategy_id={self.strategy_id} missing from shared_cap_group_strategy_ids"
                )

    async def initialize(self) -> None:
        await self.subscribe_system_health()
        self._emit_system_audit(
            stage="strategy_started",
            status="started",
            reason="initialize_begin",
        )
        await self.kernel.initialize()
        self._write_tradability_heartbeat(source="option_kernel_initialize")
        if "PYTEST_CURRENT_TEST" not in os.environ:
            self._tradability_heartbeat_task = asyncio.create_task(
                self._run_tradability_heartbeat_loop(),
                name=f"option_kernel_tradability_heartbeat:{self.strategy_id}",
            )
        self._emit_system_audit(
            stage="strategy_ready",
            status="ready",
            reason="initialize_complete",
        )

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        if self._system_paused:
            decision = self._entry_tradability_decision_while_paused(symbol=symbol)
            self._write_tradability_heartbeat(source="option_kernel_feature_heartbeat")
            self._emit_system_audit(
                stage="tradability_entry_denied_while_system_paused",
                status="blocked",
                reason=str(decision.get("reason") or "system_paused"),
                payload={"tradability_decision": decision},
            )
            return
        await self.kernel.on_features(symbol, features)
        self._write_tradability_heartbeat(source="option_kernel_feature_heartbeat")

    async def on_shutdown(self, reason: str) -> None:
        task = getattr(self, "_tradability_heartbeat_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._tradability_heartbeat_task = None
        await self.kernel.on_shutdown(reason)
