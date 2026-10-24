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
from tool.replay.build_expected_trace_from_shadow_sample import build_expected_trace


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_sort_key(path: Path) -> tuple[str, str]:
    stem = path.stem
    symbol, _, ts = stem.partition("_")
    return (symbol.upper(), ts or stem)


def _resolve_actual_trace_path(sample_path: Path, actual_trace_dir: Path) -> Path:
    stem = sample_path.stem
    candidate = actual_trace_dir / f"{stem}.json"
    if candidate.exists():
        return candidate
    symbol, _, _ = stem.partition("_")
    symbol = symbol.strip().upper()
    if symbol:
        matches = sorted(actual_trace_dir.glob(f"{symbol}_*.json"))
        if matches:
            return matches[-1]
    return candidate


def _shadow_trace_id(sample_path: Path, symbol: str) -> str:
    return f"shadow:{symbol}:{sample_path.stem}"


def _normalize_actual_rows_for_sample(sample_path: Path, symbol: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    trace_id = _shadow_trace_id(sample_path, symbol)
    for row in rows:
        item = dict(row)
        item["trace_id"] = trace_id
        payload = dict(item.get("payload") or {})
        payload["trace_id"] = trace_id
        item["payload"] = payload
        normalized.append(item)
    return normalized


def build_shadow_session_parity_report(
    *,
    sample_dir: Path,
    actual_trace_dir: Path,
    output_dir: Path,
    qty_tolerance: float,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_dir = output_dir / "expected_traces"
    comparisons_dir = output_dir / "comparisons"
    expected_dir.mkdir(parents=True, exist_ok=True)
    comparisons_dir.mkdir(parents=True, exist_ok=True)

    sample_paths = sorted(
        [path for path in sample_dir.glob("*.json") if path.name not in {"session_report.json", "session_manifest.json"}],
        key=_sample_sort_key,
    )

    rows: List[Dict[str, Any]] = []
    pass_count = 0
    fail_count = 0
    missing_actual_count = 0

    for sample_path in sample_paths:
        expected_trace = build_expected_trace(sample_path)
        expected_trace_path = expected_dir / f"{sample_path.stem}.expected.json"
        expected_trace_path.write_text(
            json.dumps(expected_trace, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        actual_trace_path = _resolve_actual_trace_path(sample_path, actual_trace_dir)
        sample_payload = _load_json(sample_path)
        row: Dict[str, Any] = {
            "sample_path": str(sample_path),
            "symbol": str(sample_payload.get("symbol") or "").strip().upper(),
            "captured_at_et": sample_payload.get("captured_at_et"),
            "expected_trace_path": str(expected_trace_path),
            "actual_trace_path": str(actual_trace_path),
            "status": "missing_actual_trace",
        }

        if actual_trace_path.exists():
            actual_trace = _load_json(actual_trace_path)
            actual_rows = _normalize_actual_rows_for_sample(
                sample_path,
                row["symbol"],
                list(actual_trace.get("events") or []),
            )
            comparison = compare_event_streams(
                list(expected_trace.get("events") or []),
                actual_rows,
                qty_tolerance=qty_tolerance,
            )
            comparison_path = comparisons_dir / f"{sample_path.stem}.comparison.json"
            comparison_payload = {
                "sample_path": str(sample_path),
                "expected_trace_path": str(expected_trace_path),
                "actual_trace_path": str(actual_trace_path),
                "normalized_trace_id": _shadow_trace_id(sample_path, row["symbol"]),
                "comparison": comparison,
            }
            comparison_path.write_text(
                json.dumps(comparison_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            row.update(
                {
                    "comparison_path": str(comparison_path),
                    "comparison_ok": bool(comparison.get("ok")),
                    "mismatch_count": int(comparison.get("mismatch_count", 0)),
                    "status": "pass" if bool(comparison.get("ok")) else "fail",
                }
            )
            if bool(comparison.get("ok")):
                pass_count += 1
            else:
                fail_count += 1
        else:
            missing_actual_count += 1

        rows.append(row)

    ready = fail_count == 0 and missing_actual_count == 0 and len(rows) > 0
    report = {
        "sample_dir": str(sample_dir),
        "actual_trace_dir": str(actual_trace_dir),
        "output_dir": str(output_dir),
        "sample_count": len(rows),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "missing_actual_count": missing_actual_count,
        "ready_for_cutin": ready,
        "rows": rows,
    }
    report_path = output_dir / "shadow_session_parity_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build same-symbol shadow session parity report")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--actual-trace-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    args = parser.parse_args()

    report = build_shadow_session_parity_report(
        sample_dir=Path(args.sample_dir),
        actual_trace_dir=Path(args.actual_trace_dir),
        output_dir=Path(args.output_dir),
        qty_tolerance=float(args.qty_tolerance),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("ready_for_cutin")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
