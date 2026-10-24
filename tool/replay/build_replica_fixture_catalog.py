#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def _event(
    *,
    stage: str,
    symbol: str = "QQQ",
    trace_id: str,
    side: str | None = None,
    qty: float | None = None,
    reason: str | None = None,
    order_ref: str | None = None,
    runtime_before: str | None = None,
    runtime_after: str | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if side is not None:
        payload["side"] = side
    if qty is not None:
        payload["qty"] = qty
    if reason is not None:
        payload["reason"] = reason
    if order_ref is not None:
        payload["order_ref"] = order_ref
    if runtime_before is not None:
        payload["runtime_lifecycle_before"] = runtime_before
    if runtime_after is not None:
        payload["runtime_lifecycle_after"] = runtime_after
    return {
        "stage": stage,
        "symbol": symbol,
        "trace_id": trace_id,
        "reason": reason,
        "payload": payload,
    }


def build_fixture_catalog() -> Dict[str, Any]:
    fixtures: Dict[str, Dict[str, Any]] = {
        "gate_allowed_long": {
            "category": "gate",
            "events": [
                _event(
                    stage="entry_gate_allowed",
                    trace_id="fixture:gate_allowed_long",
                    side="LONG",
                    qty=8.0,
                    reason="entry_allowed",
                    runtime_before="flat",
                    runtime_after="flat",
                ),
                _event(
                    stage="entry_intent_submitted",
                    trace_id="fixture:gate_allowed_long",
                    side="LONG",
                    qty=8.0,
                    reason="entry_allowed",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:fixture",
                    runtime_before="flat",
                    runtime_after="intent_submitted",
                ),
            ],
        },
        "gate_blocked_required_data_missing": {
            "category": "gate",
            "events": [
                _event(
                    stage="entry_gate_blocked",
                    trace_id="fixture:gate_blocked_required_data_missing",
                    reason="required_data_missing",
                    runtime_before="flat",
                    runtime_after="flat",
                )
            ],
        },
        "gate_blocked_restart_degrade": {
            "category": "gate",
            "events": [
                _event(
                    stage="entry_gate_blocked",
                    trace_id="fixture:gate_blocked_restart_degrade",
                    reason="restart_degrade_active",
                    runtime_before="flat",
                    runtime_after="flat",
                )
            ],
        },
        "gate_blocked_market_data_unavailable": {
            "category": "gate",
            "events": [
                _event(
                    stage="entry_gate_blocked",
                    trace_id="fixture:gate_blocked_market_data_unavailable",
                    reason="market_data_unavailable",
                    runtime_before="flat",
                    runtime_after="flat",
                )
            ],
        },
        "exit_tp1_partial_fill": {
            "category": "exit",
            "events": [
                _event(
                    stage="tp1_partial_submitted",
                    trace_id="fixture:exit_tp1_partial_fill",
                    side="LONG",
                    qty=-4.0,
                    reason="tp1_hit",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:TP1:TRG:fixture",
                    runtime_before="live",
                    runtime_after="live",
                ),
                _event(
                    stage="tp1_exit_confirmed",
                    trace_id="fixture:exit_tp1_partial_fill",
                    side="LONG",
                    qty=-4.0,
                    reason="order_update:filled",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:TP1:TRG:fixture",
                    runtime_before="live",
                    runtime_after="live",
                ),
            ],
        },
        "exit_tp1_then_trailing_stop": {
            "category": "exit",
            "events": [
                _event(
                    stage="tp1_partial_submitted",
                    trace_id="fixture:exit_tp1_then_trailing_stop",
                    side="LONG",
                    qty=-4.0,
                    reason="tp1_hit",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:TP1:TRG:fixture",
                    runtime_before="live",
                    runtime_after="live",
                ),
                _event(
                    stage="flatten_submitted",
                    trace_id="fixture:exit_tp1_then_trailing_stop",
                    side="LONG",
                    qty=-4.0,
                    reason="tp1_then_trailing_stop",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:FLATTEN:TRG:fixture",
                    runtime_before="live",
                    runtime_after="live",
                ),
            ],
        },
        "exit_breakeven_stop": {
            "category": "exit",
            "events": [
                _event(
                    stage="protective_stop_submitted",
                    trace_id="fixture:exit_breakeven_stop",
                    side="LONG",
                    qty=-8.0,
                    reason="breakeven_shield",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:fixture:1",
                    runtime_before="live",
                    runtime_after="live",
                ),
                _event(
                    stage="protective_stop_live",
                    trace_id="fixture:exit_breakeven_stop",
                    side="LONG",
                    qty=-8.0,
                    reason="order_update:submitted",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:fixture:1",
                    runtime_before="live",
                    runtime_after="live",
                ),
            ],
        },
        "exit_force_eod_flatten": {
            "category": "exit",
            "events": [
                _event(
                    stage="flatten_submitted",
                    trace_id="fixture:exit_force_eod_flatten",
                    side="LONG",
                    qty=-8.0,
                    reason="force_flatten_1559_EOD",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:FLATTEN:TRG:fixture",
                    runtime_before="live",
                    runtime_after="live",
                ),
                _event(
                    stage="flatten_confirmed",
                    trace_id="fixture:exit_force_eod_flatten",
                    side="LONG",
                    qty=-8.0,
                    reason="order_update:filled",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:FLATTEN:TRG:fixture",
                    runtime_before="live",
                    runtime_after="flat",
                ),
            ],
        },
        "protection_restart_missing_stop_kill_switch": {
            "category": "protection",
            "events": [
                _event(
                    stage="restart_unprotected_kill_switch_preview",
                    trace_id="fixture:protection_restart_missing_stop_kill_switch",
                    side="LONG",
                    qty=-8.0,
                    reason="restart_unprotected_no_stop_px",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:FLATTEN:TRG:fixture",
                    runtime_before="live",
                    runtime_after="live",
                )
            ],
        },
        "protection_audit_self_heal_after_threshold": {
            "category": "protection",
            "events": [
                _event(
                    stage="protection_audit_warning",
                    trace_id="fixture:protection_audit_self_heal_after_threshold",
                    reason="broker_stop_missing",
                    runtime_before="live",
                    runtime_after="live",
                ),
                _event(
                    stage="protection_audit_self_heal",
                    trace_id="fixture:protection_audit_self_heal_after_threshold",
                    reason="audit_stop_inactive",
                    runtime_before="live",
                    runtime_after="live",
                ),
                _event(
                    stage="protection_audit_self_heal_preview",
                    trace_id="fixture:protection_audit_self_heal_after_threshold",
                    side="LONG",
                    qty=-8.0,
                    reason="audit_stop_inactive",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:fixture:1",
                    runtime_before="live",
                    runtime_after="live",
                ),
            ],
        },
        "protection_audit_recovered_stop_live": {
            "category": "protection",
            "events": [
                _event(
                    stage="protection_audit_recovered",
                    trace_id="fixture:protection_audit_recovered_stop_live",
                    reason="broker_stop_visible_again",
                    runtime_before="live",
                    runtime_after="live",
                ),
                _event(
                    stage="protection_audit_stop_live",
                    trace_id="fixture:protection_audit_recovered_stop_live",
                    side="LONG",
                    qty=-8.0,
                    reason="stop_active",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:fixture:1",
                    runtime_before="live",
                    runtime_after="live",
                ),
            ],
        },
        "protection_audit_kill_switch_after_threshold": {
            "category": "protection",
            "events": [
                _event(
                    stage="protection_audit_warning",
                    trace_id="fixture:protection_audit_kill_switch_after_threshold",
                    reason="broker_stop_missing",
                    runtime_before="live",
                    runtime_after="live",
                ),
                _event(
                    stage="protection_audit_kill_switch_preview",
                    trace_id="fixture:protection_audit_kill_switch_after_threshold",
                    side="LONG",
                    qty=-8.0,
                    reason="audit_stop_inactive_kill_switch",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:FLATTEN:TRG:fixture",
                    runtime_before="live",
                    runtime_after="live",
                ),
            ],
        },
    }
    return {
        "generated_by": "build_replica_fixture_catalog.py",
        "fixture_names": list(fixtures.keys()),
        "fixtures": fixtures,
    }


def write_fixture_catalog(out_path: Path) -> Path:
    payload = build_fixture_catalog()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build synthetic replay fixture catalog for index replica")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[2] / "docs" / "baselines" / "index_super_win_replica_fixture_catalog.json"),
    )
    args = parser.parse_args()
    path = write_fixture_catalog(Path(args.out))
    print(json.dumps({"written": str(path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
