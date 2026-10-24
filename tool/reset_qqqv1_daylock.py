#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-click reset for QQQ v1 day lock (local runtime state only).

Purpose:
- make strategy think "today has not traded yet"
- keep other runtime fields as-is
- avoid touching DB / Redis / broker state
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _today_str_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _now_iso_et() -> str:
    return datetime.now(ET).isoformat()


def _state_path(state_dir: Path, strategy_id: str) -> Path:
    return state_dir / f"qqq_intraday_{strategy_id}.json"


def reset_daylock(state_file: Path, *, day: str, reason: str) -> Dict[str, Any]:
    if state_file.exists():
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"invalid state json object: {state_file}")
    else:
        raw = {}

    raw.update(
        {
            "day": day,
            "is_traded": False,
            "success_count": 0,
            "pending_decision_id": None,
            "side": None,
            "exec_symbol": None,
            "entry_px": None,
            "stop_px": None,
            "stop_dist_pre": None,
            "stop_dist_cap": None,
            "stop_dist_cur": None,
            "last_stop_exit_at": None,
            "last_stop_exit_side": None,
            "updated_at": _now_iso_et(),
            "reason": reason,
        }
    )

    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(state_file)
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset QQQ v1 local daylock state")
    parser.add_argument("--strategy-id", default="qqq_intraday_001")
    parser.add_argument("--state-dir", default="runtime/state")
    parser.add_argument("--day", default=_today_str_et())
    parser.add_argument("--reason", default="manual_reset_for_intraday_test")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    state_file = _state_path(state_dir=state_dir, strategy_id=args.strategy_id)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "state_file": str(state_file),
                    "day": args.day,
                    "reason": args.reason,
                    "action": "preview_only",
                },
                ensure_ascii=False,
            )
        )
        return 0

    payload = reset_daylock(state_file, day=args.day, reason=args.reason)
    print(json.dumps({"state_file": str(state_file), "result": payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
