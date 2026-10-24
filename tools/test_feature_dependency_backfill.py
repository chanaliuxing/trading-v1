#!/usr/bin/env python3
"""
Smoke test for feature dependency inference and auto‑backfill wiring.

This does NOT touch IBKR or ClickHouse. It only verifies that
_infer_symbol_dependencies_from_specs correctly detects cross‑asset
symbols from Brain‑style specs (expr + DSL), and that the function
is robust under a patched strategy universe.

Usage:
  python tools/test_feature_dependency_backfill.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

from contextlib import contextmanager


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@contextmanager
def _patch_strategy_symbols(symbols: List[str]):
    """Temporarily patch _get_strategy_symbols in control_event_consumer."""
    from unittest.mock import patch
    import scripts.ai_trader.control_event_consumer as cec

    with patch.object(cec, "_get_strategy_symbols", return_value=list(symbols)):
        yield cec


def _make_specs() -> List[Dict[str, Any]]:
    """Construct representative Brain specs with cross‑asset columns."""
    return [
        {
            "name": "spy_ret5_atr14_ratio",
            "expr": "(f.spy_ret_5) / nullIf(f.atr_14, 1e-06)",
            "dtype": "Float64",
            "notes": "Cross‑asset: SPY 5m return vs TSLA ATR",
        },
        {
            "name": "qqq_ret5_scaled",
            "dsl": {
                "op": "ratio",
                "num": "f.qqq_ret_5",
                "den": "f.atr_14",
            },
            "dtype": "Float64",
            "notes": "Cross‑asset: QQQ 5m return scaled by TSLA ATR",
        },
        {
            "name": "tsla_local_momentum",
            "expr": "f.ret_5",
            "dtype": "Float64",
            "notes": "Single‑asset TSLA feature only",
        },
    ]


def main() -> None:
    from scripts.ai_trader.control_event_consumer import _infer_symbol_dependencies_from_specs

    specs = _make_specs()
    # Simulate a realistic universe: TSLA + cross‑asset indices
    universe = ["TSLA", "SPY", "QQQ", "AAPL"]

    with _patch_strategy_symbols(universe) as cec:
        deps = _infer_symbol_dependencies_from_specs(specs)

    print("Universe:", universe)
    print("Specs:")
    for s in specs:
        print("  -", s["name"])
    print("Inferred dependencies:", deps)

    # Simple expectation: SPY and QQQ are required; TSLA alone is not.
    expected = {"SPY", "QQQ"}
    missing = expected.difference(deps)
    extra = set(deps).difference(expected)

    if missing or extra:
        print("⚠️  Unexpected dependency inference result")
        print("  expected:", sorted(expected))
        print("  got     :", deps)
        sys.exit(1)

    print("✅ Dependency inference looks correct.")


if __name__ == "__main__":
    main()

