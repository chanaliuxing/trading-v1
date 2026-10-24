#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.strategy_runtime_v2.parity_recorder import compare_event_streams
from tool.replay.export_strategy_traces_for_shadow_session import export_strategy_traces_for_shadow_session


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parity_trace_id(sample_name: str, symbol: str) -> str:
    return f"dual:{str(symbol or '').strip().upper()}:{sample_name}"


def _normalize_rows(rows: List[Dict[str, Any]], *, sample_name: str, symbol: str) -> List[Dict[str, Any]]:
    trace_id = _parity_trace_id(sample_name, symbol)
    normalized: List[Dict[str, Any]] = []
    for row in list(rows or []):
        item = dict(row)
        item["trace_id"] = trace_id
        payload = dict(item.get("payload") or {})
        payload["trace_id"] = trace_id
        item["payload"] = payload
        normalized.append(item)
    return normalized


def _default_symbol_aliases() -> Dict[str, List[str]]:
    return {
        "QQQ": ["TQQQ", "SQQQ"],
    }


def _sample_expects_trace(sample: Dict[str, Any]) -> bool:
    summary = dict(sample.get("summary") or {})
    if bool(summary.get("qqq_is_traded", False)):
        return True
    final_state = str(summary.get("index_final_state") or "").strip().lower()
    if final_state and final_state not in {"", "no_attempt", "none", "null"}:
        return True
    if bool(summary.get("index_entry_enabled", False)):
        return True
    return False


def build_dual_strategy_trace_parity_report(
    *,
    sample_dir: Path,
    audit_root: Path,
    output_dir: Path,
    qqq_strategy_id: str,
    replica_strategy_id: str,
    qty_tolerance: float,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    qqq_trace_dir = output_dir / "qqq_traces"
    replica_trace_dir = output_dir / "replica_traces"
    comparisons_dir = output_dir / "comparisons"
    comparisons_dir.mkdir(parents=True, exist_ok=True)

    qqq_export = export_strategy_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=qqq_trace_dir,
        strategy_id=qqq_strategy_id,
        symbol_aliases=_default_symbol_aliases(),
    )
    replica_export = export_strategy_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=replica_trace_dir,
        strategy_id=replica_strategy_id,
        symbol_aliases=_default_symbol_aliases(),
    )

    qqq_rows_by_sample = {Path(str(row.get("sample_path") or "")).name: row for row in list(qqq_export.get("rows") or [])}
    replica_rows_by_sample = {Path(str(row.get("sample_path") or "")).name: row for row in list(replica_export.get("rows") or [])}

    rows: List[Dict[str, Any]] = []
    pass_count = 0
    fail_count = 0
    missing_count = 0

    sample_paths = sorted(
        [path for path in sample_dir.glob("*.json") if path.name not in {"session_report.json", "session_manifest.json"}],
        key=lambda p: p.name,
    )
    for sample_path in sample_paths:
        sample = _load_json(sample_path)
        sample_name = sample_path.name
        symbol = str(sample.get("symbol") or "").strip().upper()
        expects_trace = _sample_expects_trace(sample)
        row: Dict[str, Any] = {
            "sample_path": str(sample_path),
            "sample_name": sample_name,
            "symbol": symbol,
            "expects_trace": expects_trace,
            "qqq_strategy_id": qqq_strategy_id,
            "replica_strategy_id": replica_strategy_id,
            "status": "missing_trace",
        }
        qqq_row = qqq_rows_by_sample.get(sample_name) or {}
        replica_row = replica_rows_by_sample.get(sample_name) or {}
        qqq_trace_raw = str(qqq_row.get("output_path") or "").strip()
        replica_trace_raw = str(replica_row.get("output_path") or "").strip()
        qqq_trace_path = Path(qqq_trace_raw) if qqq_trace_raw else None
        replica_trace_path = Path(replica_trace_raw) if replica_trace_raw else None
        row["qqq_trace_path"] = str(qqq_trace_path) if qqq_trace_path is not None else None
        row["replica_trace_path"] = str(replica_trace_path) if replica_trace_path is not None else None

        if qqq_trace_path is None or replica_trace_path is None or (not qqq_trace_path.exists()) or (not replica_trace_path.exists()):
            if expects_trace:
                missing_count += 1
                row["status"] = "missing_trace"
            else:
                row["status"] = "skipped_no_trace_expected"
            rows.append(row)
            continue

        qqq_trace = _load_json(qqq_trace_path)
        replica_trace = _load_json(replica_trace_path)
        qqq_events = _normalize_rows(list(qqq_trace.get("events") or []), sample_name=sample_path.stem, symbol=symbol)
        replica_events = _normalize_rows(list(replica_trace.get("events") or []), sample_name=sample_path.stem, symbol=symbol)
        comparison = compare_event_streams(qqq_events, replica_events, qty_tolerance=qty_tolerance)
        comparison_path = comparisons_dir / f"{sample_path.stem}.comparison.json"
        comparison_payload = {
            "sample_path": str(sample_path),
            "symbol": symbol,
            "qqq_trace_path": str(qqq_trace_path),
            "replica_trace_path": str(replica_trace_path),
            "comparison": comparison,
        }
        comparison_path.write_text(
            json.dumps(comparison_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        row.update(
            {
                "status": "pass" if bool(comparison.get("ok")) else "fail",
                "comparison_ok": bool(comparison.get("ok")),
                "mismatch_count": int(comparison.get("mismatch_count", 0)),
                "comparison_path": str(comparison_path),
            }
        )
        if bool(comparison.get("ok")):
            pass_count += 1
        else:
            fail_count += 1
        rows.append(row)

    report = {
        "sample_dir": str(sample_dir),
        "audit_root": str(audit_root),
        "output_dir": str(output_dir),
        "qqq_strategy_id": qqq_strategy_id,
        "replica_strategy_id": replica_strategy_id,
        "sample_count": len(rows),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "missing_count": missing_count,
        "ready_for_parity": len(rows) > 0 and fail_count == 0 and missing_count == 0,
        "qqq_export_report_path": str(qqq_export.get("report_path") or ""),
        "replica_export_report_path": str(replica_export.get("report_path") or ""),
        "rows": rows,
    }
    report_path = output_dir / "dual_strategy_trace_parity_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare qqq strategy traces against replica traces for the same captured samples")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--qqq-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--replica-strategy-id", default="index_super_win_001")
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    args = parser.parse_args()

    report = build_dual_strategy_trace_parity_report(
        sample_dir=Path(args.sample_dir),
        audit_root=Path(args.audit_root),
        output_dir=Path(args.output_dir),
        qqq_strategy_id=str(args.qqq_strategy_id),
        replica_strategy_id=str(args.replica_strategy_id),
        qty_tolerance=float(args.qty_tolerance),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("ready_for_parity")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
