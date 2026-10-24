#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_STRATEGY_ID = "vegas_hma_setup_trigger_risk_v12_spy_001"


class VegasRejectionDriver(str, Enum):
    NO_BOTTLENECK = "NO_BOTTLENECK"
    LATENCY_DLB_SYSTEMIC = "LATENCY_DLB_SYSTEMIC"
    EDGE_COST_SYSTEMIC = "EDGE_COST_SYSTEMIC"
    DATA_READINESS = "DATA_READINESS"
    SYSTEM_RISK_BLOCKER = "SYSTEM_RISK_BLOCKER"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    MIXED = "MIXED"


@dataclass(frozen=True)
class VegasMeasurementConfig:
    strategy_id: str = DEFAULT_STRATEGY_ID
    symbol: str = "SPY"
    dlb_limit_ms: int = 2000
    ttl_limit_ms: int = 2000
    min_sample_count: int = 20
    systemic_rate_threshold: float = 0.20


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


def _latency_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() * 1000.0)


def _nested(row: dict[str, Any], key: str) -> Any:
    if key in row:
        return row.get(key)
    payload = row.get("payload")
    if isinstance(payload, dict) and key in payload:
        return payload.get(key)
    extras = row.get("extras")
    if isinstance(extras, dict) and key in extras:
        return extras.get(key)
    return None


def _rejection_reasons(row: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("reason", "error_type", "status", "stage"):
        values.append(row.get(key))
        values.append(_nested(row, key))
    for key in ("reasons", "result_reasons", "submit_reasons", "blockers", "rejection_reasons"):
        value = _nested(row, key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    reasons: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        reasons.append(text)
    return reasons


def _edge_cost_from_reasons(reasons: Iterable[str]) -> tuple[float | None, float | None]:
    for reason in reasons:
        text = str(reason or "")
        match = re.search(r"edge_insufficient[:=]?\s*([-+]?\d+(?:\.\d+)?)\s*<=\s*cost[:=]?\s*([-+]?\d+(?:\.\d+)?)", text)
        if match:
            return float(match.group(1)), float(match.group(2))
        match = re.search(r"edge[:=]\s*([-+]?\d+(?:\.\d+)?).*cost[:=]\s*([-+]?\d+(?:\.\d+)?)", text)
        if match:
            return float(match.group(1)), float(match.group(2))
    return None, None


def _status_from_row(row: dict[str, Any], reasons: list[str]) -> str:
    joined = " ".join(str(x).lower() for x in reasons)
    status = str(row.get("status") or _nested(row, "status") or "").strip().lower()
    stage = str(row.get("stage") or _nested(row, "stage") or "").strip().lower()
    if "stale_decision" in joined or "dlb_exceeded" in joined:
        return "stale_decision"
    if "edge_insufficient" in joined:
        return "edge_insufficient"
    if status in {"approved", "accepted", "ok"}:
        return "approved"
    if status in {"submitted", "filled"} or "submitted" in stage:
        return "submitted"
    if "validator" in stage or "validator_rejected" in joined:
        return "validator_rejected"
    if "rejected" in status or "rejected" in stage:
        return "validator_rejected"
    return status or "unknown"


def measurement_from_audit_row(row: dict[str, Any], config: VegasMeasurementConfig | None = None) -> dict[str, Any] | None:
    cfg = config or VegasMeasurementConfig()
    strategy_id = str(row.get("strategy_id") or _nested(row, "strategy_id") or "").strip()
    if strategy_id != cfg.strategy_id:
        return None
    reasons = _rejection_reasons(row)
    signal_ts = _first_ts(
        _nested(row, "signal_ts"),
        _nested(row, "source_trigger_ts"),
        _nested(row, "source_ts"),
    )
    decision_ts = _first_ts(
        _nested(row, "decision_ts"),
        _nested(row, "entry_submit_ts"),
        _nested(row, "entry_ts"),
        row.get("ts"),
    )
    validation_ts = _first_ts(_nested(row, "validation_ts"), _nested(row, "validator_ts"), row.get("ts"))
    submit_intent_ts = _first_ts(_nested(row, "submit_intent_ts"), _nested(row, "entry_submit_ts"))
    broker_submit_ts = _first_ts(_nested(row, "broker_submit_ts"), _nested(row, "broker_ts"))
    signal_to_decision_ms = _latency_ms(signal_ts, decision_ts)
    decision_to_validation_ms = _latency_ms(decision_ts, validation_ts)
    validation_to_submit_ms = _latency_ms(validation_ts, submit_intent_ts)
    signal_to_validation_ms = _latency_ms(signal_ts, validation_ts)
    signal_to_submit_ms = _latency_ms(signal_ts, submit_intent_ts)
    dlb_limit_ms = int(_float(_nested(row, "dlb_limit_ms")) or _float(_nested(row, "dlb_ms")) or cfg.dlb_limit_ms)
    ttl_limit_ms = int(_float(_nested(row, "ttl_limit_ms")) or _float(_nested(row, "ttl_ms")) or cfg.ttl_limit_ms)
    edge_reason, cost_reason = _edge_cost_from_reasons(reasons)
    edge_value = _float(_nested(row, "edge_value"))
    if edge_value is None:
        edge_value = _float(_nested(row, "edge_bp"))
    if edge_value is None:
        edge_value = edge_reason
    cost_value = _float(_nested(row, "cost_value"))
    if cost_value is None:
        cost_value = _float(_nested(row, "cost_bp"))
    if cost_value is None:
        cost_value = cost_reason
    final_status = _status_from_row(row, reasons)
    dlb_elapsed_ms = signal_to_validation_ms if signal_to_validation_ms is not None else signal_to_decision_ms
    ttl_elapsed_ms = signal_to_submit_ms if signal_to_submit_ms is not None else signal_to_validation_ms
    dlb_exceeded = bool(_nested(row, "dlb_exceeded")) or final_status == "stale_decision" or (
        dlb_elapsed_ms is not None and dlb_elapsed_ms > float(dlb_limit_ms)
    )
    ttl_exceeded = bool(_nested(row, "ttl_exceeded")) or (
        ttl_elapsed_ms is not None and ttl_elapsed_ms > float(ttl_limit_ms)
    )
    edge_minus_cost = edge_value - cost_value if edge_value is not None and cost_value is not None else None
    return {
        "strategy_id": cfg.strategy_id,
        "symbol": str(row.get("symbol") or _nested(row, "symbol") or cfg.symbol),
        "event_id": str(row.get("event_id") or _nested(row, "event_id") or row.get("decision_id") or _nested(row, "decision_id") or uuid.uuid4()),
        "trace_id": str(row.get("trace_id") or _nested(row, "trace_id") or row.get("decision_id") or _nested(row, "decision_id") or ""),
        "signal_ts": signal_ts.isoformat() if signal_ts is not None else None,
        "decision_ts": decision_ts.isoformat() if decision_ts is not None else None,
        "validation_ts": validation_ts.isoformat() if validation_ts is not None else None,
        "submit_intent_ts": submit_intent_ts.isoformat() if submit_intent_ts is not None else None,
        "broker_submit_ts": broker_submit_ts.isoformat() if broker_submit_ts is not None else None,
        "final_status": final_status,
        "rejection_reasons": reasons,
        "dlb_limit_ms": dlb_limit_ms,
        "ttl_limit_ms": ttl_limit_ms,
        "signal_to_decision_ms": signal_to_decision_ms,
        "decision_to_validation_ms": decision_to_validation_ms,
        "validation_to_submit_ms": validation_to_submit_ms,
        "signal_to_validation_ms": signal_to_validation_ms,
        "signal_to_submit_ms": signal_to_submit_ms,
        "dlb_elapsed_ms": dlb_elapsed_ms,
        "ttl_elapsed_ms": ttl_elapsed_ms,
        "dlb_exceeded": dlb_exceeded,
        "ttl_exceeded": ttl_exceeded,
        "edge_value": edge_value,
        "cost_value": cost_value,
        "edge_minus_cost": edge_minus_cost,
        "data_ready": bool(_nested(row, "data_ready")) if _nested(row, "data_ready") is not None else True,
        "tradable_entry_ready": bool(_nested(row, "tradable_entry_ready")) if _nested(row, "tradable_entry_ready") is not None else True,
        "system_ready": bool(_nested(row, "system_ready")) if _nested(row, "system_ready") is not None else True,
        "risk_ready": bool(_nested(row, "risk_ready")) if _nested(row, "risk_ready") is not None else True,
        "execution_ready": bool(_nested(row, "execution_ready")) if _nested(row, "execution_ready") is not None else True,
    }


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
    values = [float(row[key]) for row in records if row.get(key) is not None]
    return {
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def classify_rejection_driver(records: list[dict[str, Any]], *, min_sample_count: int = 20, systemic_rate_threshold: float = 0.20) -> str:
    sample_count = len(records)
    if sample_count < int(min_sample_count):
        return VegasRejectionDriver.INSUFFICIENT_SAMPLE.value
    dlb_rate = sum(1 for row in records if row.get("dlb_exceeded")) / float(sample_count)
    edge_rate = sum(1 for row in records if row.get("final_status") == "edge_insufficient") / float(sample_count)
    data_rate = sum(1 for row in records if row.get("data_ready") is False) / float(sample_count)
    system_risk_rate = sum(1 for row in records if row.get("system_ready") is False or row.get("risk_ready") is False) / float(sample_count)
    drivers: list[str] = []
    if dlb_rate >= systemic_rate_threshold:
        drivers.append(VegasRejectionDriver.LATENCY_DLB_SYSTEMIC.value)
    if edge_rate >= systemic_rate_threshold:
        drivers.append(VegasRejectionDriver.EDGE_COST_SYSTEMIC.value)
    if data_rate >= systemic_rate_threshold:
        drivers.append(VegasRejectionDriver.DATA_READINESS.value)
    if system_risk_rate >= systemic_rate_threshold:
        drivers.append(VegasRejectionDriver.SYSTEM_RISK_BLOCKER.value)
    if len(drivers) > 1:
        return VegasRejectionDriver.MIXED.value
    return drivers[0] if drivers else VegasRejectionDriver.NO_BOTTLENECK.value


def build_measurement_report(
    records: list[dict[str, Any]],
    *,
    config: VegasMeasurementConfig | None = None,
    generated_at: datetime | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, Any]:
    cfg = config or VegasMeasurementConfig()
    now = generated_at.astimezone(ET) if generated_at is not None else datetime.now(ET)
    status_counts: dict[str, int] = {}
    for row in records:
        status = str(row.get("final_status") or "unknown")
        status_counts[status] = int(status_counts.get(status, 0) + 1)
    edge_values = [float(row["edge_minus_cost"]) for row in records if row.get("edge_minus_cost") is not None]
    edge_count = sum(1 for row in records if row.get("final_status") == "edge_insufficient")
    breach_count = sum(1 for row in records if row.get("dlb_exceeded"))
    classification = classify_rejection_driver(
        records,
        min_sample_count=cfg.min_sample_count,
        systemic_rate_threshold=cfg.systemic_rate_threshold,
    )
    recommendations = _recommendations(classification)
    return {
        "generated_at": now.isoformat(),
        "strategy_id": cfg.strategy_id,
        "symbol": cfg.symbol,
        "window": {"start": window_start, "end": window_end},
        "sample_count": len(records),
        "status_counts": {
            "approved": int(status_counts.get("approved", 0)),
            "submitted": int(status_counts.get("submitted", 0)),
            "validator_rejected": int(status_counts.get("validator_rejected", 0)),
            "stale_decision": int(status_counts.get("stale_decision", 0)),
            "edge_insufficient": int(status_counts.get("edge_insufficient", 0)),
            "unknown": int(status_counts.get("unknown", 0)),
        },
        "latency_ms": {
            "signal_to_decision": _latency_summary(records, "signal_to_decision_ms"),
            "decision_to_validation": _latency_summary(records, "decision_to_validation_ms"),
            "validation_to_submit": _latency_summary(records, "validation_to_submit_ms"),
            "signal_to_validation": _latency_summary(records, "signal_to_validation_ms"),
            "signal_to_submit": _latency_summary(records, "signal_to_submit_ms"),
        },
        "dlb": {
            "limit_ms": cfg.dlb_limit_ms,
            "breach_count": breach_count,
            "breach_rate": breach_count / float(len(records)) if records else 0.0,
            "worst_elapsed_ms": max([float(row["dlb_elapsed_ms"]) for row in records if row.get("dlb_elapsed_ms") is not None], default=None),
        },
        "edge_cost": {
            "sample_count": len(edge_values),
            "edge_insufficient_count": edge_count,
            "edge_insufficient_rate": edge_count / float(len(records)) if records else 0.0,
            "edge_minus_cost_p50": _percentile(edge_values, 0.50),
            "edge_minus_cost_p10": _percentile(edge_values, 0.10),
            "edge_minus_cost_min": min(edge_values) if edge_values else None,
        },
        "classification": {
            "primary_rejection_driver": classification,
            "is_latency_systemic": classification in {VegasRejectionDriver.LATENCY_DLB_SYSTEMIC.value, VegasRejectionDriver.MIXED.value},
            "is_edge_cost_systemic": classification in {VegasRejectionDriver.EDGE_COST_SYSTEMIC.value, VegasRejectionDriver.MIXED.value},
            "needs_parameter_calibration": classification
            in {
                VegasRejectionDriver.LATENCY_DLB_SYSTEMIC.value,
                VegasRejectionDriver.EDGE_COST_SYSTEMIC.value,
                VegasRejectionDriver.MIXED.value,
            },
        },
        "recommendations": recommendations,
        "measurements": records,
    }


def _recommendations(classification: str) -> list[str]:
    if classification == VegasRejectionDriver.NO_BOTTLENECK.value:
        return ["continue_collecting_paper_shadow_measurements_before_parameter_changes"]
    if classification == VegasRejectionDriver.INSUFFICIENT_SAMPLE.value:
        return ["collect_more_samples_before_tuning"]
    if classification == VegasRejectionDriver.LATENCY_DLB_SYSTEMIC.value:
        return ["measure_p95_p99_signal_to_validation_before_ttl_or_dlb_calibration"]
    if classification == VegasRejectionDriver.EDGE_COST_SYSTEMIC.value:
        return ["calibrate_edge_cost_model_from_spread_slippage_fees_and_realized_fill_quality"]
    if classification == VegasRejectionDriver.DATA_READINESS.value:
        return ["fix_data_readiness_before_strategy_parameter_calibration"]
    if classification == VegasRejectionDriver.SYSTEM_RISK_BLOCKER.value:
        return ["fix_system_or_risk_readiness_before_strategy_parameter_calibration"]
    return ["separate_latency_edge_data_and_risk_drivers_before_parameter_changes"]


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


def load_measurements_from_audit(audit_root: Path, *, day: str | None = None, config: VegasMeasurementConfig | None = None) -> list[dict[str, Any]]:
    cfg = config or VegasMeasurementConfig()
    roots = [audit_root / str(day)] if day else [p for p in audit_root.iterdir() if p.is_dir()] if audit_root.exists() else []
    measurements: list[dict[str, Any]] = []
    for root in sorted(roots):
        for path in sorted(root.glob("strategy-*.jsonl")):
            for row in _read_jsonl(path):
                measurement = measurement_from_audit_row(row, cfg)
                if measurement is not None:
                    measurements.append(measurement)
    return measurements


def build_vegas_dlb_edge_measurement_report(
    *,
    repo_root: str | Path = ".",
    audit_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    day: str | None = None,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    symbol: str = "SPY",
    min_sample_count: int = 20,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    cfg = VegasMeasurementConfig(strategy_id=strategy_id, symbol=symbol, min_sample_count=int(min_sample_count))
    audit_path = Path(audit_root) if audit_root is not None else root / "runtime" / "audit"
    records = load_measurements_from_audit(audit_path, day=day, config=cfg)
    report = build_measurement_report(records, config=cfg, window_start=day, window_end=day)
    out_dir = Path(output_dir) if output_dir is not None else root / "reports" / "vegas_dlb_edge_measurement"
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "vegas_dlb_edge_measurement_latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build offline Vegas DLB/TTL/edge-cost measurement report")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--audit-root", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--day", default=None)
    parser.add_argument("--strategy-id", default=DEFAULT_STRATEGY_ID)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--min-sample-count", type=int, default=20)
    args = parser.parse_args()
    report = build_vegas_dlb_edge_measurement_report(
        repo_root=args.repo_root,
        audit_root=args.audit_root,
        output_dir=args.output_dir,
        day=args.day,
        strategy_id=args.strategy_id,
        symbol=args.symbol,
        min_sample_count=args.min_sample_count,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
