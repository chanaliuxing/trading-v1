#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return str(result.stdout).strip()


def _load_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _find_strategy(config: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    for item in list(config.get("strategies") or []):
        if str(item.get("id") or "").strip() == strategy_id:
            return dict(item)
    raise KeyError(f"strategy not found: {strategy_id}")


def _execution_contract(config: dict[str, Any]) -> dict[str, Any]:
    execution = dict(config.get("execution") or {})
    trace_validation = dict(execution.get("trace_validation") or {})
    entry_isolation = dict(execution.get("entry_isolation") or {})
    return {
        "enabled": bool(execution.get("enabled", False)),
        "route": execution.get("route"),
        "tif": execution.get("tif"),
        "trace_validation": trace_validation,
        "entry_isolation": {
            "enabled": bool(entry_isolation.get("enabled", False)),
            "symbols": list(entry_isolation.get("symbols") or []),
        },
    }


def _reconciliation_contract(config: dict[str, Any]) -> dict[str, Any]:
    reconciliation = dict(config.get("reconciliation") or {})
    runtime_cfg = dict(reconciliation.get("runtime") or {})
    return {
        "runtime": runtime_cfg,
        "adopt_time_window_min": reconciliation.get("adopt_time_window_min"),
        "cancel_unmatched_orphans": reconciliation.get("cancel_unmatched_orphans"),
        "executions_lookback_min": reconciliation.get("executions_lookback_min"),
        "safe_guard_max_actions": reconciliation.get("safe_guard_max_actions"),
    }


def _strategy_baseline(strategy: dict[str, Any]) -> dict[str, Any]:
    params = dict(strategy.get("params") or {})
    extras = dict(params.get("extras") or {})
    return {
        "id": strategy.get("id"),
        "enabled": bool(strategy.get("enabled", False)),
        "module": strategy.get("module"),
        "class": strategy.get("class"),
        "capital_limit_usd": strategy.get("capital_limit_usd"),
        "params": {
            "cadence_s": params.get("cadence_s"),
            "ttl_ms": params.get("ttl_ms"),
            "extras": extras,
        },
    }


def build_baseline(*, root: Path, config_path: Path, strategy_id: str) -> dict[str, Any]:
    config = _load_config(config_path)
    strategy = _find_strategy(config, strategy_id)
    tracked_files = {
        "strategy_file": root / "strategies" / "qqq_intraday_strategy.py",
        "execution_service_file": root / "services" / "exec" / "execution_service.py",
        "runtime_reconciler_file": root / "services" / "reconciliation" / "runtime_reconciler.py",
        "fill_event_handler_file": root / "services" / "exec" / "fill_event_handler.py",
        "config_file": config_path,
    }
    return {
        "generated_at_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "git_head": _git_head(root),
        "strategy_id": strategy_id,
        "baseline_scope": {
            "strategy_behavior": "qqq_intraday",
            "shared_execution_chain": [
                "execution_service",
                "runtime_reconciler",
                "fill_event_handler",
            ],
            "input_contract": [
                "config.yml strategy params",
                "execution.entry_isolation",
                "reconciliation.runtime",
            ],
        },
        "file_hashes": {name: _sha256(path) for name, path in tracked_files.items()},
        "strategy": _strategy_baseline(strategy),
        "execution_contract": _execution_contract(config),
        "reconciliation_contract": _reconciliation_contract(config),
        "parity_fields": [
            "gate_allow_or_block",
            "block_reason",
            "side",
            "qty",
            "entry_order_ref_kind",
            "protective_stop_submit",
            "flatten_reason",
            "runtime_transition",
            "restart_recovery_transition",
        ],
        "minimum_replay_fixtures": [
            "normal_entry_tp1_trailing_exit",
            "restart_live_position_missing_protection",
            "same_symbol_foreign_open_order_conflict",
        ],
    }


def render_markdown(baseline: dict[str, Any]) -> str:
    strategy = dict(baseline.get("strategy") or {})
    params = dict(strategy.get("params") or {})
    extras = dict(params.get("extras") or {})
    execution_contract = dict(baseline.get("execution_contract") or {})
    entry_isolation = dict(execution_contract.get("entry_isolation") or {})
    reconciliation_contract = dict(baseline.get("reconciliation_contract") or {})
    lines = [
        "# QQQ Intraday Replica Baseline",
        "",
        f"- Generated at ET: `{baseline.get('generated_at_et')}`",
        f"- Git HEAD: `{baseline.get('git_head')}`",
        f"- Strategy ID: `{baseline.get('strategy_id')}`",
        "",
        "## Frozen Files",
        "",
    ]
    for name, digest in dict(baseline.get("file_hashes") or {}).items():
        lines.append(f"- `{name}`: `{digest}`")
    lines.extend(
        [
            "",
            "## Strategy Contract",
            "",
            f"- Module: `{strategy.get('module')}`",
            f"- Class: `{strategy.get('class')}`",
            f"- Symbols: `{','.join(list(extras.get('symbols') or []))}`",
            f"- Loop interval sec: `{extras.get('loop_interval_sec')}`",
            f"- ATR period: `{extras.get('atr_period')}`",
            f"- Stop ATR mult: `{extras.get('stop_atr_mult')}`",
            f"- Submit exchange stop intent: `{extras.get('submit_exchange_stop_intent')}`",
            f"- Hard stop enabled: `{extras.get('enable_hard_stop')}`",
            f"- Breakeven shield enabled: `{extras.get('enable_breakeven_shield')}`",
            f"- ATR seed path: `{extras.get('atr_seed_path')}`",
            f"- Daily ATR path: `{extras.get('qqq_daily_atr_path')}`",
            f"- Observability snapshot path: `{extras.get('obs_snapshot_path')}`",
            f"- Diag log path: `{extras.get('diag_log_path')}`",
            "",
            "## Shared Execution Contract",
            "",
            f"- Execution enabled: `{execution_contract.get('enabled')}`",
            f"- Route: `{execution_contract.get('route')}`",
            f"- TIF: `{execution_contract.get('tif')}`",
            f"- Entry isolation enabled: `{entry_isolation.get('enabled')}`",
            f"- Entry isolation symbols: `{','.join(list(entry_isolation.get('symbols') or []))}`",
            f"- Reconciliation runtime interval sec: `{dict(reconciliation_contract.get('runtime') or {}).get('interval_seconds')}`",
            "",
            "## Frozen Parity Fields",
            "",
        ]
    )
    for item in list(baseline.get("parity_fields") or []):
        lines.append(f"- `{item}`")
    lines.extend(
        [
            "",
            "## Minimum Replay Fixtures",
            "",
        ]
    )
    for item in list(baseline.get("minimum_replay_fixtures") or []):
        lines.append(f"- `{item}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build frozen qqq replica baseline artifacts")
    parser.add_argument("--strategy-id", default="qqq_intraday_001")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument(
        "--json-out",
        default=str(ROOT / "docs" / "baselines" / "qqq_intraday_001_replica_baseline.json"),
    )
    parser.add_argument(
        "--md-out",
        default=str(ROOT / "docs" / "baselines" / "QQQ_INTRADAY_001_REPLICA_BASELINE.md"),
    )
    args = parser.parse_args()

    baseline = build_baseline(
        root=ROOT,
        config_path=Path(args.config),
        strategy_id=str(args.strategy_id),
    )
    json_out = Path(args.json_out)
    md_out = Path(args.md_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_out.write_text(render_markdown(baseline), encoding="utf-8")
    print(str(json_out))
    print(str(md_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
