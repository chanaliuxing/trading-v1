#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class StrategyExecutionReliabilityDriver(str, Enum):
    NO_BOTTLENECK = "NO_BOTTLENECK"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    OPTION_DATA_POLICY = "OPTION_DATA_POLICY"
    VALIDATOR_LATENCY = "VALIDATOR_LATENCY"
    SUBMIT_INTENT_LOSS = "SUBMIT_INTENT_LOSS"
    UOM_ROUTING = "UOM_ROUTING"
    EXECUTION_GATE = "EXECUTION_GATE"
    PAPER_BROKER = "PAPER_BROKER"
    BROKER_ACK = "BROKER_ACK"
    FILL_LIQUIDITY = "FILL_LIQUIDITY"
    ATTRIBUTION = "ATTRIBUTION"
    PNL_REPLAY = "PNL_REPLAY"
    SYSTEM_READINESS = "SYSTEM_READINESS"
    MIXED = "MIXED"


@dataclass(frozen=True)
class StrategyExecutionReliabilityConfig:
    min_sample_count: int = 20
    bottleneck_rate_threshold: float = 0.20
    latency_ms_threshold: float = 2000.0


@dataclass
class ReliabilityChainAccumulator:
    strategy_id: str = ""
    strategy_instance_id: str = ""
    symbol: str = ""
    underlying: str = ""
    asset_type: str = ""
    trace_id: str = ""
    decision_id: str = ""
    intent_id: str = ""
    client_order_id: str = ""
    lot_id: str = ""
    signal_ts: datetime | None = None
    decision_approved_ts: datetime | None = None
    validator_start_ts: datetime | None = None
    validator_end_ts: datetime | None = None
    submit_intent_ts: datetime | None = None
    uom_delegate_ts: datetime | None = None
    execution_gate_ts: datetime | None = None
    paper_broker_submit_ts: datetime | None = None
    broker_ack_ts: datetime | None = None
    first_fill_ts: datetime | None = None
    attribution_ts: datetime | None = None
    pnl_replay_ts: datetime | None = None
    reject_reasons: list[str] = field(default_factory=list)
    validator_rejected: bool = False
    execution_rejected: bool = False
    fill_seen: bool = False
    uom_route_status: str = ""
    uom_delegate_target: str | None = None
    uom_reject_reason: str | None = None
    uom_shadow_mode: bool = False
    owner_identity_complete: bool | None = None
    attribution_status: str = ""
    pnl_replay_ready: bool = False
    system_health: str = ""
    broker_truth_ready: bool | None = None
    position_safety_ready: bool | None = None
    quarantine_clear: bool | None = None
    runtime_fresh: bool | None = None
    manual_recovery_clear: bool | None = None
    quote_missing: bool = False
    quote_source: str | None = None
    quote_age_ms: float | None = None
    contract_policy: str | None = None
    queue_delay_ms: float | None = None
    signal_age_at_validator_ms: float | None = None
    validator_delay_ms: float | None = None


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(ET) if value.tzinfo else value.replace(tzinfo=ET)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone(ET) if parsed.tzinfo else parsed.replace(tzinfo=ET)
    except Exception:
        return None


def _first_ts(*values: Any) -> datetime | None:
    for value in values:
        parsed = _parse_ts(value)
        if parsed is not None:
            return parsed
    return None


def _nested(row: dict[str, Any], key: str) -> Any:
    if key in row:
        return row.get(key)
    for container in ("payload", "extras", "order", "fill", "event", "readiness", "option_data", "latency_ms"):
        value = row.get(container)
        if isinstance(value, dict) and key in value:
            return value.get(key)
    return None


def _text(row: dict[str, Any], key: str) -> str:
    return str(_nested(row, key) or "").strip()


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        fval = float(value)
    except Exception:
        return None
    if math.isnan(fval) or math.isinf(fval):
        return None
    return fval


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ok", "pass"}:
        return True
    if text in {"0", "false", "no", "fail"}:
        return False
    return None


def _latency_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() * 1000.0)


def _append_unique(target: list[str], values: Iterable[Any]) -> None:
    seen = set(target)
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            target.append(text)


def _row_reasons(row: dict[str, Any]) -> list[str]:
    values: list[Any] = [row.get("reason"), row.get("error_type"), row.get("status"), row.get("stage")]
    for key in ("reject_reasons", "rejection_reasons", "reasons", "blockers", "warnings", "submit_reasons"):
        value = _nested(row, key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    out: list[str] = []
    _append_unique(out, values)
    return out


def _stage(row: dict[str, Any]) -> str:
    return str(row.get("stage") or row.get("event_type") or row.get("type") or "").strip().lower()


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or _nested(row, "status") or "").strip().lower()


def _correlation_key(row: dict[str, Any]) -> tuple[str, str] | None:
    for key in ("intent_id", "client_order_id"):
        value = _text(row, key)
        if value:
            return key, value
    trace_id = _text(row, "trace_id")
    decision_id = _text(row, "decision_id")
    if trace_id and decision_id:
        return "trace_decision", f"{trace_id}:{decision_id}"
    return None


def _merge_identity(acc: ReliabilityChainAccumulator, row: dict[str, Any]) -> None:
    for attr, key in (
        ("strategy_id", "strategy_id"),
        ("strategy_instance_id", "strategy_instance_id"),
        ("symbol", "symbol"),
        ("underlying", "underlying"),
        ("asset_type", "asset_type"),
        ("trace_id", "trace_id"),
        ("decision_id", "decision_id"),
        ("intent_id", "intent_id"),
        ("client_order_id", "client_order_id"),
        ("lot_id", "lot_id"),
    ):
        if not getattr(acc, attr):
            value = _text(row, key)
            if value:
                setattr(acc, attr, value)
    if not acc.underlying:
        acc.underlying = _text(row, "bars_symbol") or acc.symbol
    if not acc.asset_type:
        instrument_type = _text(row, "instrument_type").lower()
        symbol = acc.symbol.upper()
        acc.asset_type = "option" if instrument_type == "option" or symbol.startswith("OPT:") else "stock"


def _set_readiness(acc: ReliabilityChainAccumulator, row: dict[str, Any]) -> None:
    system_health = _text(row, "system_health") or _text(row, "system_health_state")
    if system_health:
        acc.system_health = system_health
    for attr, key in (
        ("broker_truth_ready", "broker_truth_ready"),
        ("position_safety_ready", "position_safety_ready"),
        ("quarantine_clear", "quarantine_clear"),
        ("runtime_fresh", "runtime_fresh"),
        ("manual_recovery_clear", "manual_recovery_clear"),
    ):
        parsed = _bool_or_none(_nested(row, key))
        if parsed is not None:
            setattr(acc, attr, parsed)
    quote_missing = _bool_or_none(_nested(row, "quote_missing"))
    if quote_missing is not None:
        acc.quote_missing = bool(quote_missing)
    quote_source = _text(row, "quote_source")
    if quote_source:
        acc.quote_source = quote_source
    quote_age_ms = _float(_nested(row, "quote_age_ms"))
    if quote_age_ms is None:
        quote_age_sec = _float(_nested(row, "quote_age_sec"))
        if quote_age_sec is not None:
            quote_age_ms = quote_age_sec * 1000.0
    if quote_age_ms is not None:
        acc.quote_age_ms = quote_age_ms
    contract_policy = _text(row, "contract_policy")
    if contract_policy:
        acc.contract_policy = contract_policy


def _apply_row(acc: ReliabilityChainAccumulator, row: dict[str, Any]) -> None:
    _merge_identity(acc, row)
    _set_readiness(acc, row)
    stage = _stage(row)
    status = _status(row)
    ts = _first_ts(row.get("ts"), row.get("created_at"), row.get("timestamp"), _nested(row, "ts"), _nested(row, "timestamp"))
    acc.signal_ts = acc.signal_ts or _first_ts(_nested(row, "signal_ts"), _nested(row, "source_trigger_ts"), _nested(row, "source_ts"))
    if "approved" in stage or status == "approved" or "decision_approved" in stage:
        acc.decision_approved_ts = acc.decision_approved_ts or _first_ts(_nested(row, "decision_approved_ts"), _nested(row, "approved_ts"), ts)
    if "validator_start" in stage:
        acc.validator_start_ts = acc.validator_start_ts or _first_ts(_nested(row, "validator_start_ts"), ts)
    if ("validator" in stage or "validation" in stage) and "validator_start" not in stage:
        if acc.validator_start_ts is None:
            acc.validator_start_ts = _first_ts(_nested(row, "validator_start_ts"), _nested(row, "validation_start_ts"))
        acc.validator_end_ts = acc.validator_end_ts or _first_ts(_nested(row, "validator_end_ts"), _nested(row, "validation_ts"), ts)
        if "reject" in stage or "reject" in status or "stale_decision" in " ".join(_row_reasons(row)):
            acc.validator_rejected = True
    if "submit_intent" in stage or "intent_created" in stage:
        acc.submit_intent_ts = acc.submit_intent_ts or _first_ts(_nested(row, "submit_intent_ts"), ts)
    if "uom_delegate" in stage or "delegate" in stage:
        acc.uom_delegate_ts = acc.uom_delegate_ts or _first_ts(_nested(row, "uom_delegate_ts"), ts)
    if stage.startswith("uom_") or _nested(row, "uom_route_status") is not None:
        acc.uom_delegate_ts = acc.uom_delegate_ts or _first_ts(_nested(row, "uom_delegate_ts"), ts)
        status_text = _text(row, "uom_route_status") or status
        if status_text:
            acc.uom_route_status = status_text
        target = _text(row, "uom_delegate_target")
        if target:
            acc.uom_delegate_target = target
        reason = _text(row, "uom_reject_reason") or _text(row, "reason")
        if reason:
            acc.uom_reject_reason = reason
        shadow = _bool_or_none(_nested(row, "uom_shadow_mode"))
        if shadow is not None:
            acc.uom_shadow_mode = bool(shadow)
        owner_complete = _bool_or_none(_nested(row, "owner_identity_complete"))
        if owner_complete is not None:
            acc.owner_identity_complete = bool(owner_complete)
    if "execution_gate" in stage or "execution_accept" in stage or "execution_accepted" in stage:
        acc.execution_gate_ts = acc.execution_gate_ts or _first_ts(_nested(row, "execution_gate_ts"), _nested(row, "execution_accept_ts"), ts)
    if "execution_reject" in stage or ("execution" in stage and "reject" in stage):
        acc.execution_rejected = True
        acc.execution_gate_ts = acc.execution_gate_ts or _first_ts(_nested(row, "execution_gate_ts"), ts)
    if "paper_broker_submit" in stage or "broker_submit" in stage or "broker_submitted" in stage:
        acc.paper_broker_submit_ts = acc.paper_broker_submit_ts or _first_ts(_nested(row, "paper_broker_submit_ts"), _nested(row, "broker_submit_ts"), ts)
    if "broker_ack" in stage or "order_ack" in stage or status == "ack":
        acc.broker_ack_ts = acc.broker_ack_ts or _first_ts(_nested(row, "broker_ack_ts"), ts)
    if "fill" in stage or status in {"filled", "partially_filled"}:
        acc.fill_seen = True
        acc.first_fill_ts = acc.first_fill_ts or _first_ts(_nested(row, "first_fill_ts"), _nested(row, "fill_ts"), ts)
    if "attribution" in stage:
        acc.attribution_ts = acc.attribution_ts or _first_ts(_nested(row, "attribution_ts"), ts)
    attribution = _text(row, "attribution_status")
    if attribution:
        acc.attribution_status = attribution
        if attribution == "attributed":
            acc.attribution_ts = acc.attribution_ts or ts
    if "pnl_replay" in stage:
        acc.pnl_replay_ts = acc.pnl_replay_ts or _first_ts(_nested(row, "pnl_replay_ts"), ts)
        acc.pnl_replay_ready = status not in {"failed", "rejected"}
    if _nested(row, "pnl_replay_ready") is not None:
        acc.pnl_replay_ready = bool(_nested(row, "pnl_replay_ready"))
        if acc.pnl_replay_ready:
            acc.pnl_replay_ts = acc.pnl_replay_ts or ts
    acc.queue_delay_ms = _float(_nested(row, "queue_delay_ms")) if _float(_nested(row, "queue_delay_ms")) is not None else acc.queue_delay_ms
    acc.signal_age_at_validator_ms = _float(_nested(row, "signal_age_at_validator")) or _float(_nested(row, "signal_age_at_validator_ms")) or acc.signal_age_at_validator_ms
    acc.validator_delay_ms = _float(_nested(row, "validator_delay")) or _float(_nested(row, "validator_delay_ms")) or acc.validator_delay_ms
    _append_unique(acc.reject_reasons, _row_reasons(row))


def _system_readiness_blocked(row: dict[str, Any]) -> bool:
    readiness = dict(row.get("readiness") or {})
    if readiness.get("broker_truth_ready") is False:
        return True
    if readiness.get("position_safety_ready") is False:
        return True
    if readiness.get("quarantine_clear") is False:
        return True
    if readiness.get("runtime_fresh") is False:
        return True
    if readiness.get("manual_recovery_clear") is False:
        return True
    system_health = str(readiness.get("system_health") or "").strip().lower()
    return system_health not in {"", "unknown", "ok", "healthy", "recovering"}


def _final_status_and_drop(acc: ReliabilityChainAccumulator) -> tuple[str, str]:
    if acc.validator_rejected:
        return "validator_rejected", "validator"
    if acc.decision_approved_ts is not None and acc.submit_intent_ts is None:
        return "approved_only", "submit_intent"
    if acc.submit_intent_ts is not None and acc.uom_delegate_ts is None:
        return "intent_created", "uom_delegate"
    if acc.uom_route_status in {"rejected", "missing"}:
        return "uom_rejected", "uom_delegate"
    if acc.execution_rejected:
        return "execution_rejected", "execution_gate"
    if acc.uom_delegate_ts is not None and acc.execution_gate_ts is None:
        if acc.uom_shadow_mode and acc.uom_route_status in {"skipped", "delegated_shadow"}:
            return "shadow_no_broker_submit", "none"
        return "uom_delegated", "execution_gate"
    if acc.execution_gate_ts is not None and acc.paper_broker_submit_ts is None:
        if acc.uom_shadow_mode and acc.uom_route_status in {"skipped", "delegated_shadow"}:
            return "shadow_no_broker_submit", "none"
        return "execution_accepted", "paper_broker_submit"
    if acc.paper_broker_submit_ts is not None and acc.broker_ack_ts is None:
        return "paper_broker_submitted", "broker_ack"
    if acc.broker_ack_ts is not None and not acc.fill_seen:
        return "broker_acknowledged", "fill"
    if acc.fill_seen and (not acc.attribution_ts or acc.attribution_status in {"missing_owner", "ambiguous_owner", "unknown"}):
        return "filled", "attribution"
    if acc.attribution_ts is not None and not acc.pnl_replay_ready:
        return "attributed", "pnl_replay"
    if acc.pnl_replay_ready:
        return "pnl_replay_ready", "none"
    return "unknown", "none"


def _to_record(acc: ReliabilityChainAccumulator) -> dict[str, Any]:
    final_status, drop_stage = _final_status_and_drop(acc)
    signal_age = acc.signal_age_at_validator_ms
    if signal_age is None:
        signal_age = _latency_ms(acc.signal_ts, acc.validator_start_ts or acc.validator_end_ts)
    validator_delay = acc.validator_delay_ms
    if validator_delay is None:
        validator_delay = _latency_ms(acc.validator_start_ts, acc.validator_end_ts)
    queue_delay = acc.queue_delay_ms
    if queue_delay is None:
        queue_delay = _latency_ms(acc.decision_approved_ts, acc.validator_start_ts)
    readiness = {
        "system_health": acc.system_health or "unknown",
        "broker_truth_ready": True if acc.broker_truth_ready is None else acc.broker_truth_ready,
        "position_safety_ready": True if acc.position_safety_ready is None else acc.position_safety_ready,
        "quarantine_clear": True if acc.quarantine_clear is None else acc.quarantine_clear,
        "runtime_fresh": True if acc.runtime_fresh is None else acc.runtime_fresh,
        "manual_recovery_clear": True if acc.manual_recovery_clear is None else acc.manual_recovery_clear,
    }
    return {
        "strategy_id": acc.strategy_id,
        "strategy_instance_id": acc.strategy_instance_id,
        "symbol": acc.symbol,
        "underlying": acc.underlying or acc.symbol,
        "asset_type": acc.asset_type or "stock",
        "trace_id": acc.trace_id,
        "decision_id": acc.decision_id,
        "intent_id": acc.intent_id,
        "client_order_id": acc.client_order_id,
        "lot_id": acc.lot_id,
        "signal_ts": acc.signal_ts.isoformat() if acc.signal_ts else None,
        "decision_approved_ts": acc.decision_approved_ts.isoformat() if acc.decision_approved_ts else None,
        "validator_start_ts": acc.validator_start_ts.isoformat() if acc.validator_start_ts else None,
        "validator_end_ts": acc.validator_end_ts.isoformat() if acc.validator_end_ts else None,
        "submit_intent_ts": acc.submit_intent_ts.isoformat() if acc.submit_intent_ts else None,
        "uom_delegate_ts": acc.uom_delegate_ts.isoformat() if acc.uom_delegate_ts else None,
        "execution_gate_ts": acc.execution_gate_ts.isoformat() if acc.execution_gate_ts else None,
        "paper_broker_submit_ts": acc.paper_broker_submit_ts.isoformat() if acc.paper_broker_submit_ts else None,
        "broker_ack_ts": acc.broker_ack_ts.isoformat() if acc.broker_ack_ts else None,
        "first_fill_ts": acc.first_fill_ts.isoformat() if acc.first_fill_ts else None,
        "attribution_ts": acc.attribution_ts.isoformat() if acc.attribution_ts else None,
        "pnl_replay_ts": acc.pnl_replay_ts.isoformat() if acc.pnl_replay_ts else None,
        "final_status": final_status,
        "drop_stage": drop_stage,
        "reject_reasons": acc.reject_reasons,
        "uom_route_status": acc.uom_route_status or ("missing" if acc.submit_intent_ts is not None and acc.uom_delegate_ts is None else None),
        "uom_delegate_target": acc.uom_delegate_target,
        "uom_reject_reason": acc.uom_reject_reason,
        "uom_shadow_mode": bool(acc.uom_shadow_mode),
        "owner_identity_complete": acc.owner_identity_complete,
        "latency_ms": {
            "signal_age_at_validator": signal_age,
            "queue_delay": queue_delay,
            "validator_delay": validator_delay,
            "quote_age_at_validator": acc.quote_age_ms,
            "approved_to_submit_intent": _latency_ms(acc.decision_approved_ts, acc.submit_intent_ts),
            "submit_intent_to_uom": _latency_ms(acc.submit_intent_ts, acc.uom_delegate_ts),
            "uom_to_execution_gate": _latency_ms(acc.uom_delegate_ts, acc.execution_gate_ts),
            "execution_gate_to_broker_submit": _latency_ms(acc.execution_gate_ts, acc.paper_broker_submit_ts),
            "broker_submit_to_ack": _latency_ms(acc.paper_broker_submit_ts, acc.broker_ack_ts),
            "ack_to_fill": _latency_ms(acc.broker_ack_ts, acc.first_fill_ts),
            "fill_to_attribution": _latency_ms(acc.first_fill_ts, acc.attribution_ts),
            "attribution_to_pnl_replay": _latency_ms(acc.attribution_ts, acc.pnl_replay_ts),
        },
        "readiness": readiness,
        "option_data": {
            "quote_missing": bool(acc.quote_missing),
            "quote_source": acc.quote_source,
            "quote_age_ms": acc.quote_age_ms,
            "contract_policy": acc.contract_policy,
        },
    }


def build_chains_from_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chains: dict[tuple[str, str], ReliabilityChainAccumulator] = {}
    for row in rows:
        key = _correlation_key(row)
        if key is None:
            key = ("uncorrelated", str(row.get("event_id") or row.get("ts") or uuid.uuid4()))
        acc = chains.setdefault(key, ReliabilityChainAccumulator())
        _apply_row(acc, row)
    records = [_to_record(acc) for acc in chains.values()]
    return [
        record
        for record in records
        if record.get("decision_approved_ts")
        or record.get("validator_start_ts")
        or record.get("submit_intent_ts")
        or record.get("first_fill_ts")
    ]


def _percentile(values: list[float], pct: float) -> float | None:
    clean = sorted(float(v) for v in values if v is not None and not math.isnan(float(v)))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = (len(clean) - 1) * pct
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return clean[int(rank)]
    return clean[lo] + (clean[hi] - clean[lo]) * (rank - lo)


def _latency_summary(records: list[dict[str, Any]], key: str) -> dict[str, float | None]:
    values = [
        float((row.get("latency_ms") or {}).get(key))
        for row in records
        if (row.get("latency_ms") or {}).get(key) is not None
    ]
    return {
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def _rate(num: int, denom: int) -> float:
    return float(num) / float(denom) if denom > 0 else 0.0


def classify_reliability_driver(
    records: list[dict[str, Any]],
    *,
    min_sample_count: int = 20,
    threshold: float = 0.20,
    latency_ms_threshold: float = 2000.0,
) -> str:
    sample_count = len(records)
    if sample_count < min_sample_count:
        return StrategyExecutionReliabilityDriver.INSUFFICIENT_SAMPLE.value
    drop_counts: dict[str, int] = {}
    for row in records:
        drop = str(row.get("drop_stage") or "none")
        drop_counts[drop] = drop_counts.get(drop, 0) + 1
    readiness_count = sum(1 for row in records if _system_readiness_blocked(row))
    quote_missing_count = sum(1 for row in records if bool((row.get("option_data") or {}).get("quote_missing")))
    latency_count = sum(
        1
        for row in records
        if any(
            float((row.get("latency_ms") or {}).get(key) or 0.0) >= latency_ms_threshold
            for key in ("signal_age_at_validator", "queue_delay", "validator_delay")
        )
    )
    mapping = {
        "validator": StrategyExecutionReliabilityDriver.VALIDATOR_LATENCY.value,
        "submit_intent": StrategyExecutionReliabilityDriver.SUBMIT_INTENT_LOSS.value,
        "uom_delegate": StrategyExecutionReliabilityDriver.UOM_ROUTING.value,
        "execution_gate": StrategyExecutionReliabilityDriver.EXECUTION_GATE.value,
        "paper_broker_submit": StrategyExecutionReliabilityDriver.PAPER_BROKER.value,
        "broker_ack": StrategyExecutionReliabilityDriver.BROKER_ACK.value,
        "fill": StrategyExecutionReliabilityDriver.FILL_LIQUIDITY.value,
        "attribution": StrategyExecutionReliabilityDriver.ATTRIBUTION.value,
        "pnl_replay": StrategyExecutionReliabilityDriver.PNL_REPLAY.value,
    }
    if _rate(quote_missing_count, sample_count) >= threshold:
        return StrategyExecutionReliabilityDriver.OPTION_DATA_POLICY.value
    if _rate(readiness_count, sample_count) >= threshold:
        return StrategyExecutionReliabilityDriver.SYSTEM_READINESS.value
    drivers: list[str] = []
    if _rate(latency_count, sample_count) >= threshold:
        drivers.append(StrategyExecutionReliabilityDriver.VALIDATOR_LATENCY.value)
    for stage, driver in mapping.items():
        if _rate(drop_counts.get(stage, 0), sample_count) >= threshold and driver not in drivers:
            drivers.append(driver)
    if len(drivers) > 1:
        return StrategyExecutionReliabilityDriver.MIXED.value
    return drivers[0] if drivers else StrategyExecutionReliabilityDriver.NO_BOTTLENECK.value


def _recommendations(driver: str) -> list[str]:
    return {
        StrategyExecutionReliabilityDriver.NO_BOTTLENECK.value: ["continue_collecting_paper_shadow_execution_samples"],
        StrategyExecutionReliabilityDriver.INSUFFICIENT_SAMPLE.value: ["collect_more_paper_shadow_execution_samples"],
        StrategyExecutionReliabilityDriver.OPTION_DATA_POLICY.value: ["migrate_quote_health_to_shared_option_contract_policy"],
        StrategyExecutionReliabilityDriver.VALIDATOR_LATENCY.value: ["split_signal_age_queue_delay_validator_delay_and_quote_age"],
        StrategyExecutionReliabilityDriver.SUBMIT_INTENT_LOSS.value: ["inspect_approved_decision_to_submit_intent_bridge"],
        StrategyExecutionReliabilityDriver.UOM_ROUTING.value: ["inspect_uom_delegate_routing_by_intent_kind_and_order_role"],
        StrategyExecutionReliabilityDriver.EXECUTION_GATE.value: ["inspect_execution_gate_reject_reasons_without_relaxing_safety"],
        StrategyExecutionReliabilityDriver.PAPER_BROKER.value: ["inspect_paper_broker_submit_adapter_path"],
        StrategyExecutionReliabilityDriver.BROKER_ACK.value: ["inspect_paper_broker_ack_generation"],
        StrategyExecutionReliabilityDriver.FILL_LIQUIDITY.value: ["inspect_paper_fill_model_and_time_in_force"],
        StrategyExecutionReliabilityDriver.ATTRIBUTION.value: ["inspect_owner_complete_fill_attribution"],
        StrategyExecutionReliabilityDriver.PNL_REPLAY.value: ["inspect_pnl_replay_input_requirements"],
        StrategyExecutionReliabilityDriver.SYSTEM_READINESS.value: ["inspect_hard_readiness_blockers_without_bypassing_safety"],
    }.get(driver, ["separate_reliability_drivers_before_parameter_changes"])


def build_reliability_report(
    records: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    config: StrategyExecutionReliabilityConfig | None = None,
) -> dict[str, Any]:
    cfg = config or StrategyExecutionReliabilityConfig()
    now = generated_at.astimezone(ET) if generated_at else datetime.now(ET)
    stage_counts = {
        "approved": sum(1 for row in records if row.get("decision_approved_ts")),
        "validated": sum(1 for row in records if row.get("validator_end_ts")),
        "submit_intent": sum(1 for row in records if row.get("submit_intent_ts")),
        "uom_delegate": sum(1 for row in records if row.get("uom_delegate_ts")),
        "execution_gate": sum(1 for row in records if row.get("execution_gate_ts")),
        "paper_broker_submit": sum(1 for row in records if row.get("paper_broker_submit_ts")),
        "broker_ack": sum(1 for row in records if row.get("broker_ack_ts")),
        "fill": sum(1 for row in records if row.get("first_fill_ts")),
        "attribution": sum(1 for row in records if row.get("attribution_ts")),
        "pnl_replay": sum(1 for row in records if row.get("pnl_replay_ts")),
    }
    drop_stage_counts: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}
    quote_by_strategy: dict[str, int] = {}
    quote_by_underlying: dict[str, int] = {}
    readiness_blockers = {
        "system_health_recovering": 0,
        "broker_truth": 0,
        "position_safety": 0,
        "quarantine": 0,
        "runtime_stale": 0,
        "manual_recovery": 0,
    }
    uom_route_status_counts: dict[str, int] = {}
    uom_reject_reason_counts: dict[str, int] = {}
    uom_delegate_target_counts: dict[str, int] = {}
    for row in records:
        drop = str(row.get("drop_stage") or "none")
        drop_stage_counts[drop] = drop_stage_counts.get(drop, 0) + 1
        sid = str(row.get("strategy_id") or "unknown")
        strategy_counts[sid] = strategy_counts.get(sid, 0) + 1
        option_data = row.get("option_data") or {}
        if bool(option_data.get("quote_missing")):
            quote_by_strategy[sid] = quote_by_strategy.get(sid, 0) + 1
            underlying = str(row.get("underlying") or row.get("symbol") or "unknown")
            quote_by_underlying[underlying] = quote_by_underlying.get(underlying, 0) + 1
        readiness = row.get("readiness") or {}
        if str(readiness.get("system_health") or "").strip().lower() == "recovering":
            readiness_blockers["system_health_recovering"] += 1
        if readiness.get("broker_truth_ready") is False:
            readiness_blockers["broker_truth"] += 1
        if readiness.get("position_safety_ready") is False:
            readiness_blockers["position_safety"] += 1
        if readiness.get("quarantine_clear") is False:
            readiness_blockers["quarantine"] += 1
        if readiness.get("runtime_fresh") is False:
            readiness_blockers["runtime_stale"] += 1
        if readiness.get("manual_recovery_clear") is False:
            readiness_blockers["manual_recovery"] += 1
        uom_status = str(row.get("uom_route_status") or "").strip() or "none"
        uom_route_status_counts[uom_status] = uom_route_status_counts.get(uom_status, 0) + 1
        reason = str(row.get("uom_reject_reason") or "").strip()
        if reason:
            uom_reject_reason_counts[reason] = uom_reject_reason_counts.get(reason, 0) + 1
        target = str(row.get("uom_delegate_target") or "").strip()
        if target:
            uom_delegate_target_counts[target] = uom_delegate_target_counts.get(target, 0) + 1
    driver = classify_reliability_driver(
        records,
        min_sample_count=cfg.min_sample_count,
        threshold=cfg.bottleneck_rate_threshold,
        latency_ms_threshold=cfg.latency_ms_threshold,
    )
    return {
        "generated_at": now.isoformat(),
        "window": {"start": window_start, "end": window_end},
        "sample_count": len(records),
        "stage_counts": stage_counts,
        "drop_stage_counts": drop_stage_counts,
        "strategy_counts": strategy_counts,
        "latency_percentiles": {
            "signal_age_at_validator": _latency_summary(records, "signal_age_at_validator"),
            "queue_delay": _latency_summary(records, "queue_delay"),
            "validator_delay": _latency_summary(records, "validator_delay"),
            "quote_age_at_validator": _latency_summary(records, "quote_age_at_validator"),
            "approved_to_submit_intent": _latency_summary(records, "approved_to_submit_intent"),
            "submit_intent_to_uom": _latency_summary(records, "submit_intent_to_uom"),
            "uom_to_execution_gate": _latency_summary(records, "uom_to_execution_gate"),
            "execution_gate_to_broker_submit": _latency_summary(records, "execution_gate_to_broker_submit"),
            "broker_submit_to_ack": _latency_summary(records, "broker_submit_to_ack"),
            "ack_to_fill": _latency_summary(records, "ack_to_fill"),
            "fill_to_attribution": _latency_summary(records, "fill_to_attribution"),
            "attribution_to_pnl_replay": _latency_summary(records, "attribution_to_pnl_replay"),
        },
        "quote_missing": {
            "count": sum(quote_by_strategy.values()),
            "by_strategy": quote_by_strategy,
            "by_underlying": quote_by_underlying,
        },
        "readiness_blockers": readiness_blockers,
        "uom_route_status_counts": uom_route_status_counts,
        "uom_reject_reason_counts": uom_reject_reason_counts,
        "uom_delegate_target_counts": uom_delegate_target_counts,
        "classification": {
            "primary_bottleneck": driver,
            "needs_option_data_policy_fix": driver in {StrategyExecutionReliabilityDriver.OPTION_DATA_POLICY.value, StrategyExecutionReliabilityDriver.MIXED.value},
            "needs_validator_latency_fix": driver in {StrategyExecutionReliabilityDriver.VALIDATOR_LATENCY.value, StrategyExecutionReliabilityDriver.MIXED.value},
            "needs_uom_routing_fix": driver in {StrategyExecutionReliabilityDriver.UOM_ROUTING.value, StrategyExecutionReliabilityDriver.MIXED.value},
            "needs_execution_gate_fix": driver in {StrategyExecutionReliabilityDriver.EXECUTION_GATE.value, StrategyExecutionReliabilityDriver.MIXED.value},
            "needs_paper_broker_fix": driver in {StrategyExecutionReliabilityDriver.PAPER_BROKER.value, StrategyExecutionReliabilityDriver.MIXED.value},
            "needs_attribution_fix": driver in {StrategyExecutionReliabilityDriver.ATTRIBUTION.value, StrategyExecutionReliabilityDriver.MIXED.value},
        },
        "recommendations": _recommendations(driver),
        "chains": records,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def load_events(audit_root: Path, *, day: str | None = None) -> list[dict[str, Any]]:
    if not audit_root.exists():
        return []
    roots = [audit_root / str(day)] if day else [path for path in audit_root.iterdir() if path.is_dir()]
    rows: list[dict[str, Any]] = []
    for root in sorted(roots):
        if not root.exists():
            continue
        for path in sorted(root.glob("*.jsonl")):
            rows.extend(_read_jsonl(path))
    return rows


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    classification = report.get("classification") or {}
    lines = [
        "# Strategy Execution Reliability Evidence Gate",
        "",
        f"Generated at: `{report.get('generated_at')}`",
        "",
        "## Verdict",
        "",
        f"Primary bottleneck: `{classification.get('primary_bottleneck')}`",
        f"Sample count: `{report.get('sample_count')}`",
        "",
        "## Stage Counts",
        "",
        "| Stage | Count |",
        "|---|---:|",
    ]
    for key, value in (report.get("stage_counts") or {}).items():
        lines.append(f"| `{key}` | {value} |")
    lines.extend(["", "## Drop Stage Counts", "", "| Drop Stage | Count |", "|---|---:|"])
    for key, value in sorted((report.get("drop_stage_counts") or {}).items()):
        lines.append(f"| `{key}` | {value} |")
    lines.extend(["", "## Recommendations", ""])
    for item in report.get("recommendations") or []:
        lines.append(f"- `{item}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_strategy_execution_reliability_report(
    *,
    repo_root: str | Path = ".",
    audit_root: str | Path | None = None,
    output_json: str | Path | None = None,
    output_md: str | Path | None = None,
    day: str | None = None,
    min_sample_count: int = 20,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    audit_path = Path(audit_root) if audit_root is not None else root / "runtime" / "audit"
    records = build_chains_from_events(load_events(audit_path, day=day))
    report = build_reliability_report(
        records,
        window_start=day,
        window_end=day,
        config=StrategyExecutionReliabilityConfig(min_sample_count=min_sample_count),
    )
    out_json = Path(output_json) if output_json is not None else root / "reports" / "strategy_execution_reliability" / "strategy_execution_reliability_latest.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if output_md is not None:
        _write_markdown(report, Path(output_md))
    else:
        day_text = datetime.now(ET).strftime("%Y%m%d")
        _write_markdown(report, root / "docs" / "reports" / f"strategy_execution_reliability_{day_text}.md")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build strategy execution reliability evidence report")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--audit-root", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    parser.add_argument("--day", default=None)
    parser.add_argument("--min-sample-count", type=int, default=20)
    args = parser.parse_args()
    report = build_strategy_execution_reliability_report(
        repo_root=args.repo_root,
        audit_root=args.audit_root,
        output_json=args.output_json,
        output_md=args.output_md,
        day=args.day,
        min_sample_count=args.min_sample_count,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
