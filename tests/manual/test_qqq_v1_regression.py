#!/usr/bin/env python3
"""
QQQ v1 safety regression runner.

Covers safety-critical behaviors:
1) flatten_action cancels existing open orders (prevents ghost stops).
2) protective_stop cancels previous stop orders (prevents stop stacking).
3) disable_oco enforces "no auto-OCO attach" for intents that manage stops explicitly.
4) non-entry fills still update DB state + release reservations (prevents ghost state).
5) v47_fast_drift entry flow is enabled in runtime config.
6) logic_retreat obeys minimum hold time guard (>=360s).

Run:
  python tests/manual/test_qqq_v1_regression.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def _assert_runtime_config(root: Path) -> None:
    cfg_path = root / "config.yml"
    cfg: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    strategies = cfg.get("strategies") or []

    target = None
    for item in strategies:
        if (item or {}).get("id") == "qqq_intraday_001":
            target = item
            break
    if target is None:
        raise RuntimeError("config.yml missing strategy id=qqq_intraday_001")

    extras = ((target.get("params") or {}).get("extras") or {})
    entry_model = str(extras.get("entry_model", ""))
    max_trades_per_day = int(float(extras.get("max_trades_per_day", 0)))
    min_hold = int(float(extras.get("logic_retreat_min_hold_sec", 0)))
    third_trade_requires_positive_day_pnl = bool(extras.get("third_trade_requires_positive_day_pnl", True))
    hard_stop_atr_mult = float(extras.get("hard_stop_atr_mult", 0.0))
    stop_atr_mult = extras.get("stop_atr_mult")
    long_drift_second_confirm = bool(extras.get("long_drift_second_confirm", False))
    delayed_confirm_move5m_min = float(extras.get("delayed_confirm_move5m_min", 0.0))
    fast_vol_min = float(extras.get("fast_vol_min", 0.0))
    enable_fast = bool(extras.get("enable_fast", True))

    errors: list[str] = []
    # Keep this script focused on safety-critical invariants rather than
    # hard-coding a specific trading schedule window.
    if entry_model != "v47_fast_drift":
        errors.append(f"entry_model expected v47_fast_drift, got {entry_model}")
    if max_trades_per_day > 4:
        errors.append(f"max_trades_per_day expected <= 4, got {max_trades_per_day}")
    if min_hold < 360:
        errors.append(f"logic_retreat_min_hold_sec expected >= 360, got {min_hold}")
    if third_trade_requires_positive_day_pnl:
        errors.append("third_trade_requires_positive_day_pnl expected False")
    if abs(hard_stop_atr_mult - 0.75) > 1e-9:
        errors.append(f"hard_stop_atr_mult expected 0.75, got {hard_stop_atr_mult}")
    if stop_atr_mult is not None:
        errors.append(f"stop_atr_mult expected to be removed from qqq runtime config, got {stop_atr_mult}")
    if not long_drift_second_confirm:
        errors.append("long_drift_second_confirm expected True")
    if delayed_confirm_move5m_min <= 0:
        errors.append(f"delayed_confirm_move5m_min expected > 0, got {delayed_confirm_move5m_min}")
    if fast_vol_min <= 0:
        errors.append(f"fast_vol_min expected > 0, got {fast_vol_min}")
    if not isinstance(enable_fast, bool):
        errors.append(f"enable_fast expected bool, got {enable_fast!r}")

    if errors:
        raise RuntimeError("QQQ runtime config check failed: " + "; ".join(errors))


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    _assert_runtime_config(root)

    test_files = [
        "tests/unit/exec/test_qqq_v1_trade_safety_regression.py",
        "tests/unit/strategies/test_qqq_hardening.py",
        "tests/unit/strategies/test_qqq_stable_entry_guard.py",
        "tests/unit/strategies/test_qqq_positions_ready_gate.py",
        "tests/unit/strategies/test_qqq_intraday_observability.py",
        "tests/unit/strategies/test_qqq_intraday_atr_seed.py",
    ]

    venv_python = root / "venv" / "bin" / "python"
    python_bin = str(venv_python) if venv_python.exists() else sys.executable
    cmd = [python_bin, "-m", "pytest", "-q", "-k", "not audit_loop", *test_files]
    print("Running:", " ".join(test_files))
    proc = subprocess.run(cmd, cwd=str(root))
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
