#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
REPORT_ROOT = ROOT / "reports" / "qqq_kernel_shadow_parity"
SHADOW_IDS = ("qqq_kernel_shadow_legacy_on_001", "qqq_kernel_shadow_legacy_off_001")
TRACE_ROOT = ROOT / "reports" / "qqq_signal_snapshots"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_load_error": str(exc), "_path": str(path)}


def _strategy_instances(config_path: Path) -> dict[str, dict[str, Any]]:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return {str(item.get("id") or ""): item for item in list(payload.get("strategies") or [])}


def _shadow_config_summary(config_path: Path) -> dict[str, Any]:
    instances = _strategy_instances(config_path)
    out: dict[str, Any] = {
        "qqq_intraday_001": {
            "present": "qqq_intraday_001" in instances,
            "enabled": bool((instances.get("qqq_intraday_001") or {}).get("enabled")),
        },
        "shadows": {},
    }
    for strategy_id in SHADOW_IDS:
        item = instances.get(strategy_id) or {}
        extras = dict(((item.get("params") or {}).get("extras") or {}))
        out["shadows"][strategy_id] = {
            "present": bool(item),
            "enabled": bool(item.get("enabled")),
            "dry_run": bool(extras.get("dry_run")),
            "shadow_only": bool(extras.get("shadow_only")),
            "policy_id": extras.get("policy_id"),
            "shadow_legacy_mode": extras.get("shadow_legacy_mode"),
            "shadow_origin_strategy_id": extras.get("shadow_origin_strategy_id"),
            "shared_underlying_read_mode": extras.get("shared_underlying_read_mode"),
        }
    return out


def _parity_reports(day: str, report_root: Path) -> list[dict[str, Any]]:
    candidates = sorted(report_root.glob(f"{day}*.json"))
    rows = []
    for path in candidates:
        name = path.name.lower()
        # Only treat explicit parity artifacts as parity evidence.
        if "parity" not in name:
            continue
        if (
            name.endswith("_readiness.json")
            or name.endswith("_preopen_gate.json")
            or name.endswith("_preopen_gate_enabled.json")
            or name.endswith("_postclose_report.json")
            or name.endswith("_postclose_report_preview.json")
        ):
            continue
        payload = _load_json(path)
        if not payload:
            continue
        rows.append(
            {
                "path": str(path),
                "recommendation": payload.get("recommendation"),
                "diff_total": payload.get("diff_total"),
                "match_ratio": payload.get("match_ratio"),
                "legacy_mode": payload.get("legacy_mode"),
                "legacy_count": payload.get("legacy_count"),
                "kernel_count": payload.get("kernel_count"),
                "diff_count_by_field": payload.get("diff_count_by_field"),
            }
        )
    return rows


def _is_trading_day(day: str) -> bool:
    try:
        dt = datetime.strptime(str(day), "%Y%m%d")
    except Exception:
        return False
    return int(dt.weekday()) < 5


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _trace_summary(day: str, trace_root: Path) -> dict[str, Any]:
    summary = {
        "present": False,
        "rows_total": 0,
        "evaluation_rows": 0,
        "entry_rows": 0,
        "strategy_counts": {},
    }
    for path in sorted(trace_root.glob(f"qqq_signal_snapshots_{day}*.jsonl")):
        for row in _load_jsonl_rows(path):
            strategy_id = str(row.get("strategy_id") or "").strip()
            snapshot_kind = str(row.get("snapshot_kind") or "entry").strip().lower() or "entry"
            strategy_counts = summary["strategy_counts"].setdefault(strategy_id or "__unknown__", {"rows": 0, "evaluation": 0, "entry": 0})
            summary["present"] = True
            summary["rows_total"] += 1
            strategy_counts["rows"] += 1
            if snapshot_kind == "evaluation":
                summary["evaluation_rows"] += 1
                strategy_counts["evaluation"] += 1
            elif snapshot_kind == "entry":
                summary["entry_rows"] += 1
                strategy_counts["entry"] += 1
    return summary


def build_postclose_report(*, day: str, config_path: Path, report_root: Path, trace_root: Path) -> dict[str, Any]:
    readiness = _load_json(report_root / f"{day}_enabled_readiness.json") or _load_json(report_root / f"{day}_readiness.json")
    preopen = _load_json(report_root / f"{day}_preopen_gate.json")
    parity = _parity_reports(day, report_root)
    traces = _trace_summary(day, trace_root)
    config = _shadow_config_summary(config_path)
    blockers = []
    day_is_trading_day = _is_trading_day(day)
    if not bool(config["qqq_intraday_001"]["enabled"]):
        blockers.append("qqq_intraday_001_not_enabled")
    for strategy_id, item in dict(config["shadows"]).items():
        if not bool(item.get("enabled")):
            blockers.append(f"{strategy_id}_not_enabled")
        if not bool(item.get("dry_run")):
            blockers.append(f"{strategy_id}_dry_run_not_true")
        if not bool(item.get("shadow_only")):
            blockers.append(f"{strategy_id}_shadow_only_not_true")
    if readiness and readiness.get("ok") is False:
        blockers.append("readiness_failed")
    if day_is_trading_day and len(parity) == 0:
        if int(traces.get("evaluation_rows") or 0) > 0 and int(traces.get("entry_rows") or 0) == 0:
            blockers.append("no_entry_candidate_today")
        else:
            blockers.append("parity_runner_did_not_execute")
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "day": day,
        "recommendation": "CONTINUE_SHADOW" if not blockers else "HOLD",
        "blockers": blockers,
        "config": config,
        "readiness": {
            "present": bool(readiness),
            "ok": readiness.get("ok"),
            "recommendation": readiness.get("recommendation"),
            "failed_checks": readiness.get("failed_checks"),
        },
        "preopen_gate": {
            "present": bool(preopen),
            "ok": preopen.get("ok"),
            "recommendation": preopen.get("recommendation"),
        },
        "trace_summary": traces,
        "parity_reports": parity,
        "summary": {
            "day_is_trading_day": day_is_trading_day,
            "parity_report_count": len(parity),
            "total_diff_count": sum(int(row.get("diff_total") or 0) for row in parity),
            "shadow_enabled_count": sum(1 for item in dict(config["shadows"]).values() if bool(item.get("enabled"))),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build QQQ kernel shadow post-close report.")
    parser.add_argument("--day", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--report-root", default=str(REPORT_ROOT))
    parser.add_argument("--trace-root", default=str(TRACE_ROOT))
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    report_root = Path(args.report_root)
    output = Path(args.output) if str(args.output or "").strip() else report_root / f"{args.day}_postclose_report.json"
    payload = build_postclose_report(
        day=str(args.day),
        config_path=Path(args.config),
        report_root=report_root,
        trace_root=Path(args.trace_root),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(output), "recommendation": payload["recommendation"], "blockers": payload["blockers"]}, ensure_ascii=False, sort_keys=True))
    return 0 if not payload["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
