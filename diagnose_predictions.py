#!/usr/bin/env python3
"""
Diagnostic script to verify ML predictions are being generated correctly.
This will run a single HPO trial and check the prediction values.
"""

import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent))

from wf_hpo import WalkForwardHPO

def main():
    # Load config
    with open("alpha_config_profit_focused.json", "r") as f:
        config = json.load(f)

    # Create HPO instance
    hpo_config = config.get("hpo", {})
    # Add missing grid (empty for this test)
    if "grid" not in hpo_config:
        hpo_config["grid"] = {}
    # Add objective if missing
    if "objective" not in hpo_config:
        hpo_config["objective"] = {"metric": "sharpe", "maximize": True}

    hpo = WalkForwardHPO(hpo_config)

    # Get one fold
    if not hasattr(hpo, 'folds') or not hpo.folds:
        print("ERROR: No folds generated")
        return

    fold = hpo.folds[0]
    print(f"Using fold: {fold.fold_id}")
    print(f"  IS period: {fold.is_start} to {fold.is_end}")

    # Create test params
    params = {
        "model_type": "ridge",
        "feature_subset": ["vwap", "atr_14", "volume"],
        "decision": {"mode": "threshold", "threshold_long": 0.0001, "threshold_short": -0.0001},
        "task": "reg",
        "model_params": {"alpha": 0.01},
    }

    # Get data slice
    df_slice = hpo._slice_dataset(fold.is_start, fold.is_end)
    if df_slice is None or df_slice.empty:
        print("ERROR: No data in slice")
        return

    print(f"\nData slice shape: {df_slice.shape}")
    print(f"Data columns: {list(df_slice.columns)}")

    # Prepare model data (this should add ml_pred, ml_signal, ml_conf)
    print("\nCalling _prepare_model_data...")
    try:
        df_is_aug, df_oos_aug, meta = hpo._prepare_model_data(fold, params, df_slice)
    except Exception as e:
        print(f"ERROR in _prepare_model_data: {e}")
        import traceback
        traceback.print_exc()
        return

    if df_is_aug is None:
        print("ERROR: df_is_aug is None - model data preparation failed")
        print("This likely means:")
        print("  - Label column 'y' doesn't exist and couldn't be generated")
        print("  - Or model_type is 'none' or 'baseline'")
        print("\nTrying to check what happened...")

        # Check if we have label column
        if "y" not in df_slice.columns:
            print(f"  → Label 'y' not in dataframe columns: {list(df_slice.columns)}")
            print(f"  → This is expected - label should be generated in _prepare_model_data")

        return

    print(f"\nAugmented data shape: {df_is_aug.shape}")
    print(f"Augmented data columns: {list(df_is_aug.columns)}")

    # Check if ML columns exist
    ml_cols = ["ml_pred", "ml_signal", "ml_conf"]
    missing = [c for c in ml_cols if c not in df_is_aug.columns]
    if missing:
        print(f"\nERROR: Missing ML columns: {missing}")
        return

    print("\n✅ ML columns exist!")

    # Check prediction statistics
    ml_pred = df_is_aug["ml_pred"].values
    ml_signal = df_is_aug["ml_signal"].values
    ml_conf = df_is_aug["ml_conf"].values

    print(f"\nPrediction statistics:")
    print(f"  ml_pred range: [{ml_pred.min():.6f}, {ml_pred.max():.6f}]")
    print(f"  ml_pred mean: {ml_pred.mean():.6f}")
    print(f"  ml_pred std: {ml_pred.std():.6f}")
    print(f"  ml_pred non-zero: {np.sum(np.abs(ml_pred) > 0.0001)} / {len(ml_pred)} ({100*np.sum(np.abs(ml_pred) > 0.0001)/len(ml_pred):.1f}%)")

    print(f"\nSignal statistics:")
    print(f"  ml_signal range: [{ml_signal.min():.1f}, {ml_signal.max():.1f}]")
    print(f"  ml_signal unique values: {np.unique(ml_signal)}")
    print(f"  ml_signal counts: {np.bincount(ml_signal.astype(int) + 1)}")  # [-1, 0, 1]

    print(f"\nConfidence statistics:")
    print(f"  ml_conf range: [{ml_conf.min():.6f}, {ml_conf.max():.6f}]")
    print(f"  ml_conf mean: {ml_conf.mean():.6f}")
    print(f"  ml_conf > 0.35: {np.sum(ml_conf > 0.35)} / {len(ml_conf)} ({100*np.sum(ml_conf > 0.35)/len(ml_conf):.1f}%)")

    # Check market filter criteria
    print(f"\n📊 Market Filter Analysis:")
    if "volume" in df_is_aug.columns:
        vol = df_is_aug["volume"].values
        print(f"  volume > 3000: {np.sum(vol > 3000)} / {len(vol)} ({100*np.sum(vol > 3000)/len(vol):.1f}%)")
        print(f"  volume mean: {vol.mean():.0f}")

    # Check how many bars pass all filters
    passing = (
        (np.abs(ml_signal) > 0) &  # Has signal
        (ml_conf > 0.35) &  # Meets confidence threshold
        (df_is_aug["volume"] > 3000)  # Meets volume threshold
    )
    print(f"\n🎯 Bars passing ALL filters: {passing.sum()} / {len(passing)} ({100*passing.sum()/len(passing):.1f}%)")

    if passing.sum() > 0:
        print("\n✅ DIAGNOSIS: Predictions exist and some bars pass filters!")
        print("   Problem likely in strategy execution or order submission logic.")
    else:
        print("\n❌ DIAGNOSIS: No bars pass all filters!")
        if np.sum(np.abs(ml_signal) > 0) == 0:
            print("   → Problem: Model produces zero signals (all predictions below threshold)")
            print(f"   → Max |prediction| = {np.abs(ml_pred).max():.6f}")
            print(f"   → Decision threshold = 0.0001")
            if np.abs(ml_pred).max() < 0.0001:
                print("   → FIX: Predictions too small, threshold too high, or model not learning")
        elif np.sum(ml_conf > 0.35) == 0:
            print("   → Problem: All confidences below conf_floor=0.35")
            print(f"   → Max confidence = {ml_conf.max():.6f}")
        elif np.sum(df_is_aug["volume"] > 3000) == 0:
            print("   → Problem: All volumes below min_volume=3000")
            print(f"   → Max volume = {df_is_aug['volume'].max():.0f}")

    print("\n" + "="*80)
    print("Diagnosis complete!")

if __name__ == "__main__":
    main()
