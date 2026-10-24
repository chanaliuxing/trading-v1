#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a session/day report from captured index_super_win shadow samples.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ET = ZoneInfo("America/New_York")


def _default_sample_dir(day: str) -> Path:
    return ROOT / "runtime" / "state" / "index_super_win_shadow_samples" / day


def _load_json(path: Path) -> Dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_report(sample_dir: Path) -> Dict[str, Any]:
    samples: list[Dict[str, Any]] = []
    for path in sorted(sample_dir.glob("*.json")):
        payload = _load_json(path)
        if isinstance(payload, dict):
            payload["_path"] = str(path)
            samples.append(payload)

    by_symbol: dict[str, dict[str, Any]] = {}
    for payload in samples:
        symbol = str(payload.get("symbol") or "").strip().upper() or "UNKNOWN"
        bucket = by_symbol.setdefault(
            symbol,
            {
                "sample_count": 0,
                "exit_code_counter": Counter(),
                "finding_level_counter": Counter(),
                "messages": Counter(),
                "latest_exit_code": None,
                "latest_captured_at_et": None,
                "latest_path": None,
            },
        )
        bucket["sample_count"] += 1
        exit_code = int(payload.get("exit_code", 2))
        bucket["exit_code_counter"][exit_code] += 1
        bucket["latest_exit_code"] = exit_code
        bucket["latest_captured_at_et"] = payload.get("captured_at_et")
        bucket["latest_path"] = payload.get("_path")
        for finding in list(payload.get("findings") or []):
            level = str(finding.get("level") or "UNKNOWN")
            message = str(finding.get("message") or "unknown")
            bucket["finding_level_counter"][level] += 1
            bucket["messages"][message] += 1

    summary: dict[str, Any] = {
        "sample_dir": str(sample_dir),
        "sample_count": len(samples),
        "symbols": {},
        "ready_for_cutin": True,
    }
    for symbol, bucket in sorted(by_symbol.items()):
        top_messages = bucket["messages"].most_common(10)
        ready = int(bucket["exit_code_counter"].get(2, 0)) == 0 and int(bucket["exit_code_counter"].get(1, 0)) == 0
        if not ready:
            summary["ready_for_cutin"] = False
        summary["symbols"][symbol] = {
            "sample_count": int(bucket["sample_count"]),
            "exit_code_counter": {str(k): int(v) for k, v in sorted(bucket["exit_code_counter"].items())},
            "finding_level_counter": {str(k): int(v) for k, v in sorted(bucket["finding_level_counter"].items())},
            "top_messages": [{"message": msg, "count": int(cnt)} for msg, cnt in top_messages],
            "latest_exit_code": bucket["latest_exit_code"],
            "latest_captured_at_et": bucket["latest_captured_at_et"],
            "latest_path": bucket["latest_path"],
            "ready_for_cutin": ready,
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build index_super_win shadow session report")
    parser.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    parser.add_argument("--sample-dir", default="")
    parser.add_argument("--output-path", default="")
    args = parser.parse_args()

    sample_dir = Path(args.sample_dir) if str(args.sample_dir).strip() else _default_sample_dir(args.day)
    report = build_report(sample_dir)
    if str(args.output_path).strip():
        output_path = Path(args.output_path)
    else:
        output_path = sample_dir / "session_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(str(output_path))
    return 0 if bool(report.get("ready_for_cutin", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
