#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.options_kernel.harness import run_clickhouse_day_parity_scenario_sync

DEFAULT_STRATEGY_ID = "spy_1dte_ma10_reject_0324_001"
HARNESS_SPY_IDS = ("spy_1dte_ma10_reject_legacy_harness", "spy_1dte_ma10_reject_v2_harness")


def _load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_strategy_extras(config_path: Path, strategy_id: str) -> Dict[str, Any]:
    payload = _load_yaml(config_path)
    for row in list(payload.get("strategies") or []):
        if str((row or {}).get("id") or "").strip() != strategy_id:
            continue
        params = dict((row or {}).get("params") or {})
        extras = dict(params.get("extras") or {})
        if extras:
            return extras
    return {}


def _normalize_extras_for_harness(extras: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(extras)
    members = [str(x).strip() for x in list(out.get("shared_cap_group_strategy_ids") or []) if str(x).strip()]
    if members:
        merged = list(dict.fromkeys(members + list(HARNESS_SPY_IDS)))
        out["shared_cap_group_strategy_ids"] = merged
    return out


def _iter_days(start: str, end: str) -> List[str]:
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    if end_day < start_day:
        raise ValueError("end date must be >= start date")
    out: List[str] = []
    cur = start_day
    while cur <= end_day:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    matched_days = [row["day"] for row in results if bool(row.get("matched"))]
    mismatched_days = [row["day"] for row in results if not bool(row.get("matched"))]
    total_old = sum(int(row.get("old_count") or 0) for row in results)
    total_new = sum(int(row.get("new_count") or 0) for row in results)
    return {
        "days_total": len(results),
        "matched_days": matched_days,
        "mismatched_days": mismatched_days,
        "matched_day_count": len(matched_days),
        "mismatched_day_count": len(mismatched_days),
        "day_parity_rate": (float(len(matched_days)) / float(len(results))) if results else 1.0,
        "total_old_count": total_old,
        "total_new_count": total_new,
        "count_parity_ok": total_old == total_new,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Run date-range ClickHouse parity for legacy vs SPY MA option plugin path.")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--session-mode", default="extended", choices=["rth", "extended"])
    ap.add_argument("--config", default=str(ROOT / "config.yml"))
    ap.add_argument("--strategy-id", default=DEFAULT_STRATEGY_ID)
    ap.add_argument("--use-active-extras", action="store_true", default=True)
    ap.add_argument("--no-use-active-extras", dest="use_active_extras", action="store_false")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    strategy_extras: Dict[str, Any] = {
        "underlying": str(args.symbol).strip().upper(),
        "risk_symbol": f"OPT:{str(args.symbol).strip().upper()}:1DTE",
    }
    if bool(args.use_active_extras):
        loaded = _load_strategy_extras(Path(args.config).expanduser(), str(args.strategy_id).strip())
        if loaded:
            strategy_extras = dict(loaded)
            strategy_extras.setdefault("underlying", str(args.symbol).strip().upper())
            strategy_extras.setdefault("risk_symbol", f"OPT:{str(args.symbol).strip().upper()}:1DTE")
    strategy_extras = _normalize_extras_for_harness(strategy_extras)

    results: List[Dict[str, Any]] = []
    for day in _iter_days(args.start, args.end):
        payload = run_clickhouse_day_parity_scenario_sync(
            {
                "day": day,
                "symbol": args.symbol,
                "session_mode": args.session_mode,
                "strategy_variant": "spy_option_ma",
                "strategy_extras": dict(strategy_extras),
                "snapshot_root": f"runtime/state/decision_snapshots_spy_ma_parity/{day}_{os.getpid()}",
            }
        )
        results.append(
            {
                "day": day,
                "matched": bool(payload.get("matched")),
                "old_count": int(payload.get("old_count") or 0),
                "new_count": int(payload.get("new_count") or 0),
                "diff": payload.get("diff") or {},
            }
        )

    output = {
        "generated_at": datetime.now().isoformat(),
        "symbol": args.symbol,
        "session_mode": args.session_mode,
        "strategy_id": str(args.strategy_id).strip(),
        "strategy_extras": strategy_extras,
        "start": args.start,
        "end": args.end,
        "summary": _summarize(results),
        "days": results,
    }
    text = json.dumps(output, ensure_ascii=False, indent=2, default=str)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
