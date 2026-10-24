#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ET = ZoneInfo("America/New_York")


def _repo_root() -> Path:
    return REPO_ROOT


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _initial_state() -> dict[str, Any]:
    return {
        "paper_clean_days": 0,
        "paper_clean_day_ids": [],
        "qqq_shadow_observed_days": [],
        "live_cutover_allowed": False,
        "last_recommendation": "UNKNOWN",
        "blockers": [],
        "history": [],
    }


def _unique_sorted(values: list[Any]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def evaluate_rollout_state(
    *,
    evidence_path: str | Path,
    state_path: str | Path | None = None,
    output_path: str | Path | None = None,
    min_paper_clean_days: int = 3,
) -> dict[str, Any]:
    root = _repo_root()
    evidence_file = Path(evidence_path)
    if not evidence_file.is_absolute():
        evidence_file = root / evidence_file
    state_file = Path(state_path) if state_path is not None else root / "runtime/state/trading_rollout_state.json"
    if not state_file.is_absolute():
        state_file = root / state_file
    output_file = Path(output_path) if output_path is not None else state_file
    if not output_file.is_absolute():
        output_file = root / output_file

    evidence = _read_json(evidence_file)
    state = _initial_state()
    state.update(_read_json(state_file))

    day = str(evidence.get("day") or "").strip()
    recommendation = str(evidence.get("recommendation") or "UNKNOWN").strip()
    blockers = _unique_sorted(list(evidence.get("blockers") or []))
    gates = dict(evidence.get("gates") or {})
    qqq_gate = dict(gates.get("qqq_shadow_drift") or {})
    observed_days = _unique_sorted(list(state.get("qqq_shadow_observed_days") or []) + list(qqq_gate.get("observed_days") or []))

    paper_clean_days = _unique_sorted(list(state.get("paper_clean_day_ids") or []))
    if day:
        if bool(evidence.get("paper_clean_candidate")):
            if day not in paper_clean_days:
                paper_clean_days.append(day)
        else:
            paper_clean_days = [value for value in paper_clean_days if value != day]
        paper_clean_days = sorted(paper_clean_days)

    live_cutover_allowed = (
        len(paper_clean_days) >= int(min_paper_clean_days)
        and bool(evidence.get("live_cutover_candidate"))
        and not blockers
    )

    history = list(state.get("history") or [])
    if day:
        history = [row for row in history if dict(row).get("day") != day]
        history.append(
            {
                "day": day,
                "evaluated_at": datetime.now(ET).isoformat(timespec="seconds"),
                "recommendation": recommendation,
                "paper_clean_candidate": bool(evidence.get("paper_clean_candidate")),
                "live_cutover_candidate": bool(evidence.get("live_cutover_candidate")),
                "blockers": blockers,
                "qqq_observed_days": list(qqq_gate.get("observed_days") or []),
            }
        )
        history = sorted(history, key=lambda row: str(dict(row).get("day") or ""))[-30:]

    evaluated_at = datetime.now(ET).isoformat(timespec="seconds")
    updated = {
        **state,
        "generated_at": evaluated_at,
        "updated_at": evaluated_at,
        "last_evidence_path": str(evidence_file),
        "last_day": day,
        "last_recommendation": recommendation,
        "blockers": blockers,
        "paper_clean_day_ids": paper_clean_days,
        "paper_clean_days": len(paper_clean_days),
        "qqq_shadow_observed_days": observed_days,
        "qqq_shadow_observed_day_count": len(observed_days),
        "live_cutover_allowed": live_cutover_allowed,
        "min_paper_clean_days": int(min_paper_clean_days),
        "history": history,
    }
    _write_json(output_file, updated)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate trading rollout state from daily evidence")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--state", default="runtime/state/trading_rollout_state.json")
    parser.add_argument("--output", default="")
    parser.add_argument("--min-paper-clean-days", type=int, default=3)
    args = parser.parse_args(argv)
    state = evaluate_rollout_state(
        evidence_path=args.evidence,
        state_path=args.state,
        output_path=args.output or None,
        min_paper_clean_days=args.min_paper_clean_days,
    )
    print(
        json.dumps(
            {
                "last_day": state["last_day"],
                "last_recommendation": state["last_recommendation"],
                "paper_clean_days": state["paper_clean_days"],
                "qqq_shadow_observed_day_count": state["qqq_shadow_observed_day_count"],
                "live_cutover_allowed": state["live_cutover_allowed"],
                "blocker_count": len(state["blockers"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
