#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_jsonl(path: Path) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = str(line or "").strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    except Exception:
        return []
    return rows


def _audit_rows_for_day(audit_root: Path, day: str) -> list[Dict[str, Any]]:
    day_dir = audit_root / day
    rows: list[Dict[str, Any]] = []
    if not day_dir.exists():
        return rows
    for path in sorted(day_dir.glob("exec-*.jsonl")):
        rows.extend(_load_jsonl(path))
    return rows


def build_snapshot(
    *,
    qqq_obs: Dict[str, Any],
    index_diag: Dict[str, Any],
    audit_rows: list[Dict[str, Any]],
    day: str,
) -> Dict[str, Any]:
    symbols: dict[str, Dict[str, Any]] = {}

    qqq_state = dict(qqq_obs.get("strategy_state") or {})
    qqq_symbol = str(qqq_state.get("exec_symbol") or "").strip().upper()
    if qqq_symbol:
        symbols.setdefault(qqq_symbol, {})
        symbols[qqq_symbol]["qqq"] = {
            "is_traded": bool(qqq_state.get("is_traded", False)),
            "side": qqq_state.get("side"),
            "ready": bool(qqq_obs.get("ready", False)),
        }

    trader_view = dict(index_diag.get("trader_view_by_symbol") or {})
    conflict_report = dict(index_diag.get("conflict_report_by_symbol") or {})
    for symbol, payload in trader_view.items():
        symbol_u = str(symbol or "").strip().upper()
        symbols.setdefault(symbol_u, {})
        symbols[symbol_u]["index_super_win"] = {
            "final_state": payload.get("final_state"),
            "which_gate_blocked": payload.get("which_gate_blocked"),
            "foreign_owner_strategy_id": payload.get("foreign_owner_strategy_id"),
            "entry_attempts": payload.get("entry_attempts"),
        }
        conflict = dict(conflict_report.get(symbol_u) or {})
        if conflict:
            symbols[symbol_u]["index_conflict"] = {
                "foreign_open_order_conflicts": conflict.get("foreign_open_order_conflicts"),
                "foreign_symbol_ownership_conflicts": conflict.get("foreign_symbol_ownership_conflicts"),
                "foreign_owner_strategy_id": conflict.get("foreign_owner_strategy_id"),
            }

    latest_isolation: dict[str, Dict[str, Any]] = {}
    latest_attribution: dict[str, Dict[str, Any]] = {}
    for row in audit_rows:
        stage = str(row.get("stage") or "")
        symbol_u = str(row.get("symbol") or "").strip().upper()
        if not symbol_u:
            continue
        if stage.startswith("entry_isolation_"):
            latest_isolation[symbol_u] = {
                "stage": stage,
                "status": row.get("status"),
                "reason": row.get("reason"),
                "payload": row.get("payload") or {},
                "ts": row.get("ts"),
            }
        if stage.startswith("fill_attribution_"):
            latest_attribution[symbol_u] = {
                "stage": stage,
                "status": row.get("status"),
                "reason": row.get("reason"),
                "payload": row.get("payload") or {},
                "ts": row.get("ts"),
            }
    for symbol_u, payload in latest_isolation.items():
        symbols.setdefault(symbol_u, {})
        symbols[symbol_u]["entry_isolation"] = payload
    for symbol_u, payload in latest_attribution.items():
        symbols.setdefault(symbol_u, {})
        symbols[symbol_u]["fill_attribution"] = payload

    return {
        "generated_at_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "day": day,
        "symbols": symbols,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build multi-strategy isolation snapshot")
    parser.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--qqq-obs", default=str(ROOT / "runtime" / "state" / "qqq_intraday_observability_qqq_intraday_001.json"))
    parser.add_argument("--index-diag", default=str(ROOT / "runtime" / "state" / "index_super_win_diag_snapshot_index_super_win_001.json"))
    parser.add_argument("--out", default=str(ROOT / "runtime" / "state" / "multi_strategy_isolation_snapshot.json"))
    args = parser.parse_args()

    payload = build_snapshot(
        qqq_obs=_load_json(Path(args.qqq_obs)),
        index_diag=_load_json(Path(args.index_diag)),
        audit_rows=_audit_rows_for_day(Path(args.audit_root), str(args.day)),
        day=str(args.day),
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
