#!/usr/bin/env python3
"""
Quick smoke test for ModelMLInferenceService + AiBrainMLGateway.

Usage:
  python tools/test_ml_gateway.py --model-dir models/xgb_h30_tsla_optimized

It will:
  - Load the model service from the given directory
  - Construct a synthetic feature dict with all required feature_cols
  - Run the gateway decision logic and print the result
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Any


def main() -> None:
    from services.ml.model_inference_service import get_model_service
    from services.ml.ai_brain_ml_gateway import AiBrainMLGateway

    ap = argparse.ArgumentParser(description="Test ML Gateway stack (service + gateway).")
    ap.add_argument(
        "--model-dir",
        default="models/xgb_h30_tsla_optimized",
        help="Path to model directory (default: models/xgb_h30_tsla_optimized)",
    )
    ap.add_argument(
        "--symbol",
        default="TSLA",
        help="Symbol to use in synthetic features (default: TSLA)",
    )
    args = ap.parse_args()

    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        print(f"❌ Model dir not found: {model_dir}")
        return

    try:
        svc = get_model_service(model_dir=str(model_dir))
    except Exception as exc:
        print(f"❌ Failed to init ModelMLInferenceService: {exc}")
        return

    try:
        gateway = AiBrainMLGateway(svc)
    except Exception as exc:
        print(f"❌ Failed to init AiBrainMLGateway: {exc}")
        return

    # Build a minimal synthetic feature dict: fill all required feature_cols with 0.0,
    # and set atr_14 to baseline value (if present) so regime filter is not tripped.
    features: Dict[str, Any] = {"symbol": args.symbol}
    for col in getattr(svc, "feature_cols", []):
        features[col] = 0.0

    baseline_atr = float(getattr(svc, "metadata", {}).get("baseline_atr_14", 0.0) or 0.0)
    if baseline_atr > 0.0:
        features["atr_14"] = baseline_atr

    decision = gateway.get_decision(args.symbol, features)

    print("=== ML Gateway Decision ===")
    for k in sorted(decision.keys()):
        print(f"{k}: {decision[k]}")


if __name__ == "__main__":
    main()

