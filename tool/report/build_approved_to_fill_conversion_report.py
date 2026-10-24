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


class ApprovedToFillLossDriver(str, Enum):
    NO_BOTTLENECK = "NO_BOTTLENECK"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    POST_APPROVAL_LOSS = "POST_APPROVAL_LOSS"
    RESERVATION_BOTTLENECK = "RESERVATION_BOTTLENECK"
    EXECUTION_GATE_BOTTLENECK = "EXECUTION_GATE_BOTTLENECK"
    BROKER_SUBMISSION_BOTTLENECK = "BROKER_SUBMISSION_BOTTLENECK"
    BROKER_ACK_BOTTLENECK = "BROKER_ACK_BOTTLENECK"
    FILL_LIQUIDITY_BOTTLENECK = "FILL_LIQUIDITY_BOTTLENECK"
    ATTRIBUTION_BOTTLENECK = "ATTRIBUTION_BOTTLENECK"
    MIXED = "MIXED"


@dataclass(frozen=True)
class ApprovedToFillConfig:
    min_sample_count: int = 20
    bottleneck_rate_threshold: float = 0.20


@dataclass
class ChainAccumulator:
    strategy_id: str = ""
    strategy_instance_id: str = ""
    symbol: str = ""
    account_id: str = ""
    trace_id: str = ""
    decision_id: str = ""
    intent_id: str = ""
    client_order_id: str = ""
    lot_id: str = ""
    order_role: str = "ENTRY"
    signal_ts: datetime | None = None
    approved_ts: datetime | None = None
    submit_intent_ts: datetime | None = None
    reservation_ts: datetime | None = None
    execution_accept_ts: datetime | None = None
    broker_submit_ts: datetime | None = None
    broker_ack_ts: datetime | None = None
    first_fill_ts: datetime | None = None
    final_fill_ts: datetime | None = None
    broker_order_id: str | None = None
    ib_perm_id: str | None = None
    requested_qty: float = 0.0
    fill_qty: float = 0.0
    reject_reasons: list[str] = field(default_factory=list)
    reservation_rejected: bool = False
    execution_rejected: bool = False
    uom_route_status: str = ""
    uom_reject_reason: str = ""
    uom_shadow_mode: bool = False
    attribution_status: str = "not_applicable"
    pnl_replay_ready: bool = False


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
    for container in ("payload", "extras", "order", "fill", "event"):
        value = row.get(container)
        if isinstance(value, dict) and key in value:
            return value.get(key)
    return None


def _text(row: dict[str, Any], key: str) -> str:
    return str(_nested(row, key) or "").strip()


def _float(value: Any) -> float | None:
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
    for key in ("reject_reasons", "rejection_reasons", "reasons", "blockers", "submit_reasons"):
        value = _nested(row, key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    out: list[str] = []
    _append_unique(out, values)
    return out


def _correlation_key(row: dict[str, Any]) -> tuple[str, str] | None:
    for key in ("intent_id", "client_order_id"):
        value = _text(row, key)
        if value:
            return key, value
    trace_id = _text(row, "trace_id")
    decision_id = _text(row, "decision_id")
    if trace_id and decision_id:
        return "trace_decision", f"{trace_id}:{decision_id}"
    for key in ("broker_order_id", "ib_perm_id"):
        value = _text(row, key)
        owner = _text(row, "strategy_instance_id") or _text(row, "account_id")
        if value and owner:
            return key, f"{owner}:{value}"
    return None


def _stage(row: dict[str, Any]) -> str:
    return str(row.get("stage") or row.get("event_type") or row.get("type") or "").strip().lower()


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or _nested(row, "status") or "").strip().lower()


def _merge_identity(acc: ChainAccumulator, row: dict[str, Any]) -> None:
    for attr, key in (
        ("strategy_id", "strategy_id"),
        ("strategy_instance_id", "strategy_instance_id"),
        ("symbol", "symbol"),
        ("account_id", "account_id"),
        ("trace_id", "trace_id"),
        ("decision_id", "decision_id"),
        ("intent_id", "intent_id"),
        ("client_order_id", "client_order_id"),
        ("lot_id", "lot_id"),
        ("order_role", "order_role"),
    ):
        if not getattr(acc, attr):
            value = _text(row, key)
            if value:
                setattr(acc, attr, value)
    if acc.broker_order_id is None:
        acc.broker_order_id = _text(row, "broker_order_id") or None
    if acc.ib_perm_id is None:
        acc.ib_perm_id = _text(row, "ib_perm_id") or _text(row, "perm_id") or None
    qty = _float(_nested(row, "requested_qty") or _nested(row, "target_qty") or _nested(row, "qty"))
    if qty is not None and abs(qty) > abs(acc.requested_qty):
        acc.requested_qty = abs(qty)


def _apply_row(acc: ChainAccumulator, row: dict[str, Any]) -> None:
    _merge_identity(acc, row)
    stage = _stage(row)
    status = _status(row)
    ts = _first_ts(row.get("ts"), row.get("created_at"), _nested(row, "ts"), _nested(row, "timestamp"))
    acc.signal_ts = acc.signal_ts or _first_ts(_nested(row, "signal_ts"), _nested(row, "source_trigger_ts"))
    if "approved" in stage or status == "approved" or "decision_approved" in stage:
        acc.approved_ts = acc.approved_ts or _first_ts(_nested(row, "approved_ts"), ts)
    if "submit_intent" in stage or "intent_created" in stage:
        acc.submit_intent_ts = acc.submit_intent_ts or _first_ts(_nested(row, "submit_intent_ts"), ts)
    if stage.startswith("uom_") or _nested(row, "uom_route_status") is not None:
        status_text = _text(row, "uom_route_status") or status
        if status_text:
            acc.uom_route_status = status_text
        reason = _text(row, "uom_reject_reason") or _text(row, "reason")
        if reason:
            acc.uom_reject_reason = reason
        shadow = _bool_or_none(_nested(row, "uom_shadow_mode"))
        if shadow is not None:
            acc.uom_shadow_mode = bool(shadow)
    if "reservation" in stage or "reserve" in stage:
        acc.reservation_ts = acc.reservation_ts or _first_ts(_nested(row, "reservation_ts"), ts)
        if "reject" in stage or "reject" in status:
            acc.reservation_rejected = True
    if "execution_accept" in stage or "execution_accepted" in stage or status in {"execution_accept", "accepted"}:
        acc.execution_accept_ts = acc.execution_accept_ts or _first_ts(_nested(row, "execution_accept_ts"), ts)
    if "execution_reject" in stage or ("execution" in stage and "reject" in stage):
        acc.execution_rejected = True
    if "broker_submit" in stage or "broker_submitted" in stage:
        acc.broker_submit_ts = acc.broker_submit_ts or _first_ts(_nested(row, "broker_submit_ts"), ts)
    if "broker_ack" in stage or "order_ack" in stage or "ack" == status:
        acc.broker_ack_ts = acc.broker_ack_ts or _first_ts(_nested(row, "broker_ack_ts"), ts)
    if "fill" in stage or status in {"filled", "partially_filled"}:
        fill_ts = _first_ts(_nested(row, "first_fill_ts"), _nested(row, "fill_ts"), ts)
        if fill_ts is not None:
            acc.first_fill_ts = acc.first_fill_ts or fill_ts
            acc.final_fill_ts = fill_ts if acc.final_fill_ts is None or fill_ts > acc.final_fill_ts else acc.final_fill_ts
        fill_qty = _float(_nested(row, "fill_qty") or _nested(row, "filled_qty") or _nested(row, "qty"))
        if fill_qty is not None:
            acc.fill_qty += abs(fill_qty)
    attribution = _text(row, "attribution_status")
    if attribution:
        acc.attribution_status = attribution
    if _nested(row, "pnl_replay_ready") is not None:
        acc.pnl_replay_ready = bool(_nested(row, "pnl_replay_ready"))
    _append_unique(acc.reject_reasons, _row_reasons(row))


def _owner_identity_complete(acc: ChainAccumulator) -> bool:
    return bool(
        acc.account_id
        and acc.strategy_instance_id
        and acc.symbol
        and (acc.intent_id or acc.client_order_id)
    )


def _final_status_and_drop(acc: ChainAccumulator) -> tuple[str, str]:
    if acc.uom_shadow_mode and acc.uom_route_status in {"skipped", "delegated_shadow"}:
        return "shadow_no_broker_submit", "none"
    if acc.fill_qty > 0 and acc.requested_qty > 0 and acc.fill_qty >= acc.requested_qty:
        return "filled", "attribution" if acc.attribution_status in {"missing_owner", "ambiguous_owner", "unknown"} else "none"
    if acc.fill_qty > 0:
        return "partially_filled", "fill"
    if acc.broker_ack_ts is not None:
        return "broker_acknowledged", "fill"
    if acc.broker_submit_ts is not None:
        return "broker_submitted", "broker_ack"
    if acc.execution_rejected:
        return "execution_rejected", "execution_gate"
    if acc.execution_accept_ts is not None:
        return "execution_accepted", "broker_submit"
    if acc.reservation_rejected:
        return "reservation_rejected", "reservation"
    if acc.reservation_ts is not None:
        return "reservation_accepted", "execution_gate"
    if acc.submit_intent_ts is not None:
        return "intent_created", "reservation"
    if acc.approved_ts is not None:
        return "approved_only", "post_approval"
    return "unknown", "unknown"


def _to_record(acc: ChainAccumulator) -> dict[str, Any]:
    final_status, drop_stage = _final_status_and_drop(acc)
    requested_qty = float(acc.requested_qty or 0.0)
    fill_qty = float(acc.fill_qty or 0.0)
    fill_ratio = fill_qty / requested_qty if requested_qty > 0 else 0.0
    attribution_status = acc.attribution_status
    if fill_qty > 0 and attribution_status == "not_applicable":
        attribution_status = "attributed" if _owner_identity_complete(acc) else "missing_owner"
    return {
        "strategy_id": acc.strategy_id,
        "strategy_instance_id": acc.strategy_instance_id,
        "symbol": acc.symbol,
        "account_id": acc.account_id,
        "trace_id": acc.trace_id,
        "decision_id": acc.decision_id,
        "intent_id": acc.intent_id,
        "client_order_id": acc.client_order_id,
        "lot_id": acc.lot_id,
        "order_role": acc.order_role or "ENTRY",
        "signal_ts": acc.signal_ts.isoformat() if acc.signal_ts else None,
        "approved_ts": acc.approved_ts.isoformat() if acc.approved_ts else None,
        "submit_intent_ts": acc.submit_intent_ts.isoformat() if acc.submit_intent_ts else None,
        "reservation_ts": acc.reservation_ts.isoformat() if acc.reservation_ts else None,
        "execution_accept_ts": acc.execution_accept_ts.isoformat() if acc.execution_accept_ts else None,
        "broker_submit_ts": acc.broker_submit_ts.isoformat() if acc.broker_submit_ts else None,
        "broker_ack_ts": acc.broker_ack_ts.isoformat() if acc.broker_ack_ts else None,
        "first_fill_ts": acc.first_fill_ts.isoformat() if acc.first_fill_ts else None,
        "final_fill_ts": acc.final_fill_ts.isoformat() if acc.final_fill_ts else None,
        "final_status": final_status,
        "drop_stage": drop_stage,
        "reject_reasons": acc.reject_reasons,
        "uom_route_status": acc.uom_route_status or None,
        "uom_reject_reason": acc.uom_reject_reason or None,
        "uom_shadow_mode": bool(acc.uom_shadow_mode),
        "latency_ms": {
            "approved_to_intent": _latency_ms(acc.approved_ts, acc.submit_intent_ts),
            "intent_to_reservation": _latency_ms(acc.submit_intent_ts, acc.reservation_ts),
            "reservation_to_execution": _latency_ms(acc.reservation_ts, acc.execution_accept_ts),
            "execution_to_broker_submit": _latency_ms(acc.execution_accept_ts, acc.broker_submit_ts),
            "broker_submit_to_ack": _latency_ms(acc.broker_submit_ts, acc.broker_ack_ts),
            "ack_to_first_fill": _latency_ms(acc.broker_ack_ts, acc.first_fill_ts),
            "approved_to_first_fill": _latency_ms(acc.approved_ts, acc.first_fill_ts),
        },
        "owner_identity_complete": _owner_identity_complete(acc),
        "broker_order_id": acc.broker_order_id,
        "ib_perm_id": acc.ib_perm_id,
        "fill_qty": fill_qty,
        "requested_qty": requested_qty,
        "fill_ratio": fill_ratio,
        "attribution_status": attribution_status,
        "pnl_replay_ready": bool(acc.pnl_replay_ready and attribution_status == "attributed"),
    }


def build_chains_from_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chains: dict[tuple[str, str], ChainAccumulator] = {}
    for row in rows:
        key = _correlation_key(row)
        if key is None:
            key = ("uncorrelated", str(row.get("event_id") or row.get("ts") or uuid.uuid4()))
        acc = chains.setdefault(key, ChainAccumulator())
        _apply_row(acc, row)
    return [_to_record(acc) for acc in chains.values() if acc.approved_ts is not None or acc.submit_intent_ts is not None or acc.fill_qty > 0]


def _percentile(values: list[float], pct: float) -> float | None:
    clean = sorted(v for v in values if v is not None and not math.isnan(v))
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
    values = [float((row.get("latency_ms") or {}).get(key)) for row in records if (row.get("latency_ms") or {}).get(key) is not None]
    return {
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def _rate(num: int, denom: int) -> float:
    return float(num) / float(denom) if denom > 0 else 0.0


def classify_conversion_loss(records: list[dict[str, Any]], *, min_sample_count: int = 20, threshold: float = 0.20) -> str:
    sample_count = len(records)
    if sample_count < min_sample_count:
        return ApprovedToFillLossDriver.INSUFFICIENT_SAMPLE.value
    counts: dict[str, int] = {}
    for row in records:
        drop = str(row.get("drop_stage") or "unknown")
        counts[drop] = counts.get(drop, 0) + 1
    mapping = {
        "post_approval": ApprovedToFillLossDriver.POST_APPROVAL_LOSS.value,
        "reservation": ApprovedToFillLossDriver.RESERVATION_BOTTLENECK.value,
        "execution_gate": ApprovedToFillLossDriver.EXECUTION_GATE_BOTTLENECK.value,
        "broker_submit": ApprovedToFillLossDriver.BROKER_SUBMISSION_BOTTLENECK.value,
        "broker_ack": ApprovedToFillLossDriver.BROKER_ACK_BOTTLENECK.value,
        "fill": ApprovedToFillLossDriver.FILL_LIQUIDITY_BOTTLENECK.value,
        "attribution": ApprovedToFillLossDriver.ATTRIBUTION_BOTTLENECK.value,
    }
    drivers = [driver for stage, driver in mapping.items() if _rate(counts.get(stage, 0), sample_count) >= threshold]
    if len(drivers) > 1:
        return ApprovedToFillLossDriver.MIXED.value
    return drivers[0] if drivers else ApprovedToFillLossDriver.NO_BOTTLENECK.value


def _recommendations(driver: str) -> list[str]:
    return {
        ApprovedToFillLossDriver.NO_BOTTLENECK.value: ["continue_collecting_paper_shadow_conversion_samples"],
        ApprovedToFillLossDriver.INSUFFICIENT_SAMPLE.value: ["collect_more_approved_to_fill_samples"],
        ApprovedToFillLossDriver.POST_APPROVAL_LOSS.value: ["inspect_missing_submit_intent_after_approval"],
        ApprovedToFillLossDriver.RESERVATION_BOTTLENECK.value: ["inspect_reservation_reason_distribution"],
        ApprovedToFillLossDriver.EXECUTION_GATE_BOTTLENECK.value: ["inspect_execution_gate_reject_reasons"],
        ApprovedToFillLossDriver.BROKER_SUBMISSION_BOTTLENECK.value: ["inspect_broker_submit_failures"],
        ApprovedToFillLossDriver.BROKER_ACK_BOTTLENECK.value: ["inspect_broker_ack_missing_or_gateway_path"],
        ApprovedToFillLossDriver.FILL_LIQUIDITY_BOTTLENECK.value: ["inspect_fill_liquidity_spread_and_time_in_force"],
        ApprovedToFillLossDriver.ATTRIBUTION_BOTTLENECK.value: ["inspect_attribution_owner_fields"],
    }.get(driver, ["separate_conversion_loss_drivers_before_parameter_changes"])


def build_conversion_report(
    records: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    config: ApprovedToFillConfig | None = None,
) -> dict[str, Any]:
    cfg = config or ApprovedToFillConfig()
    now = generated_at.astimezone(ET) if generated_at else datetime.now(ET)
    stage_counts = {
        "approved": sum(1 for row in records if row.get("approved_ts")),
        "intent_created": sum(1 for row in records if row.get("submit_intent_ts")),
        "shadow_no_broker_submit": sum(1 for row in records if row.get("final_status") == "shadow_no_broker_submit"),
        "reservation_rejected": sum(1 for row in records if row.get("final_status") == "reservation_rejected"),
        "execution_rejected": sum(1 for row in records if row.get("final_status") == "execution_rejected"),
        "broker_submitted": sum(1 for row in records if row.get("broker_submit_ts")),
        "broker_acknowledged": sum(1 for row in records if row.get("broker_ack_ts")),
        "partially_filled": sum(1 for row in records if row.get("final_status") == "partially_filled"),
        "filled": sum(1 for row in records if row.get("final_status") == "filled"),
        "attributed": sum(1 for row in records if row.get("attribution_status") == "attributed"),
        "pnl_replay_ready": sum(1 for row in records if row.get("pnl_replay_ready")),
    }
    drop_stage_counts: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for row in records:
        drop = str(row.get("drop_stage") or "unknown")
        drop_stage_counts[drop] = drop_stage_counts.get(drop, 0) + 1
        sid = str(row.get("strategy_id") or "unknown")
        strategy_counts[sid] = strategy_counts.get(sid, 0) + 1
        for reason in list(row.get("reject_reasons") or []):
            reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1
    sample_count = len(records)
    driver = classify_conversion_loss(records, min_sample_count=cfg.min_sample_count, threshold=cfg.bottleneck_rate_threshold)
    return {
        "generated_at": now.isoformat(),
        "window": {"start": window_start, "end": window_end},
        "sample_count": sample_count,
        "strategy_counts": strategy_counts,
        "stage_counts": stage_counts,
        "drop_stage_counts": {
            "post_approval": drop_stage_counts.get("post_approval", 0),
            "reservation": drop_stage_counts.get("reservation", 0),
            "execution_gate": drop_stage_counts.get("execution_gate", 0),
            "broker_submit": drop_stage_counts.get("broker_submit", 0),
            "broker_ack": drop_stage_counts.get("broker_ack", 0),
            "fill": drop_stage_counts.get("fill", 0),
            "attribution": drop_stage_counts.get("attribution", 0),
        },
        "conversion_rates": {
            "approved_to_intent": _rate(stage_counts["intent_created"], stage_counts["approved"]),
            "intent_to_broker_submit": _rate(stage_counts["broker_submitted"], stage_counts["intent_created"]),
            "broker_submit_to_ack": _rate(stage_counts["broker_acknowledged"], stage_counts["broker_submitted"]),
            "ack_to_fill": _rate(stage_counts["partially_filled"] + stage_counts["filled"], stage_counts["broker_acknowledged"]),
            "fill_to_attribution": _rate(stage_counts["attributed"], stage_counts["partially_filled"] + stage_counts["filled"]),
            "approved_to_fill": _rate(stage_counts["partially_filled"] + stage_counts["filled"], stage_counts["approved"]),
            "approved_to_pnl_replay_ready": _rate(stage_counts["pnl_replay_ready"], stage_counts["approved"]),
        },
        "latency_ms": {
            "approved_to_intent": _latency_summary(records, "approved_to_intent"),
            "intent_to_broker_submit": _latency_summary(records, "execution_to_broker_submit"),
            "broker_submit_to_ack": _latency_summary(records, "broker_submit_to_ack"),
            "ack_to_first_fill": _latency_summary(records, "ack_to_first_fill"),
            "approved_to_first_fill": _latency_summary(records, "approved_to_first_fill"),
        },
        "top_reject_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
        "owner_identity_failures": {
            "missing_owner": sum(1 for row in records if row.get("attribution_status") == "missing_owner" or not row.get("owner_identity_complete")),
            "ambiguous_owner": sum(1 for row in records if row.get("attribution_status") == "ambiguous_owner"),
            "unknown_broker_order": sum(1 for row in records if row.get("attribution_status") == "unknown"),
        },
        "classification": {
            "primary_conversion_loss": driver,
            "is_reservation_bottleneck": driver in {ApprovedToFillLossDriver.RESERVATION_BOTTLENECK.value, ApprovedToFillLossDriver.MIXED.value},
            "is_execution_gate_bottleneck": driver in {ApprovedToFillLossDriver.EXECUTION_GATE_BOTTLENECK.value, ApprovedToFillLossDriver.MIXED.value},
            "is_broker_submission_bottleneck": driver in {ApprovedToFillLossDriver.BROKER_SUBMISSION_BOTTLENECK.value, ApprovedToFillLossDriver.MIXED.value},
            "is_fill_liquidity_bottleneck": driver in {ApprovedToFillLossDriver.FILL_LIQUIDITY_BOTTLENECK.value, ApprovedToFillLossDriver.MIXED.value},
            "is_attribution_bottleneck": driver in {ApprovedToFillLossDriver.ATTRIBUTION_BOTTLENECK.value, ApprovedToFillLossDriver.MIXED.value},
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
    roots = [audit_root / str(day)] if day else [path for path in audit_root.iterdir() if path.is_dir()] if audit_root.exists() else []
    rows: list[dict[str, Any]] = []
    for root in sorted(roots):
        for path in sorted(root.glob("*.jsonl")):
            rows.extend(_read_jsonl(path))
    return rows


def build_approved_to_fill_conversion_report(
    *,
    repo_root: str | Path = ".",
    audit_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    day: str | None = None,
    min_sample_count: int = 20,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    audit_path = Path(audit_root) if audit_root is not None else root / "runtime" / "audit"
    records = build_chains_from_events(load_events(audit_path, day=day))
    report = build_conversion_report(records, window_start=day, window_end=day, config=ApprovedToFillConfig(min_sample_count=min_sample_count))
    out_dir = Path(output_dir) if output_dir is not None else root / "reports" / "approved_to_fill_conversion"
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "approved_to_fill_conversion_latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build offline approved-to-fill conversion diagnostics")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--audit-root", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--day", default=None)
    parser.add_argument("--min-sample-count", type=int, default=20)
    args = parser.parse_args()
    report = build_approved_to_fill_conversion_report(
        repo_root=args.repo_root,
        audit_root=args.audit_root,
        output_dir=args.output_dir,
        day=args.day,
        min_sample_count=args.min_sample_count,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
