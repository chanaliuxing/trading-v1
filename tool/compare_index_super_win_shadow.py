#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compare qqq shadow/live observability with index_super_win shadow diagnostics.

Purpose:
1. Give one PASS/WARN/FAIL report for same-symbol shadow isolation.
2. Show whether index_super_win was blocked for the right reason.
3. Detect obvious parity/isolation failures before controlled live cut-in.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Finding:
    level: str
    message: str
    details: Dict[str, Any]


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _print_finding(finding: Finding) -> None:
    suffix = ""
    if finding.details:
        suffix = " " + json.dumps(finding.details, ensure_ascii=False, separators=(",", ":"), default=str)
    print(f"[{finding.level}] {finding.message}{suffix}")


def _default_qqq_obs_path(strategy_id: str) -> Path:
    return ROOT / "runtime" / "state" / f"qqq_intraday_observability_{strategy_id}.json"


def _default_index_diag_snapshot_path(strategy_id: str) -> Path:
    return ROOT / "runtime" / "state" / f"index_super_win_diag_snapshot_{strategy_id}.json"


def _final_exit_code(findings: list[Finding]) -> int:
    if any(item.level == "FAIL" for item in findings):
        return 2
    if any(item.level == "WARN" for item in findings):
        return 1
    return 0


def build_payload(
    *,
    qqq_obs_path: Path,
    index_diag_snapshot_path: Path,
    symbol: str,
) -> Dict[str, Any]:
    findings: list[Finding] = []
    qqq_obs = _load_json(qqq_obs_path)
    if qqq_obs is None:
        findings.append(Finding("FAIL", "Failed to load qqq observability snapshot", {"path": str(qqq_obs_path)}))
    index_diag = _load_json(index_diag_snapshot_path)
    if index_diag is None:
        findings.append(
            Finding("FAIL", "Failed to load index diag snapshot", {"path": str(index_diag_snapshot_path)})
        )

    if qqq_obs is not None and index_diag is not None:
        findings.extend(compare_shadow(qqq_obs=qqq_obs, index_diag=index_diag, symbol=symbol))

    symbol_u = str(symbol or "").strip().upper()
    qqq_state = dict((qqq_obs or {}).get("strategy_state") or {})
    index_trader_view = dict((((index_diag or {}).get("trader_view_by_symbol") or {}).get(symbol_u) or {}))
    index_shadow = dict((((index_diag or {}).get("shadow_report_by_symbol") or {}).get(symbol_u) or {}))
    index_conflict = dict((((index_diag or {}).get("conflict_report_by_symbol") or {}).get(symbol_u) or {}))

    return {
        "qqq_obs_path": str(qqq_obs_path),
        "index_diag_snapshot_path": str(index_diag_snapshot_path),
        "symbol": symbol_u,
        "summary": {
            "qqq_ready": bool((qqq_obs or {}).get("ready", False)),
            "qqq_exec_symbol": str(qqq_state.get("exec_symbol") or "").strip().upper() or None,
            "qqq_is_traded": bool(qqq_state.get("is_traded", False)),
            "qqq_side": str(qqq_state.get("side") or "").strip().upper() or None,
            "index_entry_enabled": bool(((index_diag or {}).get("entry_enabled_by_symbol") or {}).get(symbol_u, False)),
            "index_final_state": index_trader_view.get("final_state"),
            "index_gate_reason": str(index_trader_view.get("which_gate_blocked") or index_shadow.get("last_gate_reason") or "").strip() or None,
            "foreign_owner_strategy_id": str(index_conflict.get("foreign_owner_strategy_id") or "").strip() or None,
        },
        "findings": [
            {"level": item.level, "message": item.message, "details": item.details}
            for item in findings
        ],
        "exit_code": _final_exit_code(findings),
    }


def compare_shadow(
    *,
    qqq_obs: Dict[str, Any],
    index_diag: Dict[str, Any],
    symbol: str,
) -> list[Finding]:
    findings: list[Finding] = []
    symbol_u = str(symbol or "").strip().upper()

    qqq_ready = bool(qqq_obs.get("ready", False))
    qqq_state = dict(qqq_obs.get("strategy_state") or {})
    qqq_exec_symbol = str(qqq_state.get("exec_symbol") or "").strip().upper()
    qqq_is_traded = bool(qqq_state.get("is_traded", False))
    qqq_side = str(qqq_state.get("side") or "").strip().upper() or None

    index_entry_enabled = bool((index_diag.get("entry_enabled_by_symbol") or {}).get(symbol_u, False))
    index_trader_view = dict((index_diag.get("trader_view_by_symbol") or {}).get(symbol_u) or {})
    index_conflict = dict((index_diag.get("conflict_report_by_symbol") or {}).get(symbol_u) or {})
    index_shadow = dict((index_diag.get("shadow_report_by_symbol") or {}).get(symbol_u) or {})
    index_chain = dict((index_diag.get("chain_report_by_symbol") or {}).get(symbol_u) or {})

    findings.append(
        Finding(
            "OK",
            "Loaded qqq and index snapshots",
            {
                "symbol": symbol_u,
                "qqq_ready": qqq_ready,
                "qqq_exec_symbol": qqq_exec_symbol or None,
                "qqq_is_traded": qqq_is_traded,
                "index_entry_enabled": index_entry_enabled,
                "index_final_state": index_trader_view.get("final_state"),
            },
        )
    )

    if "qqqv1" in json.dumps(index_diag, ensure_ascii=True, sort_keys=True):
        findings.append(Finding("FAIL", "index snapshot contains qqqv1 namespace reference", {"symbol": symbol_u}))
    else:
        findings.append(Finding("OK", "index snapshot has no qqqv1 namespace reference", {"symbol": symbol_u}))

    if qqq_ready and index_entry_enabled:
        findings.append(Finding("OK", "Both strategies report ready/entry-enabled state", {"symbol": symbol_u}))
    elif (not qqq_ready) and (not index_entry_enabled):
        findings.append(Finding("WARN", "Both strategies are not ready for entry", {"symbol": symbol_u}))
    elif qqq_ready and (not index_entry_enabled):
        findings.append(
            Finding(
                "WARN",
                "qqq is ready but index_super_win entry is disabled",
                {"symbol": symbol_u, "index_block_reason": index_shadow.get("block_reason")},
            )
        )
    else:
        findings.append(
            Finding(
                "WARN",
                "index_super_win is entry-enabled while qqq is not ready",
                {"symbol": symbol_u, "qqq_missing_required": qqq_obs.get("missing_required")},
            )
        )

    same_symbol_live = qqq_is_traded and qqq_exec_symbol == symbol_u
    foreign_owner = str(index_conflict.get("foreign_owner_strategy_id") or "").strip() or None
    gate_reason = str(index_trader_view.get("which_gate_blocked") or index_shadow.get("last_gate_reason") or "").strip()
    foreign_conflict = bool(index_trader_view.get("broker_open_order_conflict")) or bool(index_trader_view.get("broker_position_conflict")) or bool(foreign_owner)

    if same_symbol_live:
        if index_trader_view.get("final_state") == "entered":
            findings.append(
                Finding(
                    "FAIL",
                    "index_super_win entered while qqq already holds same exec symbol",
                    {
                        "symbol": symbol_u,
                        "qqq_side": qqq_side,
                        "index_last_preview": index_trader_view.get("last_preview"),
                    },
                )
            )
        elif index_trader_view.get("final_state") == "blocked" and foreign_conflict:
            findings.append(
                Finding(
                    "PASS",
                    "Same-symbol isolation held: index_super_win blocked behind qqq-owned symbol",
                    {
                        "symbol": symbol_u,
                        "gate_reason": gate_reason or None,
                        "foreign_owner_strategy_id": foreign_owner,
                        "conflict_report": index_conflict,
                    },
                )
            )
        else:
            findings.append(
                Finding(
                    "WARN",
                    "qqq is live on same symbol but index block reason is inconclusive",
                    {
                        "symbol": symbol_u,
                        "index_final_state": index_trader_view.get("final_state"),
                        "gate_reason": gate_reason or None,
                    },
                )
            )
    else:
        if index_trader_view.get("final_state") == "blocked" and foreign_conflict:
            findings.append(
                Finding(
                    "WARN",
                    "index_super_win is blocked by foreign conflict while qqq is not live on same symbol",
                    {
                        "symbol": symbol_u,
                        "qqq_exec_symbol": qqq_exec_symbol or None,
                        "qqq_is_traded": qqq_is_traded,
                        "gate_reason": gate_reason or None,
                        "conflict_report": index_conflict,
                    },
                )
            )
        else:
            findings.append(
                Finding(
                    "OK",
                    "No same-symbol live conflict detected from qqq snapshot",
                    {
                        "symbol": symbol_u,
                        "qqq_exec_symbol": qqq_exec_symbol or None,
                        "qqq_is_traded": qqq_is_traded,
                    },
                )
            )

    findings.append(
        Finding(
            "OK",
            "index shadow chain summary",
            {
                "symbol": symbol_u,
                "entry_enabled": index_entry_enabled,
                "shadow_report": index_shadow,
                "chain_report": {
                    "entry_gate": index_chain.get("entry_gate"),
                    "preview": index_chain.get("preview"),
                    "submit": index_chain.get("submit"),
                    "runtime": index_chain.get("runtime"),
                },
            },
        )
    )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare qqq vs index_super_win shadow/isolation snapshots")
    parser.add_argument("--symbol", required=True, help="Execution symbol to compare, e.g. TQQQ/AAPL")
    parser.add_argument("--qqq-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--index-strategy-id", default="index_super_win_001")
    parser.add_argument("--qqq-obs-path", default="", help="Override qqq observability snapshot path")
    parser.add_argument("--index-diag-snapshot-path", default="", help="Override index diag snapshot path")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary instead of text findings")
    args = parser.parse_args()

    qqq_obs_path = Path(args.qqq_obs_path) if str(args.qqq_obs_path).strip() else _default_qqq_obs_path(args.qqq_strategy_id)
    index_diag_path = (
        Path(args.index_diag_snapshot_path)
        if str(args.index_diag_snapshot_path).strip()
        else _default_index_diag_snapshot_path(args.index_strategy_id)
    )

    payload = build_payload(
        qqq_obs_path=qqq_obs_path,
        index_diag_snapshot_path=index_diag_path,
        symbol=args.symbol,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for item in payload["findings"]:
            finding = Finding(level=str(item["level"]), message=str(item["message"]), details=dict(item["details"]))
            _print_finding(finding)
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
