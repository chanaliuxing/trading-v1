#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _extract_daily_summary_payload(raw: dict[str, Any]) -> dict[str, Any]:
    if "strategy_sections" in raw:
        return raw
    metadata = _as_dict(raw.get("metadata"))
    if "strategy_sections" in metadata:
        return metadata
    return {}


def _extract_strategy_section(summary: dict[str, Any], *, strategy_id: str) -> dict[str, Any]:
    for row in list(summary.get("strategy_sections") or []):
        item = _as_dict(row)
        if str(item.get("strategy_id") or "").strip() == str(strategy_id).strip():
            return item
    return {}


def _has_strategy_section(summary: dict[str, Any], *, strategy_id: str) -> bool:
    for row in list(summary.get("strategy_sections") or []):
        item = _as_dict(row)
        if str(item.get("strategy_id") or "").strip() == str(strategy_id).strip():
            return True
    return False


def build_trader_parity_gate(
    *,
    identity_report: dict[str, Any],
    daily_summary_report: dict[str, Any],
    legacy_strategy_id: str,
    kernel_strategy_id: str,
    min_allow_match_ratio: float,
    min_side_match_ratio: float,
    min_reason_match_ratio: float,
    min_exec_symbol_match_ratio: float,
    max_fill_multiple: float,
    max_open_trigger_count: int,
    max_position_notional_abs_usd: float,
) -> dict[str, Any]:
    identity_ok = bool(identity_report.get("ok"))
    parity = _as_dict(identity_report.get("parity"))
    has_reason_parity = "reason_match_ratio" in parity
    has_exec_symbol_parity = "exec_symbol_match_ratio" in parity
    allow_match = _as_float(parity.get("allow_match_ratio"), 0.0)
    side_match = _as_float(parity.get("side_match_ratio"), 0.0)
    reason_match = _as_float(parity.get("reason_match_ratio"), 0.0)
    exec_symbol_match = _as_float(parity.get("exec_symbol_match_ratio"), 0.0)

    summary = _extract_daily_summary_payload(_as_dict(daily_summary_report))
    legacy_present = _has_strategy_section(summary, strategy_id=legacy_strategy_id)
    kernel_present = _has_strategy_section(summary, strategy_id=kernel_strategy_id)
    legacy_section = _extract_strategy_section(summary, strategy_id=legacy_strategy_id)
    kernel_section = _extract_strategy_section(summary, strategy_id=kernel_strategy_id)
    legacy_fills = int(_as_float(legacy_section.get("fills_count"), 0.0))
    kernel_fills = int(_as_float(kernel_section.get("fills_count"), 0.0))
    kernel_open_triggers = int(_as_float(kernel_section.get("open_trigger_count"), 0.0))
    kernel_position_notional_abs = _as_float(kernel_section.get("position_notional_abs_usd"), 0.0)

    max_allowed_fills = int(max(1, round(max_fill_multiple * max(1, legacy_fills))))
    checks = [
        {
            "name": "identity_report_present",
            "passed": identity_ok,
            "detail": {
                "identity_ok": identity_ok,
            },
        },
        {
            "name": "legacy_summary_present",
            "passed": legacy_present,
            "detail": {"legacy_strategy_id": str(legacy_strategy_id)},
        },
        {
            "name": "kernel_summary_present",
            "passed": kernel_present,
            "detail": {"kernel_strategy_id": str(kernel_strategy_id)},
        },
        {
            "name": "signal_allow_parity",
            "passed": allow_match >= min_allow_match_ratio,
            "detail": {
                "allow_match_ratio": round(allow_match, 6),
                "min_required": round(min_allow_match_ratio, 6),
            },
        },
        {
            "name": "signal_side_parity",
            "passed": side_match >= min_side_match_ratio,
            "detail": {
                "side_match_ratio": round(side_match, 6),
                "min_required": round(min_side_match_ratio, 6),
            },
        },
        {
            "name": "signal_reason_parity",
            "passed": has_reason_parity and reason_match >= min_reason_match_ratio,
            "detail": {
                "reason_match_ratio_present": has_reason_parity,
                "reason_match_ratio": round(reason_match, 6),
                "min_required": round(min_reason_match_ratio, 6),
            },
        },
        {
            "name": "exec_symbol_parity",
            "passed": has_exec_symbol_parity and exec_symbol_match >= min_exec_symbol_match_ratio,
            "detail": {
                "exec_symbol_match_ratio_present": has_exec_symbol_parity,
                "exec_symbol_match_ratio": round(exec_symbol_match, 6),
                "min_required": round(min_exec_symbol_match_ratio, 6),
            },
        },
        {
            "name": "cadence_fill_count_guard",
            "passed": kernel_fills <= max_allowed_fills,
            "detail": {
                "legacy_fills_count": legacy_fills,
                "kernel_fills_count": kernel_fills,
                "max_fill_multiple": max_fill_multiple,
                "max_allowed_kernel_fills": max_allowed_fills,
            },
        },
        {
            "name": "same_day_flatten_guard",
            "passed": kernel_open_triggers <= max_open_trigger_count and kernel_position_notional_abs <= max_position_notional_abs_usd,
            "detail": {
                "kernel_open_trigger_count": kernel_open_triggers,
                "max_open_trigger_count": int(max_open_trigger_count),
                "kernel_position_notional_abs_usd": round(kernel_position_notional_abs, 4),
                "max_position_notional_abs_usd": round(max_position_notional_abs_usd, 4),
            },
        },
    ]
    failed = [row for row in checks if not bool(row.get("passed"))]
    recommendation = "GO" if not failed else "HOLD"
    return {
        "ok": True,
        "legacy_strategy_id": str(legacy_strategy_id),
        "kernel_strategy_id": str(kernel_strategy_id),
        "recommendation": recommendation,
        "failed_checks": [str(row.get("name") or "") for row in failed],
        "checks": checks,
    }


def _load_json(path: str) -> dict[str, Any]:
    p = Path(path).expanduser()
    return _as_dict(json.loads(p.read_text(encoding="utf-8")))


def main() -> int:
    parser = argparse.ArgumentParser(description="Trader-facing parity gate: legacy QQQ vs stocks-kernel strategy")
    parser.add_argument("--identity-report", required=True, help="JSON output from run_qqq_stocks_kernel_identity_check.py")
    parser.add_argument("--daily-summary-report", required=True, help="JSON metadata from daily_trade_summary notification")
    parser.add_argument("--legacy-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--kernel-strategy-id", default="stocks_kernel_shadow_smh_001")
    parser.add_argument("--min-allow-match-ratio", type=float, default=0.98)
    parser.add_argument("--min-side-match-ratio", type=float, default=0.98)
    parser.add_argument("--min-reason-match-ratio", type=float, default=0.98)
    parser.add_argument("--min-exec-symbol-match-ratio", type=float, default=1.0)
    parser.add_argument("--max-fill-multiple", type=float, default=2.0)
    parser.add_argument("--max-open-trigger-count", type=int, default=0)
    parser.add_argument("--max-position-notional-abs-usd", type=float, default=0.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    payload = build_trader_parity_gate(
        identity_report=_load_json(args.identity_report),
        daily_summary_report=_load_json(args.daily_summary_report),
        legacy_strategy_id=str(args.legacy_strategy_id),
        kernel_strategy_id=str(args.kernel_strategy_id),
        min_allow_match_ratio=max(0.0, float(args.min_allow_match_ratio)),
        min_side_match_ratio=max(0.0, float(args.min_side_match_ratio)),
        min_reason_match_ratio=max(0.0, float(args.min_reason_match_ratio)),
        min_exec_symbol_match_ratio=max(0.0, float(args.min_exec_symbol_match_ratio)),
        max_fill_multiple=max(0.1, float(args.max_fill_multiple)),
        max_open_trigger_count=max(0, int(args.max_open_trigger_count)),
        max_position_notional_abs_usd=max(0.0, float(args.max_position_notional_abs_usd)),
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if str(args.output).strip():
        out = Path(str(args.output)).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload.get("recommendation") == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
