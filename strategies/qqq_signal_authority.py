from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class QQQSignalSnapshot:
    strategy_id: str
    decision_id: str
    trigger_id: str
    trace_id: str
    ts_et: str
    ts_epoch_s: float
    snapshot_kind: str
    direction: str
    exec_symbol: str
    signal_state: Dict[str, Any] = field(default_factory=dict)
    signal_authority: Dict[str, Any] = field(default_factory=dict)
    intent: Dict[str, Any] = field(default_factory=dict)
    order_plan: Dict[str, Any] = field(default_factory=dict)
    block_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "decision_id": self.decision_id,
            "trigger_id": self.trigger_id,
            "trace_id": self.trace_id,
            "ts_et": self.ts_et,
            "ts_epoch_s": float(self.ts_epoch_s),
            "snapshot_kind": self.snapshot_kind,
            "minute": str(self.ts_et[:16]),
            "direction": self.direction,
            "exec_symbol": self.exec_symbol,
            "signal_state": dict(self.signal_state or {}),
            "signal_authority": dict(self.signal_authority or {}),
            "intent": dict(self.intent or {}),
            "order_plan": dict(self.order_plan or {}),
            "block_reason": self.block_reason,
        }


class QQQSignalAuthority:
    """
    Transitional signal authority for QQQ migration.

    Phase scope: legacy monolith remains the calculator, while this authority
    emits the canonical per-entry signal snapshot consumed by parity tooling.
    """

    def __init__(self, *, snapshot_root: str | Path = "reports/qqq_signal_snapshots") -> None:
        self.snapshot_root = Path(snapshot_root)

    def build_entry_snapshot(
        self,
        *,
        strategy_id: str,
        decision_id: str,
        trigger_id: str,
        trace_id: str,
        ts: datetime,
        direction: str,
        exec_symbol: str,
        signal_state: Dict[str, Any],
    ) -> QQQSignalSnapshot:
        return QQQSignalSnapshot(
            strategy_id=str(strategy_id),
            decision_id=str(decision_id),
            trigger_id=str(trigger_id),
            trace_id=str(trace_id),
            ts_et=ts.isoformat(),
            ts_epoch_s=float(ts.timestamp()),
            snapshot_kind="entry",
            direction=str(direction),
            exec_symbol=str(exec_symbol),
            signal_state=dict(signal_state or {}),
            signal_authority={
                "allow": True,
                "side": str(direction),
                "reason": "entry_allowed",
            },
            intent={
                "symbol": str(exec_symbol),
                "side": str(direction),
            },
            order_plan={
                "exec_symbol": str(exec_symbol),
                "side": str(direction),
            },
        )

    def build_evaluation_snapshot(
        self,
        *,
        strategy_id: str,
        decision_id: str,
        trigger_id: str,
        trace_id: str,
        ts: datetime,
        allow: bool,
        side: str,
        reason: str,
        exec_symbol: str,
        signal_state: Dict[str, Any],
    ) -> QQQSignalSnapshot:
        side_value = str(side or "FLAT")
        exec_symbol_value = str(exec_symbol or "")
        return QQQSignalSnapshot(
            strategy_id=str(strategy_id),
            decision_id=str(decision_id),
            trigger_id=str(trigger_id),
            trace_id=str(trace_id),
            ts_et=ts.isoformat(),
            ts_epoch_s=float(ts.timestamp()),
            snapshot_kind="evaluation",
            direction=side_value,
            exec_symbol=exec_symbol_value,
            signal_state=dict(signal_state or {}),
            signal_authority={
                "allow": bool(allow),
                "side": side_value,
                "reason": str(reason or ""),
            },
            intent={
                "symbol": exec_symbol_value,
                "side": side_value,
            },
            order_plan={
                "exec_symbol": exec_symbol_value,
                "side": side_value,
            },
            block_reason=(None if allow else str(reason or "")),
        )

    def persist_snapshot(self, snapshot: QQQSignalSnapshot) -> Path:
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        day = str(snapshot.ts_et[:10]).replace("-", "")
        path = self.snapshot_root / f"qqq_signal_snapshots_{day}.jsonl"
        path.open("a", encoding="utf-8").write(json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return path

    def publish_entry_snapshot(self, **kwargs: Any) -> Dict[str, Any]:
        snapshot = self.build_entry_snapshot(**kwargs)
        path = self.persist_snapshot(snapshot)
        return {
            "signal_authority": snapshot.to_dict(),
            "snapshot_path": str(path),
        }

    def publish_evaluation_snapshot(self, **kwargs: Any) -> Dict[str, Any]:
        snapshot = self.build_evaluation_snapshot(**kwargs)
        path = self.persist_snapshot(snapshot)
        return {
            "signal_authority": snapshot.to_dict(),
            "snapshot_path": str(path),
        }
