#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.replay.export_strategy_traces_for_shadow_session import _iter_audit_files, _parse_ts

TRACE_PRIORITY_STAGES = {
    "gate_evaluated": 10,
    "entry_submit_ready": 20,
    "entry_intent_requested": 30,
    "entry_intent_submitted": 40,
    "entry_confirmed": 50,
    "tp1_submitted": 60,
    "flatten_submit_requested": 70,
    "flatten_submitted": 80,
    "flatten_confirmed": 90,
    "protective_stop_submitted": 100,
    "protective_stop_filled": 110,
}
DEFAULT_INCLUDE_STAGES = tuple(TRACE_PRIORITY_STAGES.keys())
UNDERLIER_ALIASES = {
    "TQQQ": "QQQ",
    "SQQQ": "QQQ",
}


def _load_rows(audit_root: Path, *, day: str, strategy_id: str, include_stages: set[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    day_dir = audit_root / day
    if not day_dir.exists():
        return rows
    for hour in range(24):
        for path in _iter_audit_files(audit_root, day=day, hour=str(hour).zfill(2)):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if str(row.get("strategy_id") or "") != strategy_id:
                        continue
                    stage = str(row.get("stage") or "").strip()
                    if include_stages and stage not in include_stages:
                        continue
                    if not str(row.get("trace_id") or "").strip():
                        continue
                    rows.append(row)
            except Exception:
                continue
    rows.sort(key=lambda item: str(item.get("ts") or ""))
    return rows


def _sample_symbol(symbol: str, *, normalize_underlier: bool, underlier_aliases: Dict[str, str]) -> str:
    symbol_u = str(symbol or "").strip().upper()
    if normalize_underlier:
        return underlier_aliases.get(symbol_u, symbol_u)
    return symbol_u


def _pick_anchor_row(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ranked = sorted(
        rows,
        key=lambda item: (
            TRACE_PRIORITY_STAGES.get(str(item.get("stage") or "").strip(), 9999),
            str(item.get("ts") or ""),
        ),
    )
    return ranked[0]


def build_trace_parity_samples(
    *,
    audit_root: Path,
    day: str,
    output_dir: Path,
    strategy_id: str,
    summary_prefix: str = "baseline",
    source_type: str = "trace_audit",
    underlier_aliases: Dict[str, str] | None = None,
    include_stages: Iterable[str] = DEFAULT_INCLUDE_STAGES,
    normalize_underlier: bool = True,
) -> Dict[str, Any]:
    include_stage_set = {str(item).strip() for item in include_stages if str(item).strip()}
    rows = _load_rows(audit_root, day=day, strategy_id=strategy_id, include_stages=include_stage_set)
    output_dir.mkdir(parents=True, exist_ok=True)
    alias_map = {str(k or "").strip().upper(): str(v or "").strip().upper() for k, v in dict(underlier_aliases or UNDERLIER_ALIASES).items() if str(k or "").strip() and str(v or "").strip()}
    prefix = str(summary_prefix or "baseline").strip().lower() or "baseline"
    source_type_val = str(source_type or "trace_audit").strip() or "trace_audit"

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        trace_id = str(row.get("trace_id") or "").strip()
        grouped.setdefault(trace_id, []).append(row)

    result_rows: List[Dict[str, Any]] = []
    for trace_id, trace_rows in sorted(grouped.items(), key=lambda item: min(str(r.get("ts") or "") for r in item[1])):
        anchor = _pick_anchor_row(trace_rows)
        sample_ts = _parse_ts(anchor.get("ts"))
        if sample_ts is None:
            continue
        raw_symbol = str(anchor.get("symbol") or "").strip().upper()
        sample_symbol = _sample_symbol(raw_symbol, normalize_underlier=normalize_underlier, underlier_aliases=alias_map)
        trigger_id = str(anchor.get("trigger_id") or "").strip() or None
        decision_id = str(anchor.get("decision_id") or "").strip() or None
        stages = [str(item.get("stage") or "").strip() for item in trace_rows]
        payload = {
            "symbol": sample_symbol,
            "captured_at_et": sample_ts.isoformat(),
            "captured_day_et": day,
            "summary": {
                f"{prefix}_is_traded": True,
                f"{prefix}_exec_symbol": raw_symbol or None,
                "index_entry_enabled": True,
                "index_final_state": "trace_expected",
            },
            "source": {
                "type": source_type_val,
                "strategy_id": strategy_id,
                "trace_id": trace_id,
                "trigger_id": trigger_id,
                "decision_id": decision_id,
                "anchor_stage": str(anchor.get("stage") or "").strip(),
                "anchor_symbol": raw_symbol or None,
                "trace_stage_count": len(stages),
                "trace_stages": stages,
            },
        }
        output_path = output_dir / f"{sample_symbol}_{sample_ts.strftime('%Y%m%dT%H%M%S_%f')}_{trace_id[:8]}.json"
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result_rows.append(
            {
                "trace_id": trace_id,
                "sample_path": str(output_path),
                "captured_at_et": payload["captured_at_et"],
                "symbol": sample_symbol,
                "anchor_stage": payload["source"]["anchor_stage"],
                "anchor_symbol": raw_symbol,
                "row_count": len(trace_rows),
            }
        )

    manifest = {
        "audit_root": str(audit_root),
        "day": day,
        "output_dir": str(output_dir),
        "strategy_id": strategy_id,
        "summary_prefix": prefix,
        "source_type": source_type_val,
        "underlier_aliases": alias_map,
        "include_stages": sorted(include_stage_set),
        "normalize_underlier": bool(normalize_underlier),
        "sample_count": len(result_rows),
        "rows": result_rows,
    }
    manifest_path = output_dir / "session_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def build_qqq_trace_parity_samples(
    *,
    audit_root: Path,
    day: str,
    output_dir: Path,
    strategy_id: str,
    include_stages: Iterable[str] = DEFAULT_INCLUDE_STAGES,
    normalize_underlier: bool = True,
) -> Dict[str, Any]:
    return build_trace_parity_samples(
        audit_root=audit_root,
        day=day,
        output_dir=output_dir,
        strategy_id=strategy_id,
        summary_prefix="qqq",
        source_type="qqq_trace_audit",
        underlier_aliases=UNDERLIER_ALIASES,
        include_stages=include_stages,
        normalize_underlier=normalize_underlier,
    )


def _default_output_dir(day: str) -> Path:
    return ROOT / "runtime" / "state" / "index_super_win_shadow_samples" / day / "qqq_trace_seeded"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build parity samples from qqq trace-bearing audit rows")
    parser.add_argument("--day", required=True)
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--strategy-id", default="qqq_intraday_001")
    parser.add_argument("--no-normalize-underlier", action="store_true")
    parser.add_argument("--include-stage", action="append", default=[])
    args = parser.parse_args()

    include_stages = tuple(args.include_stage) if args.include_stage else DEFAULT_INCLUDE_STAGES
    output_dir = Path(args.output_dir) if str(args.output_dir).strip() else _default_output_dir(str(args.day))
    manifest = build_qqq_trace_parity_samples(
        audit_root=Path(args.audit_root),
        day=str(args.day),
        output_dir=output_dir,
        strategy_id=str(args.strategy_id),
        include_stages=include_stages,
        normalize_underlier=not bool(args.no_normalize_underlier),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if int(manifest.get("sample_count", 0)) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
