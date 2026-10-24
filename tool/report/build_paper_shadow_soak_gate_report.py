#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

LIVE_LEAK_PATTERNS = (
    "127.0.0.1:4002",
    "127.0.0.1:4001",
    "Connected to IBKR",
    "Connecting to 127.0.0.1",
    "account snapshot",
    "position snapshot",
    "execution snapshot",
    "execDetails",
    "updatePortfolio",
)

REAL_MUTATION_MARKERS = (
    "real_order_submit",
    "real_broker_order",
    "real_cancel",
    "real_modify",
    "placeOrder",
    "cancelOrder",
    "modifyOrder",
)

OWNER_REQUIRED_FIELDS = ("account_id", "strategy_id", "strategy_instance_id", "symbol", "order_role")


@dataclass(frozen=True)
class SoakGateConfig:
    mode: str = "PAPER_AND_SHADOW"
    default_ttl_sec: int = 90
    diagnostics_ttl_sec: int = 24 * 60 * 60
    report_day: str = "20260512"


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


def _now_et(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(tz=ET)
    return now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return rows
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _generated_at(payload: dict[str, Any] | None) -> datetime | None:
    if not payload:
        return None
    for key in ("generated_at", "timestamp", "ts", "created_at"):
        parsed = _parse_ts(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _artifact_status(path: Path | None, *, now: datetime, ttl_sec: int) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"exists": False, "fresh": False, "age_sec": None, "ttl_sec": ttl_sec, "status": "MISSING"}
    if path.suffix == ".jsonl":
        rows = _load_jsonl(path)
        payload = rows[-1] if rows else None
    else:
        payload = _load_json(path)
    ts = _generated_at(payload)
    if ts is None:
        return {"exists": True, "fresh": False, "age_sec": None, "ttl_sec": ttl_sec, "status": "FAIL"}
    age = max(0.0, (now - ts).total_seconds())
    fresh = age <= ttl_sec
    return {
        "exists": True,
        "fresh": fresh,
        "age_sec": round(age, 3),
        "ttl_sec": ttl_sec,
        "status": "PASS" if fresh else "FAIL",
        "path": str(path),
    }


def _latest_path(root: Path, patterns: Iterable[str]) -> Path | None:
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(root.glob(pattern))
    matches = [path for path in matches if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _artifact_paths(repo_root: Path) -> dict[str, tuple[Path | None, int]]:
    readiness = repo_root / "runtime" / "readiness"
    return {
        "broker_truth_snapshot": (readiness / "broker_truth_snapshot_latest.json", 90),
        "broker_truth_live_or_paper_snapshot": (
            _latest_path(
                repo_root,
                (
                    "runtime/readiness/broker_truth_live_snapshot_latest.json",
                    "runtime/readiness/paper_broker_truth_snapshot_latest.json",
                    "runtime/readiness/broker_truth_snapshot_latest.json",
                ),
            ),
            90,
        ),
        "position_safety": (readiness / "position_safety_latest.json", 90),
        "production_state": (readiness / "production_state_latest.json", 90),
        "strategy_tradability_heartbeat": (
            _latest_path(repo_root, ("runtime/readiness/strategy_tradability_heartbeat*.jsonl", "reports/**/strategy_tradability_heartbeat*.jsonl")),
            90,
        ),
        "runtime_version": (
            _latest_path(repo_root, ("runtime/readiness/runtime_version*.json", "reports/**/runtime_version*.json")),
            90,
        ),
        "strategy_surface": (
            _latest_path(repo_root, ("reports/strategy_surface/live_strategy_surface_report*.json",)),
            24 * 60 * 60,
        ),
        "approved_to_fill_conversion": (
            repo_root / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
            24 * 60 * 60,
        ),
        "vegas_dlb_edge_measurement": (
            repo_root / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
            24 * 60 * 60,
        ),
    }


def _iter_payloads(repo_root: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    candidate_paths: list[Path] = []
    audit_root = repo_root / "runtime" / "audit"
    audit_files = [path for path in audit_root.glob("*") if path.is_file() and path.suffix in {".json", ".jsonl"}]
    audit_size = sum(path.stat().st_size for path in audit_files)
    if len(audit_files) <= 100 and audit_size <= 10 * 1024 * 1024:
        candidate_paths.extend(audit_files)
    else:
        candidate_paths.extend(audit_root.glob("paper_shadow_soak*.jsonl"))
        candidate_paths.extend(audit_root.glob("paper_shadow_runtime*.jsonl"))
    candidate_paths.extend((repo_root / "runtime" / "readiness").glob("*latest.json"))
    candidate_paths.extend((repo_root / "runtime" / "readiness").glob("strategy_tradability_heartbeat*.jsonl"))
    candidate_paths.append(repo_root / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json")
    candidate_paths.append(repo_root / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json")
    latest_surface = _latest_path(repo_root, ("reports/strategy_surface/live_strategy_surface_report*.json",))
    if latest_surface is not None:
        candidate_paths.append(latest_surface)

    for path in candidate_paths:
        if not path.exists() or not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        if path.suffix == ".jsonl":
            for row in _load_jsonl(path):
                yield path, row
        else:
            payload = _load_json(path)
            if payload is not None:
                yield path, payload


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _contains_any(payload: Any, markers: Iterable[str]) -> bool:
    joined = " ".join(str(item) for item in _walk(payload))
    return any(marker in joined for marker in markers)


def _is_intent(payload: dict[str, Any]) -> bool:
    stage = str(payload.get("stage") or payload.get("event_type") or payload.get("type") or "").lower()
    role = str(payload.get("order_role") or payload.get("role") or "").upper()
    mode = str(payload.get("mode") or payload.get("execution_mode") or "").upper()
    return (
        "submit_intent" in stage
        or "paper_intent" in stage
        or "shadow_intent" in stage
        or (role and mode in {"PAPER", "SHADOW", "PAPER_ONLY", "SHADOW_ONLY"})
    )


def _owner_identity_complete(payload: dict[str, Any]) -> bool:
    has_core = all(str(payload.get(field) or "").strip() for field in OWNER_REQUIRED_FIELDS)
    has_id = bool(str(payload.get("intent_id") or payload.get("client_order_id") or "").strip())
    return has_core and has_id


def _number(value: Any) -> float:
    try:
        fval = float(value)
    except Exception:
        return 0.0
    if math.isnan(fval) or math.isinf(fval):
        return 0.0
    return fval


def _oms_invariants(repo_root: Path) -> tuple[dict[str, Any], list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    ghost_count = 0
    orphan_count = 0
    unknown_owner_count = 0
    manual_review_count = 0
    symbol_only_attribution_count = 0
    order_ref_heuristic_owner_count = 0
    owner_intent_count = 0

    for _path, payload in _iter_payloads(repo_root):
        text_values = " ".join(str(item).lower() for item in _walk(payload))
        ghost_count += int(_number(payload.get("ghost_count") or payload.get("ghosts_found")))
        orphan_count += int(_number(payload.get("orphan_count") or payload.get("orphans_found")))
        unknown_owner_count += int(_number(payload.get("unknown_owner_count")))
        manual_review_count += int(_number(payload.get("manual_review_count") or payload.get("manual_review_required_count")))
        if payload.get("attribution_path") == "symbol" or payload.get("attribution_status") == "symbol_only":
            symbol_only_attribution_count += 1
        if "order_ref_prefix" in text_values or "order_ref_substring" in text_values:
            order_ref_heuristic_owner_count += 1
        if str(payload.get("owner_status") or "").upper() == "UNKNOWN_OWNER" or "unknown_owner" in text_values:
            unknown_owner_count += 1
        if "manual_review" in text_values or "manual_required" in text_values:
            manual_review_count += 1
        if str(payload.get("owner_status") or "").upper() == "UNKNOWN_OWNER" and payload.get("auto_cancel") is True:
            blockers.append("unknown_owner_auto_cancel")
        if str(payload.get("owner_status") or "").upper() == "UNKNOWN_OWNER" and payload.get("auto_flatten") is True:
            blockers.append("unknown_owner_auto_flatten")
        if payload.get("broker_aggregate_overwrote_virtual_position") is True:
            blockers.append("broker_aggregate_overwrite")
        if payload.get("strategy_flatten_used_broker_aggregate") is True:
            blockers.append("strategy_flatten_used_broker_aggregate")
        if _is_intent(payload):
            owner_intent_count += 1
            if not _owner_identity_complete(payload):
                blockers.append("missing_owner_identity")

    if symbol_only_attribution_count:
        blockers.append("symbol_only_attribution")
    if order_ref_heuristic_owner_count:
        blockers.append("order_ref_heuristic_owner")
    if unknown_owner_count and not manual_review_count:
        warnings.append("unknown_owner_without_manual_review_evidence")
    if owner_intent_count == 0:
        warnings.append("no_paper_or_shadow_intents_found")

    return (
        {
            "ghost_count": ghost_count,
            "orphan_count": orphan_count,
            "unknown_owner_count": unknown_owner_count,
            "manual_review_count": manual_review_count,
            "symbol_only_attribution_count": symbol_only_attribution_count,
            "order_ref_heuristic_owner_count": order_ref_heuristic_owner_count,
            "paper_shadow_intent_count": owner_intent_count,
        },
        blockers,
        warnings,
    )


def _diagnostic_payload(repo_root: Path, name: str) -> dict[str, Any]:
    path = {
        "approved_to_fill": repo_root / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
        "vegas_measurement": repo_root / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
    }.get(name)
    if path is None:
        return {}
    return _load_json(path) or {}


def build_soak_gate_report(
    *,
    repo_root: Path,
    mode: str = "PAPER_AND_SHADOW",
    now: datetime | None = None,
    report_day: str = "20260512",
) -> dict[str, Any]:
    root = Path(repo_root)
    now_et = _now_et(now)
    artifact_status: dict[str, Any] = {}
    blockers: list[str] = []
    warnings: list[str] = []

    for name, (path, ttl) in _artifact_paths(root).items():
        status = _artifact_status(path, now=now_et, ttl_sec=ttl)
        artifact_status[name] = status
        if status["status"] == "MISSING":
            blockers.append(f"missing_artifact:{name}")
        elif status["status"] == "FAIL":
            blockers.append(f"stale_or_invalid_artifact:{name}")

    live_leak = False
    real_mutation = False
    for _path, payload in _iter_payloads(root):
        live_leak = live_leak or _contains_any(payload, LIVE_LEAK_PATTERNS)
        real_mutation = real_mutation or _contains_any(payload, REAL_MUTATION_MARKERS)
    if live_leak:
        blockers.append("live_broker_leak_evidence")
    if real_mutation:
        blockers.append("real_order_cancel_modify_marker")

    invariants, invariant_blockers, invariant_warnings = _oms_invariants(root)
    blockers.extend(invariant_blockers)
    warnings.extend(invariant_warnings)

    diagnostics_present = bool(
        artifact_status.get("approved_to_fill_conversion", {}).get("exists")
        and artifact_status.get("vegas_dlb_edge_measurement", {}).get("exists")
    )
    if not diagnostics_present:
        blockers.append("diagnostics_missing")

    missing_required = any(str(item).startswith("missing_artifact:") for item in blockers)
    fail_blockers = [item for item in blockers if not item.startswith("missing_artifact:")]
    if fail_blockers:
        verdict = "FAIL"
    elif missing_required:
        verdict = "INSUFFICIENT_DATA"
    else:
        verdict = "PASS"

    start = None
    end = None
    generated_times = []
    for status in artifact_status.values():
        path_text = status.get("path")
        payload = _load_json(Path(path_text)) if path_text and str(path_text).endswith(".json") else None
        ts = _generated_at(payload)
        if ts:
            generated_times.append(ts)
    if generated_times:
        start = min(generated_times)
        end = max(generated_times)
    duration_minutes = round(max(0.0, ((end or now_et) - (start or now_et)).total_seconds() / 60.0), 3)

    report = {
        "generated_at": now_et.isoformat(),
        "mode": mode,
        "window": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "duration_minutes": duration_minutes,
        },
        "verdict": verdict,
        "summary": {
            "artifact_freshness_pass": not any(status["status"] in {"FAIL", "MISSING"} for status in artifact_status.values()),
            "production_state_explainable": artifact_status.get("production_state", {}).get("exists", False),
            "owner_identity_pass": "missing_owner_identity" not in blockers,
            "oms_invariants_pass": not invariant_blockers,
            "diagnostics_present": diagnostics_present,
            "no_live_leak": not live_leak,
        },
        "artifact_status": artifact_status,
        "oms_invariants": invariants,
        "strategy_status": {},
        "approved_to_fill": _diagnostic_payload(root, "approved_to_fill"),
        "vegas_measurement": _diagnostic_payload(root, "vegas_measurement"),
        "blockers": blockers,
        "warnings": warnings,
        "next_gate": "Runtime Paper/Shadow Soak Execution" if verdict == "INSUFFICIENT_DATA" else "Canary Readiness Manual Gate",
    }
    out_dir = root / "reports" / "paper_shadow_soak_gate"
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "paper_shadow_soak_gate_latest.json"
    latest.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    docs_dir = root / "docs" / "reports"
    docs_dir.mkdir(parents=True, exist_ok=True)
    md_path = docs_dir / f"paper_shadow_soak_gate_{report_day}.md"
    md_path.write_text(_markdown_report(report), encoding="utf-8")
    return report


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Paper/Shadow Soak Gate - {str(report.get('generated_at') or '')[:10]}",
        "",
        f"Verdict: **{report.get('verdict')}**",
        "",
        "## Summary",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for key, value in (report.get("summary") or {}).items():
        lines.append(f"| `{key}` | `{value}` |")
    lines.extend(["", "## Blockers", ""])
    blockers = report.get("blockers") or []
    lines.extend([f"- `{item}`" for item in blockers] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    warnings = report.get("warnings") or []
    lines.extend([f"- `{item}`" for item in warnings] or ["- none"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--mode", default="PAPER_AND_SHADOW", choices=("PAPER_ONLY", "SHADOW_ONLY", "PAPER_AND_SHADOW"))
    parser.add_argument("--day", default="20260512")
    args = parser.parse_args()
    report = build_soak_gate_report(repo_root=Path(args.repo_root), mode=args.mode, report_day=args.day)
    print(json.dumps({"verdict": report["verdict"], "blockers": report["blockers"]}, sort_keys=True))
    return 0 if report["verdict"] in {"PASS", "INSUFFICIENT_DATA"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
