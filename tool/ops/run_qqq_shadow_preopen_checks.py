#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.market_calendar import is_trading_day
from tool.replay.build_qqq_kernel_shadow_postclose_report import build_postclose_report
from tool.replay.check_qqq_kernel_shadow_readiness import build_readiness_report
from tool.replay.run_qqq_kernel_shadow_parity import build_qqq_kernel_shadow_parity_report
from tool.replay.switch_qqq_kernel_shadow import _status as shadow_status

ET = ZoneInfo("America/New_York")
TRACE_ROOT = REPO_ROOT / "reports" / "qqq_signal_snapshots"
REPORT_ROOT = REPO_ROOT / "reports" / "qqq_kernel_shadow_parity"
LIVE = "qqq_intraday_001"
LEGACY_ON = "qqq_kernel_shadow_legacy_on_001"
LEGACY_OFF = "qqq_kernel_shadow_legacy_off_001"


def _default_day() -> str:
    return (datetime.now(ET) - timedelta(days=1)).strftime("%Y%m%d")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _find_shadow_trace(day: str, strategy_id: str, trace_root: Path) -> Path | None:
    preferred = trace_root / f"qqq_signal_snapshots_{day}_{strategy_id}.jsonl"
    if preferred.exists():
        return preferred
    for path in sorted(trace_root.glob(f"qqq_signal_snapshots_{day}*.jsonl")):
        rows = _read_jsonl(path)
        if any(str(row.get("strategy_id") or "").strip() == strategy_id for row in rows):
            return path
    return None


def _config_status(config_path: Path) -> dict[str, str]:
    return shadow_status(config_path.read_text(encoding="utf-8"))


def _build_pairwise_parity(
    *,
    left_strategy_id: str,
    right_strategy_id: str,
    left_path: Path | None,
    right_path: Path | None,
    day: str,
    report_root: Path,
    output_suffix: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "reason": "missing_trace_inputs",
        "left_strategy_id": left_strategy_id,
        "right_strategy_id": right_strategy_id,
        "left_path": str(left_path) if left_path else None,
        "right_path": str(right_path) if right_path else None,
    }
    if not left_path or not right_path:
        return payload
    report = build_qqq_kernel_shadow_parity_report(
        legacy_records=_read_jsonl(left_path),
        kernel_records=_read_jsonl(right_path),
        legacy_strategy_id=left_strategy_id,
        kernel_strategy_id=right_strategy_id,
        legacy_mode=output_suffix,
        day=day,
    )
    output = report_root / f"{day}_{output_suffix}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload.update(
        {
            "ok": True,
            "output": str(output),
            "diff_total": int(report.get("diff_total") or 0),
            "match_ratio": float(report.get("match_ratio") or 0.0),
            "recommendation": report.get("recommendation"),
        }
    )
    payload.pop("reason", None)
    return payload


def build_preopen_checks(
    *,
    day: str,
    config_path: Path,
    trace_root: Path,
    report_root: Path,
) -> dict[str, Any]:
    target_date = datetime.strptime(day, "%Y%m%d").date()
    if not bool(is_trading_day(target_date)):
        return {
            "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
            "day": day,
            "recommendation": "PASS",
            "blockers": [],
            "market_calendar": {
                "trading_day": False,
                "status": "non_trading_day",
            },
            "readiness": {"ok": None, "failed_checks": []},
            "shadow_status": {},
            "postclose_refresh": {},
            "parity_smoke": {
                "ok": False,
                "reason": "non_trading_day",
            },
        }

    readiness = build_readiness_report(config_path=config_path, require_disabled=False)
    status = _config_status(config_path)
    postclose = build_postclose_report(
        day=day,
        config_path=config_path,
        report_root=report_root,
        trace_root=trace_root,
    )
    postclose_path = report_root / f"{day}_postclose_report.json"
    postclose_path.parent.mkdir(parents=True, exist_ok=True)
    postclose_path.write_text(json.dumps(postclose, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    live_path = _find_shadow_trace(day, LIVE, trace_root)
    legacy_on_path = _find_shadow_trace(day, LEGACY_ON, trace_root)
    legacy_off_path = _find_shadow_trace(day, LEGACY_OFF, trace_root)
    live_vs_legacy_on = _build_pairwise_parity(
        left_strategy_id=LIVE,
        right_strategy_id=LEGACY_ON,
        left_path=live_path,
        right_path=legacy_on_path,
        day=day,
        report_root=report_root,
        output_suffix="preopen_parity_live_vs_legacy_on",
    )
    live_vs_legacy_off = _build_pairwise_parity(
        left_strategy_id=LIVE,
        right_strategy_id=LEGACY_OFF,
        left_path=live_path,
        right_path=legacy_off_path,
        day=day,
        report_root=report_root,
        output_suffix="preopen_parity_live_vs_legacy_off",
    )
    parity_smoke: dict[str, Any] = {
        "ok": bool(live_vs_legacy_on.get("ok")) and bool(live_vs_legacy_off.get("ok")),
        "live_path": str(live_path) if live_path else None,
        "legacy_on_path": str(legacy_on_path) if legacy_on_path else None,
        "legacy_off_path": str(legacy_off_path) if legacy_off_path else None,
        "pairs": {
            "live_vs_legacy_on": live_vs_legacy_on,
            "live_vs_legacy_off": live_vs_legacy_off,
        },
    }
    if not parity_smoke["ok"]:
        parity_smoke["reason"] = "missing_live_or_shadow_trace_inputs"

    blockers: list[str] = []
    if readiness.get("ok") is False:
        blockers.extend(f"readiness:{item}" for item in readiness.get("failed_checks", []))
    for strategy_id, value in sorted(status.items()):
        if value != "true":
            blockers.append(f"shadow_status:{strategy_id}={value}")
    if not parity_smoke.get("ok"):
        blockers.append(f"parity_smoke:{parity_smoke.get('reason')}")
    else:
        for pair_name, pair_payload in sorted(dict(parity_smoke.get("pairs") or {}).items()):
            if str(pair_payload.get("recommendation") or "").strip() != "PASS":
                blockers.append(f"parity_smoke:{pair_name}:{pair_payload.get('recommendation')}")

    recommendation = "PASS" if not blockers else "HOLD"
    return {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "day": day,
        "market_calendar": {
            "trading_day": True,
            "status": "trading_day",
        },
        "recommendation": recommendation,
        "blockers": blockers,
        "readiness": {
            "ok": readiness.get("ok"),
            "failed_checks": list(readiness.get("failed_checks") or []),
        },
        "shadow_status": status,
        "postclose_refresh": {
            "output": str(postclose_path),
            "recommendation": postclose.get("recommendation"),
            "blockers": list(postclose.get("blockers") or []),
        },
        "parity_smoke": parity_smoke,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run QQQ shadow pre-open smoke checks and refresh prior-day postclose report")
    parser.add_argument("--day", default=_default_day())
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yml"))
    parser.add_argument("--trace-root", default=str(TRACE_ROOT))
    parser.add_argument("--report-root", default=str(REPORT_ROOT))
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    payload = build_preopen_checks(
        day=str(args.day),
        config_path=Path(args.config),
        trace_root=Path(args.trace_root),
        report_root=Path(args.report_root),
    )
    output = Path(args.output) if str(args.output or "").strip() else Path(args.report_root) / f"{args.day}_preopen_smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"day": payload["day"], "recommendation": payload["recommendation"], "blockers": payload["blockers"]}, ensure_ascii=False, sort_keys=True))
    return 0 if not payload["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
