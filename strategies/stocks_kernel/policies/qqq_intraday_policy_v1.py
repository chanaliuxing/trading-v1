from __future__ import annotations

from datetime import datetime
from typing import Any

from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.qqq_intraday_rules import QQQIntradayRuleEngine, load_stock_entry_recipe
from strategies.stocks_kernel.types import EntryPlan, ExitPlan, FeatureContext, GateEvaluationReport, SignalIntent

from .base import EntryPermissionDecision, Policy


class QQQIntradayPolicyV1(Policy):
    policy_id = "qqq_intraday_policy_v1"

    def __init__(
        self,
        *,
        max_trades_per_day: int = 3,
        hard_stop_atr_mult: float = 0.75,
        logic_retreat_min_hold_sec: int = 360,
        third_trade_requires_positive_day_pnl: bool = True,
        block_when_pending_entry: bool = True,
        block_when_same_strategy_pending_orders: bool = True,
        block_when_order_lifecycle_pending: bool = True,
        rule_engine_parity: bool = False,
        entry_model: str = "baseline",
        qqew_decoupled: bool = False,
        entry_recipe: dict[str, Any] | None = None,
        v3m_units: str = "auto",
        runtime_strategy_id: str | None = None,
        static_extras: dict[str, Any] | None = None,
    ) -> None:
        self.max_trades_per_day = int(max_trades_per_day)
        self.hard_stop_atr_mult = float(hard_stop_atr_mult)
        self.logic_retreat_min_hold_sec = int(logic_retreat_min_hold_sec)
        self.third_trade_requires_positive_day_pnl = bool(third_trade_requires_positive_day_pnl)
        self.block_when_pending_entry = bool(block_when_pending_entry)
        self.block_when_same_strategy_pending_orders = bool(block_when_same_strategy_pending_orders)
        self.block_when_order_lifecycle_pending = bool(block_when_order_lifecycle_pending)
        self.rule_engine_parity = bool(rule_engine_parity)
        self.entry_model = str(entry_model or "baseline").strip().lower() or "baseline"
        self.qqew_decoupled = bool(qqew_decoupled)
        self.entry_recipe = dict(entry_recipe or {})
        self.v3m_units = str(v3m_units or "auto").strip().lower() or "auto"
        self.runtime_strategy_id = str(runtime_strategy_id or self.policy_id).strip() or self.policy_id
        self.static_extras = dict(static_extras or {})
        self._rule_engine = QQQIntradayRuleEngine()

    def build_signal(self, *, symbol: str, feature_context: FeatureContext, gate_report: GateEvaluationReport) -> SignalIntent:
        if not gate_report.allowed:
            return SignalIntent(action="hold", side=None, symbol=symbol, payload={"blocked_by": gate_report.blocked_by})
        if self.rule_engine_parity:
            parity_signal = self._build_rule_engine_signal(symbol=symbol, feature_context=feature_context)
            if parity_signal is not None:
                return parity_signal
        v3m = feature_context.values.get("v3m")
        atr_eff = feature_context.values.get("atr_eff")
        if v3m is None or atr_eff is None:
            return SignalIntent(action="hold", side=None, symbol=symbol, payload={"reason": "feature_missing"})
        if float(v3m) > 0:
            return SignalIntent(action="enter", side="LONG", symbol=symbol, payload={"v3m": float(v3m), "atr_eff": float(atr_eff)})
        if float(v3m) < 0:
            return SignalIntent(action="enter", side="SHORT", symbol=symbol, payload={"v3m": float(v3m), "atr_eff": float(atr_eff)})
        return SignalIntent(action="hold", side=None, symbol=symbol, payload={"reason": "flat_signal"})

    def build_entry_plan(self, *, signal: SignalIntent, feature_context: FeatureContext) -> EntryPlan | None:
        if signal.action != "enter" or signal.side not in {"LONG", "SHORT"}:
            return None
        qty = float(feature_context.snapshot.extras.get("planned_qty") or 1.0)
        return EntryPlan(
            symbol=signal.symbol,
            side=signal.side,
            qty=qty,
            trigger_id=str(feature_context.snapshot.extras.get("trigger_id") or "") or None,
            payload={
                "hard_stop_atr_mult": self.hard_stop_atr_mult,
                "logic_retreat_min_hold_sec": self.logic_retreat_min_hold_sec,
                "max_trades_per_day": self.max_trades_per_day,
                "third_trade_requires_positive_day_pnl": self.third_trade_requires_positive_day_pnl,
                "block_when_pending_entry": self.block_when_pending_entry,
                "block_when_same_strategy_pending_orders": self.block_when_same_strategy_pending_orders,
                "block_when_order_lifecycle_pending": self.block_when_order_lifecycle_pending,
            },
        )

    def build_stop_plan(
        self,
        *,
        signal: SignalIntent,
        entry_plan: EntryPlan | None,
        feature_context: FeatureContext,
    ) -> ExitPlan | None:
        if signal.action != "enter" or entry_plan is None or signal.side not in {"LONG", "SHORT"}:
            return None
        atr_eff = feature_context.values.get("atr_eff")
        if atr_eff is None:
            return None
        return ExitPlan(
            symbol=entry_plan.symbol,
            order_role="STOP",
            qty=entry_plan.qty,
            payload={
                "side": signal.side,
                "atr_eff": float(atr_eff),
                "hard_stop_atr_mult": self.hard_stop_atr_mult,
                "logic_stop_offset": float(atr_eff) * self.hard_stop_atr_mult,
            },
        )

    def build_take_profit_plan(
        self,
        *,
        signal: SignalIntent,
        entry_plan: EntryPlan | None,
        feature_context: FeatureContext,
    ) -> ExitPlan | None:
        if signal.action != "enter" or entry_plan is None or signal.side not in {"LONG", "SHORT"}:
            return None
        atr_eff = feature_context.values.get("atr_eff")
        if atr_eff is None:
            return None
        return ExitPlan(
            symbol=entry_plan.symbol,
            order_role="TAKE_PROFIT",
            qty=entry_plan.qty,
            payload={
                "side": signal.side,
                "atr_eff": float(atr_eff),
                "target_offset_atr": 1.0,
            },
        )

    def build_retreat_plan(
        self,
        *,
        signal: SignalIntent,
        feature_context: FeatureContext,
    ) -> ExitPlan | None:
        if signal.action != "enter" or signal.side not in {"LONG", "SHORT"}:
            return None
        qty = float(feature_context.snapshot.extras.get("planned_qty") or 1.0)
        return ExitPlan(
            symbol=signal.symbol,
            order_role="RETREAT",
            qty=qty,
            payload={"side": signal.side, "logic_retreat_min_hold_sec": self.logic_retreat_min_hold_sec},
        )

    def build_exit_plans(self, *, feature_context: FeatureContext) -> list[ExitPlan]:
        return []

    def on_fill_feedback(
        self,
        *,
        position_lot_id: str,
        order_role: str,
        filled_qty: float,
        fill_price: float | None,
        feature_context: FeatureContext | None = None,
    ) -> dict[str, object] | None:
        return {
            "position_lot_id": str(position_lot_id or ""),
            "order_role": str(order_role or "").strip().upper(),
            "filled_qty": float(filled_qty or 0.0),
            "fill_price": float(fill_price) if fill_price is not None else None,
        }

    def evaluate_entry_permission(
        self,
        *,
        signal: SignalIntent,
        feature_context: FeatureContext,
        runtime_state: dict[str, Any],
    ) -> EntryPermissionDecision:
        if signal.action != "enter" or str(signal.side or "").strip().upper() not in {"LONG", "SHORT"}:
            return EntryPermissionDecision(allowed=True)
        try:
            success_count = int(runtime_state.get("success_count") or 0)
        except Exception:
            success_count = 0
        try:
            day_realized_pnl = float(runtime_state.get("day_realized_pnl") or 0.0)
        except Exception:
            day_realized_pnl = 0.0
        try:
            pending_entry_count = int(runtime_state.get("pending_entry_count") or 0)
        except Exception:
            pending_entry_count = 0
        try:
            same_strategy_pending_orders = int(runtime_state.get("same_strategy_pending_orders") or 0)
        except Exception:
            same_strategy_pending_orders = 0
        try:
            order_lifecycle_pending_count = int(runtime_state.get("order_lifecycle_pending_count") or 0)
        except Exception:
            order_lifecycle_pending_count = 0
        order_lifecycle_pending_refs = tuple(
            str(item or "").strip()
            for item in tuple(runtime_state.get("order_lifecycle_pending_refs") or ())
            if str(item or "").strip()
        )
        if self.block_when_order_lifecycle_pending and order_lifecycle_pending_count > 0:
            return EntryPermissionDecision(
                allowed=False,
                reason="order_lifecycle_pending",
                payload={
                    "order_lifecycle_pending_count": int(order_lifecycle_pending_count),
                    "order_lifecycle_pending_refs": list(order_lifecycle_pending_refs),
                },
            )
        pending_entry_symbols = tuple(
            str(item or "").strip().upper()
            for item in tuple(runtime_state.get("pending_entry_symbols") or ())
            if str(item or "").strip()
        )
        if self.block_when_same_strategy_pending_orders and same_strategy_pending_orders > 0:
            return EntryPermissionDecision(
                allowed=False,
                reason="same_strategy_pending_orders",
                payload={
                    "same_strategy_pending_orders": int(same_strategy_pending_orders),
                    "pending_entry_count": pending_entry_count,
                    "pending_entry_symbols": list(pending_entry_symbols),
                },
            )
        if self.block_when_pending_entry and pending_entry_count > 0:
            return EntryPermissionDecision(
                allowed=False,
                reason="pending_entry_inflight",
                payload={
                    "pending_entry_count": pending_entry_count,
                    "pending_entry_symbols": list(pending_entry_symbols),
                },
            )
        if success_count >= max(1, self.max_trades_per_day):
            return EntryPermissionDecision(
                allowed=False,
                reason="max_trades_per_day",
                payload={
                    "success_count": success_count,
                    "max_trades_per_day": self.max_trades_per_day,
                    "day_realized_pnl": day_realized_pnl,
                },
            )
        if self.third_trade_requires_positive_day_pnl and success_count >= 2 and day_realized_pnl <= 0.0:
            return EntryPermissionDecision(
                allowed=False,
                reason="third_trade_requires_positive_day_pnl",
                payload={
                    "success_count": success_count,
                    "day_realized_pnl": day_realized_pnl,
                    "max_trades_per_day": self.max_trades_per_day,
                },
            )
        return EntryPermissionDecision(allowed=True)

    def _build_rule_engine_signal(self, *, symbol: str, feature_context: FeatureContext) -> SignalIntent | None:
        snapshot = feature_context.snapshot
        values = dict(feature_context.values or {})
        extras = dict(getattr(snapshot, "extras", {}) or {})
        merged_extras = {**dict(self.static_extras or {}), **extras}
        values = self._inject_multi_scope_aliases(
            values=values,
            feature_context=feature_context,
            merged_extras=merged_extras,
        )
        values = self._overlay_runtime_feature_extras(values=values, merged_extras=merged_extras)
        merged_extras.setdefault("entry_model", self.entry_model)
        merged_extras.setdefault("qqew_decoupled", self.qqew_decoupled)
        if self.entry_recipe and not isinstance(merged_extras.get("entry_recipe"), dict):
            merged_extras["entry_recipe"] = dict(self.entry_recipe)
        try:
            ts_epoch_s = float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0)
        except Exception:
            ts_epoch_s = 0.0
        now = datetime.fromtimestamp(ts_epoch_s) if ts_epoch_s > 0 else datetime.utcnow()
        try:
            spot = float(getattr(snapshot, "last_price", 0.0) or 0.0)
        except Exception:
            spot = 0.0
        if spot <= 0.0:
            return SignalIntent(action="hold", side=None, symbol=symbol, payload={"reason": "feature_missing"})
        values, v3m_transform_payload = self._normalize_v3m_for_rule_engine(values=values, spot=spot)
        try:
            atr_eff = float(values.get("atr_eff") or 0.0)
        except Exception:
            atr_eff = 0.0
        runtime = PluginRuntimeState(
            strategy_id=self.runtime_strategy_id,
            kernel_mode="stocks_kernel",
            symbol=str(symbol or "").strip().upper(),
            now=now,
            wall_now=now,
            spot=spot,
            bar=KernelBarContext(
                ts=now,
                symbol=str(symbol or "").strip().upper(),
                open=float(getattr(snapshot, "minute_open", None) or spot),
                high=float(getattr(snapshot, "minute_high", None) or spot),
                low=float(getattr(snapshot, "minute_low", None) or spot),
                close=float(getattr(snapshot, "minute_close", None) or spot),
                volume=float(getattr(snapshot, "minute_volume", None) or 0.0),
                vwap=float(getattr(snapshot, "vwap", None) or spot),
                atr=atr_eff,
            ),
            features=values,
            payload={"bar_history": self._bar_history_payload(feature_context=feature_context)},
        )
        result = self._rule_engine.evaluate(
            runtime=runtime,
            extras=merged_extras,
            recipe=load_stock_entry_recipe(merged_extras),
        )
        side = str(result.translation.stock_side or "").strip().upper()
        if bool(result.translation.allowed) and side in {"LONG", "SHORT"}:
            return SignalIntent(
                action="enter",
                side=side,
                symbol=symbol,
                payload={
                    "reason": str(result.translation.reason or ""),
                    "rule_engine_parity": True,
                    **dict(v3m_transform_payload or {}),
                    **dict(result.translation.payload or {}),
                },
            )
        return SignalIntent(
            action="hold",
            side=None,
            symbol=symbol,
            payload={
                "reason": str(result.translation.reason or "blocked"),
                "rule_engine_parity": True,
                **dict(v3m_transform_payload or {}),
                **dict(result.translation.payload or {}),
            },
        )

    def _normalize_v3m_for_rule_engine(self, *, values: dict[str, Any], spot: float) -> tuple[dict[str, Any], dict[str, Any]]:
        out = dict(values or {})
        raw = out.get("v3m_signed", out.get("v3m"))
        if raw is None:
            return out, {"v3m_units_mode": self.v3m_units, "v3m_units_converted": False}
        try:
            v3m_value = float(raw)
        except Exception:
            return out, {"v3m_units_mode": self.v3m_units, "v3m_units_converted": False}
        mode = self.v3m_units
        converted = False
        if mode == "ratio":
            v3m_value = float(v3m_value) * float(spot)
            converted = True
        elif mode == "auto":
            # Heuristic for mixed upstream schemas: ratio-shaped values are tiny in absolute
            # terms and should be converted to price delta for QQQ parity rule logic.
            if abs(float(v3m_value)) <= 0.05 and float(spot) > 0.0:
                v3m_value = float(v3m_value) * float(spot)
                converted = True
        out["v3m"] = float(v3m_value)
        out["v3m_signed"] = float(v3m_value)
        return out, {"v3m_units_mode": mode, "v3m_units_converted": converted}

    @staticmethod
    def _overlay_runtime_feature_extras(*, values: dict[str, Any], merged_extras: dict[str, Any]) -> dict[str, Any]:
        out = dict(values or {})
        for key in (
            "v3m",
            "v3m_signed",
            "atr_eff",
            "atr_live",
            "atr_seed",
            "atr_cap",
            "vol_surge_ratio",
            "volume_surge_ratio",
            "distance_from_vwap_atr",
            "data_age_qqq_sec",
            "data_age_qqew_sec",
            "continuity_180s_ok",
            "qqq_now",
            "vwap_now",
            "pos_day",
            "day_position_rank",
        ):
            value = merged_extras.get(key)
            if value is not None:
                out[key] = value
        if out.get("day_position_rank") is None and out.get("pos_day") is not None:
            out["day_position_rank"] = out.get("pos_day")
        return out

    @staticmethod
    def _bar_history_payload(*, feature_context: FeatureContext) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bar in tuple(getattr(feature_context.history, "bars_1m", ()) or ()):
            try:
                ts_epoch_s = float(getattr(bar, "ts_epoch_s", 0.0) or 0.0)
                close = float(getattr(bar, "close", 0.0) or 0.0)
            except Exception:
                continue
            if ts_epoch_s <= 0.0 or close <= 0.0:
                continue
            rows.append(
                {
                    "ts": datetime.fromtimestamp(ts_epoch_s),
                    "close": close,
                    "volume": float(getattr(bar, "volume", 0.0) or 0.0),
                    "vwap": close,
                }
            )
        return rows

    @staticmethod
    def _inject_multi_scope_aliases(
        *,
        values: dict[str, Any],
        feature_context: FeatureContext,
        merged_extras: dict[str, Any],
    ) -> dict[str, Any]:
        out = dict(values or {})
        bundle = out.get("__data_context_bundle__")
        if bundle is None:
            return out
        try:
            confirm_candidates = list((bundle.scopes().get("confirm") or {}).values())
        except Exception:
            confirm_candidates = []
        if not confirm_candidates:
            return out
        configured_confirm_symbol = str(merged_extras.get("confirm_symbol") or "").strip().upper()
        confirm_ctx = None
        if configured_confirm_symbol:
            confirm_ctx = next(
                (item for item in confirm_candidates if str(getattr(item, "symbol", "") or "").strip().upper() == configured_confirm_symbol),
                None,
            )
        if confirm_ctx is None:
            confirm_ctx = confirm_candidates[0]
        snapshot = getattr(confirm_ctx, "snapshot", None)
        history = getattr(confirm_ctx, "history", None)
        if snapshot is None:
            return out
        confirm_symbol = str(getattr(confirm_ctx, "symbol", "") or "").strip().upper()
        confirm_prefix = str(merged_extras.get("confirm_feature_prefix") or confirm_symbol.lower() or "qqew").strip().lower()
        signal_ts = float(getattr(getattr(feature_context, "snapshot", None), "ts_epoch_s", 0.0) or 0.0)
        confirm_ts = float(getattr(snapshot, "ts_epoch_s", 0.0) or 0.0)
        data_age_confirm_sec = None
        if signal_ts > 0.0 and confirm_ts > 0.0:
            data_age_confirm_sec = max(0.0, signal_ts - confirm_ts)
        confirm_now = getattr(snapshot, "last_price", None)
        if confirm_now is None:
            confirm_now = getattr(snapshot, "minute_close", None)
        confirm_vwap = getattr(snapshot, "vwap", None)
        confirm_ago = None
        bars = tuple(getattr(history, "bars_1m", ()) or ()) if history is not None else ()
        if bars:
            current_minute = datetime.fromtimestamp(signal_ts).replace(second=0, microsecond=0) if signal_ts > 0.0 else None
            target_ts = None if current_minute is None else current_minute.timestamp() - 180.0
            for bar in reversed(bars):
                try:
                    bar_ts = float(getattr(bar, "ts_epoch_s", 0.0) or 0.0)
                    bar_close = float(getattr(bar, "close", 0.0) or 0.0)
                except Exception:
                    continue
                if bar_close <= 0.0:
                    continue
                if target_ts is None or bar_ts <= target_ts:
                    confirm_ago = bar_close
                    break
        for key, value in (
            ("confirm_symbol", confirm_symbol),
            ("confirm_feature_prefix", confirm_prefix),
            ("confirm_now", confirm_now),
            ("confirm_close_now", confirm_now),
            ("confirm_vwap_now", confirm_vwap),
            ("confirm_vwap", confirm_vwap),
            ("confirm_ago", confirm_ago),
            ("data_age_confirm_sec", data_age_confirm_sec),
            (f"{confirm_prefix}_now", confirm_now),
            (f"{confirm_prefix}_close_now", confirm_now),
            (f"{confirm_prefix}_vwap_now", confirm_vwap),
            (f"{confirm_prefix}_vwap", confirm_vwap),
            (f"{confirm_prefix}_ago", confirm_ago),
            (f"data_age_{confirm_prefix}_sec", data_age_confirm_sec),
        ):
            if value is not None:
                out.setdefault(key, value)
                merged_extras.setdefault(key, value)
        merged_extras.setdefault("confirm_symbol", confirm_symbol)
        merged_extras.setdefault("confirm_feature_prefix", confirm_prefix)
        return out


class SPYIntradayPolicyV1(QQQIntradayPolicyV1):
    policy_id = "spy_intraday_policy_v1"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("runtime_strategy_id", self.policy_id)
        super().__init__(**kwargs)
