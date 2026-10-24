from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strategies.stocks_kernel.policies.base import EntryPermissionDecision, Policy
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


@dataclass(frozen=True)
class AgentPaperSignal:
    signal_id: str
    symbol: str
    action: str
    side: str | None
    qty: float
    ts_epoch_s: float
    max_age_sec: float
    reason: str
    payload: dict[str, Any]


class AgentPaperBridgePolicy(Policy):
    """File-backed bridge from an external agent signal into stocks_kernel.

    The agent is intentionally limited to writing signal records. This policy
    converts the newest valid record into normal kernel action plans, so paper
    orders still pass through the existing validator, execution service, OMS,
    broker truth, and reconciliation layers.
    """

    policy_id = "agent_paper_bridge_policy"

    def __init__(self, policy_params: dict[str, Any] | None = None, extras: dict[str, Any] | None = None) -> None:
        params = {**dict(policy_params or {}), **dict(extras or {}).get("agent_paper_bridge", {})}
        self.symbol = str(params.get("symbol") or dict(extras or {}).get("signal_symbol") or "NVDA").strip().upper() or "NVDA"
        self.signal_path = Path(
            str(params.get("signal_path") or "runtime/agent_signals/agent_paper_signal.jsonl")
        )
        self.state_path = Path(str(params.get("state_path") or "runtime/state/agent_paper_bridge_emitted.json"))
        self.default_qty = max(1.0, _float_param(params, "qty", 1.0))
        self.max_qty = max(1.0, _float_param(params, "max_qty", self.default_qty))
        self.max_flatten_qty = max(self.max_qty, _float_param(params, "max_flatten_qty", 100.0))
        self.max_signal_age_sec = max(1.0, _float_param(params, "max_signal_age_sec", 120.0))
        self.max_trades_per_day = max(0, _int_param(params, "max_trades_per_day", 20))
        self.long_lane_id = str(params.get("long_lane_id") or "agent_long_exec").strip() or "agent_long_exec"
        self.short_lane_id = str(params.get("short_lane_id") or "agent_short_exec").strip() or "agent_short_exec"
        self.require_paper_ack = bool(params.get("paper_operator_ack", True))
        self._emitted_signal_ids: set[str] = self._load_emitted_signal_ids()
        self._day_counts: dict[str, int] = {}

    def build_signal(self, *, symbol: str, feature_context: FeatureContext, gate_report: GateEvaluationReport) -> SignalIntent:
        if not bool(getattr(gate_report, "allowed", True)):
            return SignalIntent(
                action="hold",
                side=None,
                symbol=self.symbol,
                payload={"reason": "gate_blocked", "blocked_by": getattr(gate_report, "blocked_by", None)},
            )
        signal = self._latest_signal(feature_context=feature_context)
        if signal is None:
            return SignalIntent(
                action="hold",
                side=None,
                symbol=self.symbol,
                payload={
                    "reason": "agent_signal_missing_or_stale",
                    "signal_path": str(self.signal_path),
                    "paper_test": True,
                    "agent_bridge": True,
                },
            )
        return SignalIntent(
            action=signal.action,
            side=signal.side,
            symbol=signal.symbol,
            confidence=1.0,
            payload={
                **signal.payload,
                "reason": signal.reason,
                "signal_id": signal.signal_id,
                "signal_path": str(self.signal_path),
                "agent_bridge": True,
                "paper_test": True,
                "test_strategy": True,
            },
        )

    def build_entry_plan(self, *, signal: SignalIntent, feature_context: FeatureContext) -> EntryPlan | None:
        action = str(signal.action or "").strip().lower()
        side = str(signal.side or "").strip().upper()
        if action != "enter" or side not in {"LONG", "SHORT"}:
            return None
        signal_id = str(signal.payload.get("signal_id") or "").strip()
        if signal_id and signal_id in self._emitted_signal_ids:
            return None
        qty = self._plan_qty(signal=signal)
        return EntryPlan(
            symbol=self.symbol,
            side=side,
            qty=qty,
            lane_id=self.long_lane_id if side == "LONG" else self.short_lane_id,
            ownership_scope="strategy",
            trigger_id=f"agent_paper:{signal_id}" if signal_id else None,
            payload={
                "reason": "agent_paper_entry",
                "agent_bridge": True,
                "paper_test": True,
                "test_strategy": True,
                "signal_id": signal_id,
                "action_type": ActionType.ENTRY,
                "absolute_signal_qty": True,
                "skip_trade_budget_sizing": True,
            },
        )

    def build_action_plans(self, *, signal: SignalIntent, feature_context: FeatureContext) -> list[ActionPlan]:
        signal_id = str(signal.payload.get("signal_id") or "").strip()
        if not signal_id or signal_id in self._emitted_signal_ids:
            return []
        day_key = self._day_key(feature_context=feature_context)
        if self.max_trades_per_day > 0 and self._day_counts.get(day_key, 0) >= self.max_trades_per_day:
            return []
        action = str(signal.action or "").strip().lower()
        side = str(signal.side or "").strip().upper()
        trigger_id = f"agent_paper:{signal_id}"
        if action == "enter" and side in {"LONG", "SHORT"}:
            self._mark_emitted(day_key=day_key, signal_id=signal_id)
            return [
                ActionPlan(
                    action_type=ActionType.ENTRY,
                    symbol=self.symbol,
                    side=side,
                    qty=self._plan_qty(signal=signal),
                    priority=10,
                    lane_id=self.long_lane_id if side == "LONG" else self.short_lane_id,
                    ownership_scope="strategy",
                    trigger_id=trigger_id,
                    reason="agent_paper_entry",
                    payload={
                        "reason": "agent_paper_entry",
                        "agent_bridge": True,
                        "paper_test": True,
                        "paper_smoke_only": True,
                        "test_strategy": True,
                        "allow_system_health_degraded_entry": True,
                        "absolute_signal_qty": True,
                        "skip_trade_budget_sizing": True,
                        "signal_id": signal_id,
                    },
                )
            ]
        if action == "flatten":
            self._mark_emitted(day_key=day_key, signal_id=signal_id)
            return [
                ActionPlan(
                    action_type=ActionType.FLATTEN,
                    symbol=self.symbol,
                    qty=self._plan_qty(signal=signal),
                    priority=5,
                    lane_id=self.long_lane_id,
                    ownership_scope="strategy",
                    close_only=True,
                    flatten_action=True,
                    reduce_only=True,
                    trigger_id=trigger_id,
                    reason="agent_paper_flatten",
                    payload={
                        "reason": "agent_paper_flatten",
                        "agent_bridge": True,
                        "paper_test": True,
                        "test_strategy": True,
                        "signal_id": signal_id,
                        "absolute_signal_qty": True,
                        "allow_outside_rth": True,
                        "suppress_manual_intervention_lock": True,
                    },
                )
            ]
        return []

    def evaluate_entry_permission(
        self,
        *,
        signal: SignalIntent,
        feature_context: FeatureContext,
        runtime_state: dict[str, Any],
    ) -> EntryPermissionDecision:
        if self.require_paper_ack is not True:
            return EntryPermissionDecision(allowed=False, reason="paper_operator_ack_missing")
        qty = self._plan_qty(signal=signal)
        if qty <= 0.0 or qty > self.max_qty:
            return EntryPermissionDecision(
                allowed=False,
                reason="agent_qty_out_of_bounds",
                payload={"qty": qty, "max_qty": self.max_qty},
            )
        return EntryPermissionDecision(allowed=True)

    def _latest_signal(self, *, feature_context: FeatureContext) -> AgentPaperSignal | None:
        path = self.signal_path
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return None
        now_ts = self._now_ts(feature_context=feature_context)
        latest: AgentPaperSignal | None = None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return None
        for line in reversed(lines[-200:]):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            signal = self._parse_signal(payload, now_ts=now_ts)
            if signal is None:
                continue
            latest = signal
            break
        return latest

    def _parse_signal(self, payload: dict[str, Any], *, now_ts: float) -> AgentPaperSignal | None:
        symbol = str(payload.get("symbol") or self.symbol).strip().upper() or self.symbol
        if symbol != self.symbol:
            return None
        action = str(payload.get("action") or payload.get("intent") or "").strip().lower()
        if action not in {"enter", "flatten", "hold"}:
            return None
        side_raw = str(payload.get("side") or "").strip().upper()
        side = side_raw if side_raw in {"LONG", "SHORT"} else None
        if action == "enter" and side is None:
            return None
        signal_id = str(payload.get("signal_id") or payload.get("id") or "").strip()
        if not signal_id:
            return None
        ts_epoch_s = self._payload_ts(payload)
        max_age_sec = max(1.0, _float_param(payload, "max_age_sec", self.max_signal_age_sec))
        if ts_epoch_s > 0.0 and now_ts > 0.0 and now_ts - ts_epoch_s > max_age_sec:
            return None
        max_allowed_qty = self.max_flatten_qty if action == "flatten" else self.max_qty
        qty = max(0.0, min(_float_param(payload, "qty", self.default_qty), max_allowed_qty))
        reason = str(payload.get("reason") or "agent_paper_signal").strip() or "agent_paper_signal"
        return AgentPaperSignal(
            signal_id=signal_id,
            symbol=symbol,
            action=action,
            side=side,
            qty=qty or self.default_qty,
            ts_epoch_s=ts_epoch_s,
            max_age_sec=max_age_sec,
            reason=reason,
            payload=dict(payload),
        )

    @staticmethod
    def _payload_ts(payload: dict[str, Any]) -> float:
        for key in ("ts_epoch_s", "timestamp_s", "epoch_s"):
            try:
                value = float(payload.get(key) or 0.0)
            except Exception:
                value = 0.0
            if value > 0.0:
                return value
        raw = payload.get("ts") or payload.get("timestamp")
        if raw:
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return float(parsed.timestamp())
            except Exception:
                return 0.0
        return 0.0

    @staticmethod
    def _now_ts(*, feature_context: FeatureContext) -> float:
        try:
            return float(getattr(getattr(feature_context, "snapshot", None), "ts_epoch_s", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _plan_qty(self, *, signal: SignalIntent) -> float:
        action = str(getattr(signal, "action", "") or "").strip().lower()
        max_allowed_qty = self.max_flatten_qty if action == "flatten" else self.max_qty
        return max(1.0, min(_float_param(dict(signal.payload or {}), "qty", self.default_qty), max_allowed_qty))

    def _mark_emitted(self, *, day_key: str, signal_id: str) -> None:
        self._emitted_signal_ids.add(signal_id)
        self._day_counts[day_key] = int(self._day_counts.get(day_key, 0)) + 1
        self._save_emitted_signal_ids()

    def _load_emitted_signal_ids(self) -> set[str]:
        path = self.state_path
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        values = payload.get("emitted_signal_ids") if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            return set()
        return {str(item or "").strip() for item in values if str(item or "").strip()}

    def _save_emitted_signal_ids(self) -> None:
        path = self.state_path
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"emitted_signal_ids": sorted(self._emitted_signal_ids)[-1000:]},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            return

    @staticmethod
    def _day_key(*, feature_context: FeatureContext) -> str:
        ts_epoch_s = AgentPaperBridgePolicy._now_ts(feature_context=feature_context)
        if ts_epoch_s <= 0.0:
            return "unknown"
        return str(int(ts_epoch_s // 86400))


def build_agent_paper_bridge_policy(
    policy_params: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> AgentPaperBridgePolicy:
    return AgentPaperBridgePolicy(policy_params=policy_params, extras=extras)
