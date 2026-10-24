#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.qqq_intraday_time_logic import parse_hms_tuple


@dataclass(frozen=True)
class Finding:
    level: str
    message: str
    details: Dict[str, Any]


def _load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _minutes_in_window(start_hms: tuple[int, int, int], end_hms: tuple[int, int, int]) -> set[int]:
    start_min = start_hms[0] * 60 + start_hms[1]
    end_min = end_hms[0] * 60 + end_hms[1]
    if start_min <= end_min:
        return set(range(start_min, end_min + 1))
    return set(range(start_min, 1440)) | set(range(0, end_min + 1))


def build_session_bundle_report(*, config_paths: List[Path]) -> Dict[str, Any]:
    findings: List[Finding] = []
    strategy_ids: set[str] = set()
    runtime_scopes: set[str] = set()
    snapshot_paths: set[str] = set()
    windows: list[tuple[str, set[int]]] = []
    for path in config_paths:
        payload = _load_yaml(path)
        strategy_id = str(payload.get("strategy_id") or "").strip()
        runtime_scope = str(payload.get("runtime_scope") or "").strip()
        runtime_snapshot_path = str(payload.get("runtime_snapshot_path") or "").strip()
        session_name = str(payload.get("session_name") or "").strip()
        start_raw = str(payload.get("session_start_et") or "").strip()
        end_raw = str(payload.get("session_end_et") or "").strip()
        if not strategy_id:
            findings.append(Finding("FAIL", "strategy_id missing", {"path": str(path)}))
        elif strategy_id in strategy_ids:
            findings.append(Finding("FAIL", "duplicate strategy_id", {"path": str(path), "strategy_id": strategy_id}))
        else:
            strategy_ids.add(strategy_id)
        if not runtime_scope:
            findings.append(Finding("FAIL", "runtime_scope missing", {"path": str(path)}))
        elif runtime_scope in runtime_scopes:
            findings.append(Finding("FAIL", "duplicate runtime_scope", {"path": str(path), "runtime_scope": runtime_scope}))
        else:
            runtime_scopes.add(runtime_scope)
        if runtime_snapshot_path:
            if runtime_snapshot_path in snapshot_paths:
                findings.append(
                    Finding("FAIL", "duplicate runtime_snapshot_path", {"path": str(path), "runtime_snapshot_path": runtime_snapshot_path})
                )
            else:
                snapshot_paths.add(runtime_snapshot_path)
        if not session_name:
            findings.append(Finding("FAIL", "session_name missing", {"path": str(path)}))
            continue
        if not start_raw or not end_raw:
            findings.append(Finding("FAIL", "session window missing", {"path": str(path), "session_name": session_name}))
            continue
        start_hms = parse_hms_tuple(start_raw, fallback=(-1, -1, -1))
        end_hms = parse_hms_tuple(end_raw, fallback=(-1, -1, -1))
        if start_hms[0] < 0 or end_hms[0] < 0:
            findings.append(Finding("FAIL", "session window invalid", {"path": str(path), "session_name": session_name}))
            continue
        windows.append((session_name, _minutes_in_window(start_hms, end_hms)))
        findings.append(Finding("PASS", "session config loaded", {"path": str(path), "session_name": session_name, "strategy_id": strategy_id}))
    for idx, (name_a, minutes_a) in enumerate(windows):
        for name_b, minutes_b in windows[idx + 1 :]:
            overlap = minutes_a & minutes_b
            if overlap:
                findings.append(Finding("FAIL", "session windows overlap", {"session_a": name_a, "session_b": name_b}))
    ready = not any(item.level == "FAIL" for item in findings)
    return {
        "ready": ready,
        "ready_for_enablement": ready,
        "config_paths": [str(path) for path in config_paths],
        "findings": [{"level": item.level, "message": item.message, "details": item.details} for item in findings],
        "errors": [
            {"message": item.message, "details": item.details}
            for item in findings
            if item.level == "FAIL"
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a bundle of index_super_win session configs.")
    parser.add_argument("--config", action="append", required=True, dest="configs")
    parser.add_argument("--output-path", default=None)
    args = parser.parse_args()
    report = build_session_bundle_report(config_paths=[Path(item) for item in list(args.configs or [])])
    if args.output_path:
        path = Path(args.output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if bool(report.get("ready")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
