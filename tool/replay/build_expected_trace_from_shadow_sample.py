#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_expected_trace(sample_path: Path) -> Dict[str, Any]:
    payload = _load_json(sample_path)
    summary = dict(payload.get("summary") or {})
    symbol = str(payload.get("symbol") or "").strip().upper()
    trace_id = f"shadow:{symbol}:{Path(sample_path).stem}"
    events: List[Dict[str, Any]] = []

    index_final_state = str(summary.get("index_final_state") or "").strip().lower()
    gate_reason = str(summary.get("index_gate_reason") or "").strip() or None
    index_entry_enabled = bool(summary.get("index_entry_enabled", False))
    qqq_is_traded = bool(summary.get("qqq_is_traded", False))
    qqq_exec_symbol = str(summary.get("qqq_exec_symbol") or "").strip().upper()
    same_symbol_live = qqq_is_traded and qqq_exec_symbol == symbol

    if same_symbol_live and index_final_state == "blocked":
        events.append(
            {
                "stage": "entry_gate_blocked",
                "symbol": symbol,
                "trace_id": trace_id,
                "reason": gate_reason or "same_symbol_live_blocked",
                "payload": {"reason": gate_reason or "same_symbol_live_blocked"},
            }
        )
    elif index_entry_enabled and index_final_state in {"no_attempt", "", "idle"}:
        events.append(
            {
                "stage": "entry_gate_allowed",
                "symbol": symbol,
                "trace_id": trace_id,
                "reason": "entry_enabled_no_conflict",
                "payload": {"reason": "entry_enabled_no_conflict"},
            }
        )
    elif index_final_state == "entered":
        events.append(
            {
                "stage": "entry_intent_submitted",
                "symbol": symbol,
                "trace_id": trace_id,
                "reason": "entered",
                "payload": {"reason": "entered"},
            }
        )
    else:
        events.append(
            {
                "stage": "entry_gate_blocked",
                "symbol": symbol,
                "trace_id": trace_id,
                "reason": gate_reason or "shadow_inconclusive_block",
                "payload": {"reason": gate_reason or "shadow_inconclusive_block"},
            }
        )

    return {
        "meta": {
            "source_sample": str(sample_path),
            "symbol": symbol,
            "same_symbol_live": same_symbol_live,
        },
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build expected parity trace from one captured shadow sample")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()

    report = build_expected_trace(Path(args.sample))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
