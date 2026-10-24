#!/usr/bin/env python3
"""
Quick smoke test for:
- DataHealer coverage checks (no endless backfill loop when data is present)
- AiBrainMLGateway behaviour on invalid_features (should degrade to HOLD, not BLOCK)

Run from repo root:
  venv/bin/python tools/test_data_healer_and_gateway.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys

# Ensure project root on sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.data.data_healer import DataHealer
from services.ml.ai_brain_ml_gateway import AiBrainMLGateway
from services.ml.model_inference_service import ModelMLInferenceService


class _DummyInferenceService(ModelMLInferenceService):
    """
    Minimal stub for testing AiBrainMLGateway invalid_features path.

    We intentionally override __init__ to avoid loading any real model
    artifacts so the test is fast and side‑effect free.
    """

    def __init__(self) -> None:  # type: ignore[override]
        self.is_ready = True
        self.scaler = None
        self.feature_cols = ["feat1", "feat2"]
        self.metadata = {}

    def predict_raw(self, features: dict) -> dict:  # type: ignore[override]
        # Simulate strict feature validation failing in ModelMLInferenceService
        return {
            "status": "error",
            "error_reason": "invalid_features: 2 (feat1,feat2)",
            "raw_pred": 0.5,
        }


def _test_data_healer(symbols: list[str]) -> dict:
    """Run a non‑intrusive coverage check via DataHealer._check_coverage."""
    healer = DataHealer("runs/ai_trader")
    missing = healer._check_coverage(symbols, days=2)
    return {
        "symbols": symbols,
        "missing": missing,
        "ok": not bool(missing),
    }


def _test_gateway_invalid_features() -> dict:
    """Verify that invalid_features leads to degraded HOLD instead of BLOCK."""
    dummy_service = _DummyInferenceService()
    gw = AiBrainMLGateway(dummy_service)
    decision = gw.get_decision("TEST", {"feat1": None, "feat2": None})
    return {
        "decision": decision,
        "is_degraded_hold": decision.get("decision") == "hold"
        and decision.get("status") == "degraded"
        and decision.get("ml_conf") == 0.0,
    }


def main() -> None:
    symbols = ["TSLA", "NVDA"]
    healer_res = _test_data_healer(symbols)
    gw_res = _test_gateway_invalid_features()

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "data_healer": healer_res,
        "gateway_invalid_features": gw_res,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
