#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_audit_files(audit_root: Path, *, day: str, hour: str) -> Iterable[Path]:
    day_dir = audit_root / day
    if not day_dir.exists():
        return []
    return sorted(day_dir.glob(f"*-{hour.zfill(2)}.jsonl"))


def _parse_ts(raw: Any) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _group_records(
    audit_root: Path,
    *,
    day: str,
    hour: str,
    strategy_id: str,
    symbols: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    symbol_set = {str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()}
    for path in _iter_audit_files(audit_root, day=day, hour=hour):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if str(row.get("strategy_id") or "") != strategy_id:
                    continue
                if symbol_set and str(row.get("symbol") or "").upper() not in symbol_set:
                    continue
                trace_id = str(row.get("trace_id") or "").strip()
                if not trace_id:
                    continue
                groups[trace_id].append(row)
        except Exception:
            continue
    for rows in groups.values():
        rows.sort(key=lambda item: str(item.get("ts") or ""))
    return dict(groups)


def _trace_distance_seconds(rows: List[Dict[str, Any]], sample_ts: datetime) -> float:
    if not rows:
        return float("inf")
    first_ts = _parse_ts(rows[0].get("ts"))
    last_ts = _parse_ts(rows[-1].get("ts"))
    distances: List[float] = []
    for item in (first_ts, last_ts):
        if item is not None:
            distances.append(abs((item - sample_ts).total_seconds()))
    return min(distances) if distances else float("inf")


def _pick_best_trace(groups: Dict[str, List[Dict[str, Any]]], sample_ts: datetime) -> tuple[str | None, List[Dict[str, Any]]]:
    best_trace_id: str | None = None
    best_rows: List[Dict[str, Any]] = []
    best_score: tuple[int, float, int] | None = None
    for trace_id, rows in groups.items():
        preferred_rank = 0 if any(str(row.get("stage") or "").strip() in PREFERRED_TRACE_STAGES for row in rows) else 1
        score = (preferred_rank, _trace_distance_seconds(rows, sample_ts), -len(rows))
        if best_score is None or score < best_score:
            best_score = score
            best_trace_id = trace_id
            best_rows = rows
    return best_trace_id, best_rows


def export_strategy_traces_for_shadow_session(
    *,
    sample_dir: Path,
    audit_root: Path,
    output_dir: Path,
    strategy_id: str,
    symbol_aliases: Dict[str, List[str]] | None = None,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    exported_count = 0
    missing_count = 0

    sample_paths = sorted(
        [path for path in sample_dir.glob("*.json") if path.name not in {"session_report.json", "session_manifest.json"}],
        key=lambda p: p.name,
    )

    for sample_path in sample_paths:
        sample = _load_json(sample_path)
        symbol = str(sample.get("symbol") or "").strip().upper()
        captured_at_et = str(sample.get("captured_at_et") or "").strip()
        sample_ts = _parse_ts(captured_at_et)
        row: Dict[str, Any] = {
            "sample_path": str(sample_path),
            "symbol": symbol,
            "captured_at_et": captured_at_et,
            "strategy_id": strategy_id,
            "status": "missing_sample_timestamp",
        }
        if sample_ts is None:
            missing_count += 1
            rows.append(row)
            continue

        day = sample_ts.strftime("%Y-%m-%d")
        hour = sample_ts.strftime("%H")
        candidate_symbols = [symbol]
        alias_rows = list((symbol_aliases or {}).get(symbol, []) or [])
        candidate_symbols.extend(str(item or "").strip().upper() for item in alias_rows if str(item or "").strip())
        groups = _group_records(
            audit_root,
            day=day,
            hour=hour,
            strategy_id=strategy_id,
            symbols=list(dict.fromkeys(candidate_symbols)),
        )
        trace_id, trace_rows = _pick_best_trace(groups, sample_ts)
        if not trace_id or not trace_rows:
            row.update({"status": "missing_trace", "day": day, "hour": hour, "candidate_symbols": list(dict.fromkeys(candidate_symbols))})
            missing_count += 1
            rows.append(row)
            continue

        out_path = output_dir / f"{sample_path.stem}.json"
        payload = {
            "meta": {
                "sample_path": str(sample_path),
                "strategy_id": strategy_id,
                "trace_id": trace_id,
                "symbol": symbol,
                "day": day,
                "hour": hour,
                "row_count": len(trace_rows),
            },
            "events": trace_rows,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        row.update(
            {
                "status": "exported",
                "day": day,
                "hour": hour,
                "trace_id": trace_id,
                "row_count": len(trace_rows),
                "output_path": str(out_path),
                "candidate_symbols": list(dict.fromkeys(candidate_symbols)),
            }
        )
        exported_count += 1
        rows.append(row)

    report = {
        "sample_dir": str(sample_dir),
        "audit_root": str(audit_root),
        "output_dir": str(output_dir),
        "strategy_id": strategy_id,
        "sample_count": len(rows),
        "exported_count": exported_count,
        "missing_count": missing_count,
        "rows": rows,
    }
    report_path = output_dir / "export_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Export strategy audit traces for one captured shadow session")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--strategy-id", required=True)
    args = parser.parse_args()

    report = export_strategy_traces_for_shadow_session(
        sample_dir=Path(args.sample_dir),
        audit_root=Path(args.audit_root),
        output_dir=Path(args.output_dir),
        strategy_id=str(args.strategy_id),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if int(report.get("missing_count", 0)) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
PREFERRED_TRACE_STAGES = {
    "gate_evaluated",
    "entry_gate_blocked",
    "entry_submit_ready",
    "entry_intent_requested",
    "entry_intent_submitted",
    "entry_confirmed",
    "entry_terminal",
    "tp1_submitted",
    "flatten_submitted",
    "flatten_confirmed",
    "protective_stop_submitted",
    "protective_stop_filled",
}
