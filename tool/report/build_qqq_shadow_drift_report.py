#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
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


def _day_from_text(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return ""


def _snapshot_day(path: Path, row: dict[str, Any] | None = None) -> str:
    day = _day_from_text(path.name)
    if day:
        return day
    if row:
        return _day_from_text(row.get("ts_et") or row.get("ts") or row.get("generated_at"))
    return ""


def _load_signal_snapshots(snapshot_root: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(snapshot_root.glob("*.jsonl")):
        rows = _read_jsonl(path)
        for row in rows:
            day = _snapshot_day(path, row)
            if day:
                out.setdefault(day, []).append(row)
    return out


def _load_whitelist(path: Path | None) -> set[tuple[str, str]]:
    if path is None or not path.exists():
        return set()
    payload = _read_json(path)
    rows = payload.get("entries") if isinstance(payload.get("entries"), list) else payload.get("whitelist")
    approved: set[tuple[str, str]] = set()
    for row in rows or []:
        if not isinstance(row, dict) or not bool(row.get("approved", True)):
            continue
        key = str(row.get("key") or row.get("decision_key") or "").strip()
        field = str(row.get("field") or "*").strip()
        if key:
            approved.add((key, field))
    return approved


def _is_whitelisted(*, whitelist: set[tuple[str, str]], key: str, field: str) -> bool:
    return (key, field) in whitelist or (key, "*") in whitelist


def _extract_diffs(report: dict[str, Any], *, whitelist: set[tuple[str, str]], path: Path) -> list[dict[str, Any]]:
    day = str(report.get("day") or _day_from_text(path.name) or "").strip()
    out: list[dict[str, Any]] = []
    for row in report.get("rows") or []:
        if not isinstance(row, dict) or bool(row.get("matched", False)):
            continue
        key = str(row.get("key") or row.get("decision_id") or "").strip()
        row_classification = str(row.get("diff_classification") or "").strip() or None
        blocking = bool(
            row.get(
                "blocking",
                row_classification not in {"semantic_equivalent", "timing_only"} if row_classification else True,
            )
        )
        for diff in row.get("diffs") or []:
            if not isinstance(diff, dict):
                continue
            field = str(diff.get("field") or "__unknown__").strip()
            approved = _is_whitelisted(whitelist=whitelist, key=key, field=field)
            classification = str(diff.get("classification") or row_classification or "true_decision_diff").strip()
            out.append(
                {
                    "day": day,
                    "key": key,
                    "field": field,
                    "expected": diff.get("expected"),
                    "actual": diff.get("actual"),
                    "classification": classification,
                    "blocking": blocking and classification == "true_decision_diff",
                    "whitelisted": approved,
                    "source_report": str(path),
                    "needs_48h_attribution": not approved and not bool(diff.get("root_cause") or diff.get("attribution")),
                }
            )
    return out


def _load_parity_reports(parity_root: Path, *, whitelist: set[tuple[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reports: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    for path in sorted(parity_root.glob("*.json")):
        payload = _read_json(path)
        if not payload:
            continue
        if "diff_total" not in payload and "rows" not in payload:
            continue
        summary = {
            "path": str(path),
            "day": str(payload.get("day") or _day_from_text(path.name) or ""),
            "recommendation": payload.get("recommendation"),
            "diff_total": int(payload.get("diff_total") or 0),
            "match_ratio": payload.get("match_ratio"),
            "legacy_count": payload.get("legacy_count"),
            "kernel_count": payload.get("kernel_count"),
        }
        try:
            legacy_count = int(summary["legacy_count"] or 0)
        except Exception:
            legacy_count = 0
        try:
            kernel_count = int(summary["kernel_count"] or 0)
        except Exception:
            kernel_count = 0
        summary["empty_input"] = legacy_count <= 0 or kernel_count <= 0
        reports.append(summary)
        diffs.extend(_extract_diffs(payload, whitelist=whitelist, path=path))
    return reports, diffs


def build_qqq_shadow_drift_report(
    *,
    snapshot_root: str | Path = "reports/qqq_signal_snapshots",
    parity_root: str | Path = "reports/qqq_kernel_shadow_parity",
    whitelist_path: str | Path | None = None,
    min_days: int = 3,
    max_whitelist_entries: int = 5,
) -> dict[str, Any]:
    snapshot_dir = Path(snapshot_root)
    parity_dir = Path(parity_root)
    whitelist = _load_whitelist(Path(whitelist_path) if whitelist_path else None)
    snapshots_by_day = _load_signal_snapshots(snapshot_dir)
    parity_reports, diffs = _load_parity_reports(parity_dir, whitelist=whitelist)
    observed_days = sorted(set(snapshots_by_day) | {str(row.get("day") or "") for row in parity_reports if row.get("day")})
    unresolved = [row for row in diffs if not bool(row.get("whitelisted"))]
    blocking_unresolved = [row for row in unresolved if bool(row.get("blocking"))]
    needs_attribution = [row for row in blocking_unresolved if bool(row.get("needs_48h_attribution"))]
    unresolved_by_field = Counter(str(row.get("field") or "__unknown__") for row in unresolved)
    unresolved_by_source = Counter(str(row.get("source_report") or "__unknown__") for row in unresolved)
    unresolved_by_classification = Counter(str(row.get("classification") or "__unknown__") for row in unresolved)
    blocking_unresolved_by_field = Counter(str(row.get("field") or "__unknown__") for row in blocking_unresolved)
    blocking_unresolved_by_source = Counter(str(row.get("source_report") or "__unknown__") for row in blocking_unresolved)
    presence_only_count = int(unresolved_by_field.get("__presence__", 0))
    presence_only_ratio = (presence_only_count / len(unresolved)) if unresolved else 0.0
    high_input_skew = any(
        int(row.get("legacy_count") or 0) > 0
        and int(row.get("kernel_count") or 0) >= int(row.get("legacy_count") or 0) * 5
        for row in parity_reports
    )
    suspected_key_alignment_mismatch = bool(
        len(unresolved) >= 100 and presence_only_ratio >= 0.95 and high_input_skew
    )
    whitelist_count = len(whitelist)
    d3_ready = len(observed_days) >= int(min_days)
    whitelist_ok = whitelist_count <= int(max_whitelist_entries)
    empty_reports = [row for row in parity_reports if bool(row.get("empty_input"))]
    match_ratios = [
        float(row["match_ratio"])
        for row in parity_reports
        if row.get("match_ratio") is not None
    ]
    min_match_ratio = min(match_ratios) if match_ratios else None
    blockers: list[str] = []
    if not parity_reports:
        blockers.append("no_parity_report_yet")
    if empty_reports:
        blockers.append("empty_parity_report_input")
    if blocking_unresolved:
        blockers.append("unresolved_diffs_present")
    if not whitelist_ok:
        blockers.append("whitelist_limit_exceeded")
    if blockers:
        recommendation = "HOLD"
    elif not d3_ready:
        recommendation = "CONTINUE_SHADOW"
    else:
        recommendation = "PASS"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_root": str(snapshot_dir),
        "parity_root": str(parity_dir),
        "observed_days": observed_days,
        "min_days": int(min_days),
        "d3_ready": d3_ready,
        "signal_snapshot_count_by_day": {day: len(rows) for day, rows in sorted(snapshots_by_day.items())},
        "parity_report_count": len(parity_reports),
        "empty_parity_report_count": len(empty_reports),
        "min_match_ratio": min_match_ratio,
        "parity_reports": parity_reports,
        "diff_count": len(diffs),
        "unresolved_diff_count": len(unresolved),
        "blocking_unresolved_diff_count": len(blocking_unresolved),
        "whitelisted_diff_count": len(diffs) - len(unresolved),
        "whitelist_entry_count": whitelist_count,
        "max_whitelist_entries": int(max_whitelist_entries),
        "whitelist_ok": whitelist_ok,
        "needs_48h_attribution_count": len(needs_attribution),
        "unresolved_diff_count_by_field": dict(sorted(unresolved_by_field.items())),
        "unresolved_diff_count_by_classification": dict(sorted(unresolved_by_classification.items())),
        "unresolved_diff_count_by_source_report": dict(sorted(unresolved_by_source.items())),
        "blocking_unresolved_diff_count_by_field": dict(sorted(blocking_unresolved_by_field.items())),
        "blocking_unresolved_diff_count_by_source_report": dict(sorted(blocking_unresolved_by_source.items())),
        "presence_only_unresolved_count": presence_only_count,
        "presence_only_unresolved_ratio": round(presence_only_ratio, 6),
        "suspected_key_alignment_mismatch": suspected_key_alignment_mismatch,
        "unresolved_diffs": unresolved[:200],
        "blockers": blockers,
        "recommendation": recommendation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build QQQ D+3 shadow drift report")
    parser.add_argument("--snapshot-root", default="reports/qqq_signal_snapshots")
    parser.add_argument("--parity-root", default="reports/qqq_kernel_shadow_parity")
    parser.add_argument("--whitelist", default="")
    parser.add_argument("--min-days", type=int, default=3)
    parser.add_argument("--max-whitelist-entries", type=int, default=5)
    parser.add_argument("--output", default="reports/qqq_kernel_shadow_parity/qqq_shadow_drift_report_latest.json")
    parser.add_argument("--fail-on-hold", action="store_true")
    args = parser.parse_args(argv)

    report = build_qqq_shadow_drift_report(
        snapshot_root=args.snapshot_root,
        parity_root=args.parity_root,
        whitelist_path=args.whitelist or None,
        min_days=args.min_days,
        max_whitelist_entries=args.max_whitelist_entries,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "recommendation": report["recommendation"], "unresolved_diff_count": report["unresolved_diff_count"]}, sort_keys=True))
    if args.fail_on_hold and report["recommendation"] == "HOLD":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
