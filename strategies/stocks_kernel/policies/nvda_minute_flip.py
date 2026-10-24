from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from strategies.stocks_kernel.policies.base import Policy
from strategies.stocks_kernel.types import ActionPlan, ActionType, EntryPlan, FeatureContext, GateEvaluationReport, SignalIntent


def _float_param(params: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except Exception:
        return float(default)


def _int_param(params: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(params.get(key, default))
    except Exception:
        return int(default)


@dataclass
class _FlipDecision:
    bucket: int
    action: str
    side: str | None
    trigger_id: str


class NVDAMinuteFlipPolicy(Policy):
    """Paper/live-path NVDA churn policy for after-hours system testing.

    The policy intentionally alternates between one long entry minute and one
    flatten minute. It is stateful per process so a 5s feature stream does not
    submit repeatedly within the same minute bucket.
    """

    policy_id = "nvda_minute_flip_policy"

    def __init__(self, policy_params: dict[str, Any] | None = None, extras: dict[str, Any] | None = None) -> None:
        params = {**dict(policy_params or {}), **dict(extras or {}).get("nvda_minute_flip", {})}
        self.symbol = str(params.get("symbol") or dict(extras or {}).get("signal_symbol") or "NVDA").strip().upper() or "NVDA"
        self.qty = max(1.0, _float_param(params, "qty", 1.0))
        self.interval_sec = max(30, _int_param(params, "trade_interval_sec", 60))
        self.max_cycles_per_day = max(0, _int_param(params, "max_cycles_per_day", 0))
        self.exit_limit_offset_bp = max(0.0, _float_param(params, "exit_limit_offset_bp", 1000.0))
        self.start_with_entry = bool(params.get("start_with_entry", True))
        self._emitted: set[tuple[str, int]] = set()
        self._day_counts: dict[str, int] = {}

    def build_signal(self, *, symbol: str, feature_context: FeatureContext, gate_report: GateEvaluationReport) -> SignalIntent:
        decision = self._decision(feature_context=feature_context, gate_report=gate_report)
        if decision is None:
            return SignalIntent(
                action="hold",
                side=None,
                symbol=self.symbol,
                payload={"reason": "nvda_minute_flip_not_ready"},
            )
        return SignalIntent(
            action=decision.action,
            side=decision.side,
            symbol=self.symbol,
            confidence=1.0,
            payload={
                "reason": "nvda_minute_flip",
                "bucket": decision.bucket,
                "trigger_id": decision.trigger_id,
                "test_strategy": True,
                "paper_test": True,
            },
        )

    def build_entry_plan(self, *, signal: SignalIntent, feature_context: FeatureContext) -> EntryPlan | None:
        if str(signal.action or "").strip().lower() != "enter" or str(signal.side or "").strip().upper() != "LONG":
            return None
        trigger_id = str(signal.payload.get("trigger_id") or "").strip()
        return EntryPlan(
            symbol=self.symbol,
            side="LONG",
            qty=float(self.qty),
            lane_id="nvda_long_exec",
            ownership_scope="strategy",
            trigger_id=trigger_id or None,
            payload={
                "reason": "nvda_minute_flip_entry",
                "test_strategy": True,
                "paper_test": True,
                "action_type": ActionType.ENTRY,
            },
        )

    def build_action_plans(self, *, signal: SignalIntent, feature_context: FeatureContext) -> list[ActionPlan]:
        action = str(signal.action or "").strip().lower()
        bucket = int(signal.payload.get("bucket") or 0)
        trigger_id = str(signal.payload.get("trigger_id") or "").strip()
        if bucket <= 0 or not trigger_id:
            return []
        emit_key = (action, bucket)
        if emit_key in self._emitted:
            return []
        day_key = self._day_key(feature_context=feature_context)
        if self.max_cycles_per_day > 0 and self._day_counts.get(day_key, 0) >= self.max_cycles_per_day:
            return []
        if action == "enter" and str(signal.side or "").strip().upper() == "LONG":
            self._mark_emitted(day_key=day_key, emit_key=emit_key)
            return [
                ActionPlan(
                    action_type=ActionType.ENTRY,
                    symbol=self.symbol,
                    side="LONG",
                    qty=float(self.qty),
                    priority=10,
                    lane_id="nvda_long_exec",
                    ownership_scope="strategy",
                    trigger_id=trigger_id,
                    reason="nvda_minute_flip_entry",
                    payload={
                        "reason": "nvda_minute_flip_entry",
                        "test_strategy": True,
                        "paper_test": True,
                    },
                )
            ]
        if action == "flatten":
            self._mark_emitted(day_key=day_key, emit_key=emit_key)
            return [
                ActionPlan(
                    action_type=ActionType.FLATTEN,
                    symbol=self.symbol,
                    qty=float(self.qty),
                    priority=5,
                    lane_id="nvda_long_exec",
                    ownership_scope="strategy",
                    close_only=True,
                    flatten_action=True,
                    reduce_only=True,
                    trigger_id=trigger_id,
                    reason="nvda_minute_flip_flatten",
                    payload={
                        "reason": "nvda_minute_flip_flatten",
                        "test_strategy": True,
                        "paper_test": True,
                        "force_order_style": True,
                        "exit_order_type": "LMT",
                        "exit_limit_offset_bp": self.exit_limit_offset_bp,
                        "allow_outside_rth": True,
                        "suppress_manual_intervention_lock": True,
                    },
                )
            ]
        return []

    def _decision(self, *, feature_context: FeatureContext, gate_report: GateEvaluationReport) -> _FlipDecision | None:
        if not bool(getattr(gate_report, "allowed", True)):
            return None
        ts_epoch_s = float(getattr(getattr(feature_context, "snapshot", None), "ts_epoch_s", 0.0) or 0.0)
        if ts_epoch_s <= 0.0:
            return None
        bucket = int(ts_epoch_s // float(self.interval_sec))
        entry_phase = (bucket % 2 == 0) if self.start_with_entry else (bucket % 2 == 1)
        action = "enter" if entry_phase else "flatten"
        side = "LONG" if action == "enter" else None
        return _FlipDecision(
            bucket=bucket,
            action=action,
            side=side,
            trigger_id=f"nvda_minute_flip:{bucket}:{action}",
        )

    def _mark_emitted(self, *, day_key: str, emit_key: tuple[str, int]) -> None:
        self._emitted.add(emit_key)
        self._day_counts[day_key] = int(self._day_counts.get(day_key, 0)) + 1

    @staticmethod
    def _day_key(*, feature_context: FeatureContext) -> str:
        ts_epoch_s = float(getattr(getattr(feature_context, "snapshot", None), "ts_epoch_s", 0.0) or 0.0)
        if ts_epoch_s <= 0.0:
            return "unknown"
        return str(int(ts_epoch_s // 86400))


def build_nvda_minute_flip_policy(policy_params: dict[str, Any] | None = None, extras: dict[str, Any] | None = None) -> NVDAMinuteFlipPolicy:
    return NVDAMinuteFlipPolicy(policy_params=policy_params, extras=extras)
