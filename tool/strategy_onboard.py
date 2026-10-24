#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def build_option_onboard_plan(
    *,
    strategy_id: str,
    plugin: str,
    underlying: str,
    output_docs_path: str = "",
) -> dict:
    sid = str(strategy_id or "").strip()
    plugin_s = str(plugin or "").strip()
    underlying_s = str(underlying or "").strip().upper()
    if not sid:
        raise ValueError("strategy_id is required")
    if not plugin_s:
        raise ValueError("plugin is required")
    if not underlying_s:
        raise ValueError("underlying is required")
    docs_path = output_docs_path or f"docs/strategies/{sid}.md"
    return {
        "artifact_type": "strategy_onboard_plan",
        "generated_at": datetime.now(ET).isoformat(),
        "strategy_id": sid,
        "strategy_type": "option_plugin",
        "config_patch": {
            "strategies[]": {
                "id": sid,
                "class": "OptionKernelStrategy",
                "enabled": False,
                "params": {
                    "extras": {
                        "kernel_mode": "pure_plugin",
                        "plugin": plugin_s,
                        "underlying": underlying_s,
                        "data_contract": {
                            "production_blocking": True,
                            "symbols": [underlying_s],
                            "ttl_sec": 30,
                            "requirements": [
                                {
                                    "dataset": "market_bars",
                                    "symbols": [underlying_s],
                                    "resolution": "1m",
                                    "ttl_sec": 90,
                                    "rth": False,
                                    "warmup_required": True,
                                    "min_rows": 0,
                                },
                                {
                                    "dataset": "option_chain",
                                    "symbols": [underlying_s],
                                    "resolution": "snapshot",
                                    "ttl_sec": 300,
                                    "rth": False,
                                    "warmup_required": False,
                                    "min_rows": 1,
                                },
                            ],
                        },
                        "production_manifest": {
                            "production_blocking": True,
                            "entry_contract": {
                                "uses_decision_pipeline": True,
                                "uom_only": True,
                            },
                            "session_policy": {
                                "entry_window": "",
                                "force_flatten_deadline": "",
                            },
                            "protection_contract": {
                                "required": True,
                                "fallback": "flatten",
                            },
                            "recovery_contract": {
                                "policy_id": "option_close_only",
                                "unknown_owner_action": "quarantine",
                                "fallback": "flatten",
                                "broker_truth_first": True,
                            },
                            "eod_contract": {
                                "no_overnight": True,
                                "platform_eod_required": True,
                            },
                            "tradability_reporting": {
                                "reports_strategy_tradability_gate": True,
                            },
                            "option_contract": {
                                "required": True,
                                "identity_fields": [
                                    "opt_symbol",
                                    "expiry",
                                    "strike",
                                    "right",
                                    "multiplier",
                                    "exchange",
                                    "tradingClass",
                                    "conId_or_localSymbol",
                                ],
                            },
                        },
                    }
                },
            }
        },
        "phase0_baseline_diff": {
            "action": "add_strategy_to_baseline",
            "strategy_id": sid,
            "requires_operator_review": True,
        },
        "docs_stub": {
            "path": docs_path,
            "sections": [
                "Overview",
                "Data Contract",
                "Entry Intent",
                "Exit Plan",
                "Risk Controls",
                "Production Gates",
                "Replay And Acceptance",
            ],
        },
        "acceptance": [
            "OptionPluginTestHarness warmup/reject/accepted-entry tests pass",
            "contract_selection readiness emits current selected/rejected verdict",
            "production_state has no data_availability or manifest blockers",
            "Phase0 baseline diff reviewed and committed with strategy config",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an auditable option strategy onboarding plan")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--underlying", required=True)
    parser.add_argument("--docs-path", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    plan = build_option_onboard_plan(
        strategy_id=args.strategy_id,
        plugin=args.plugin,
        underlying=args.underlying,
        output_docs_path=args.docs_path,
    )
    text = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
