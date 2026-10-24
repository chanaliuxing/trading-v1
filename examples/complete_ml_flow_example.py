#!/usr/bin/env python3
"""
Complete ML Flow Example - End-to-End Integration
==================================================

This example demonstrates how to use all the upgraded components together:
- Label Engine: Standardized label computation
- Label Sanity: Quality checks on labels
- Sandbox Executor: Safe feature creation
- Dataset Gate: AI brain decision making
- Experiment Audit: Complete traceability

This is a REFERENCE IMPLEMENTATION showing the integration pattern.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime

import pandas as pd
import numpy as np

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import all our new components
from labels.label_engine import (
    LabelEngine,
    LabelRegistry,
    LabelSpec,
    LabelOutputSpec,
)
from labels.label_sanity import (
    LabelSanityConfig,
    run_label_sanity_checks,
    summarize_label_for_llm,
)
from features.sandbox_executor import apply_sandbox_features
from brain.contracts import BrainDatasetContext, BrainTrainingDecision
from brain.dataset_gate import DatasetGate
from experiments.audit import start_experiment_run, ExperimentRunLogger
from experiments.trial_guard import guard_trial_metrics


def create_sample_data(n_rows: int = 10000) -> pd.DataFrame:
    """
    Create sample OHLCV data for demonstration.

    In production, this would come from ClickHouse.
    """
    np.random.seed(42)

    # Generate realistic price data
    base_price = 100.0
    returns = np.random.randn(n_rows) * 0.02
    close = base_price * np.exp(np.cumsum(returns))

    high = close * (1 + np.abs(np.random.randn(n_rows)) * 0.01)
    low = close * (1 - np.abs(np.random.randn(n_rows)) * 0.01)
    open_ = close * (1 + np.random.randn(n_rows) * 0.005)
    volume = np.random.randint(100000, 500000, n_rows)

    # Calculate ATR for triple barrier
    tr = np.maximum(high - low, np.abs(high - np.roll(close, 1)))
    atr_14 = pd.Series(tr).rolling(14).mean().fillna(tr).values

    df = pd.DataFrame({
        'ts': pd.date_range('2024-01-01', periods=n_rows, freq='1min'),
        'open': open_,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
        'atr_14': atr_14,
    })

    return df


def step_1_compute_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Step 1: Use Label Engine to compute labels.

    This replaces LLM-generated target SQL.
    """
    print("\n" + "="*80)
    print("STEP 1: Compute Labels with Label Engine")
    print("="*80)

    # Initialize registry
    label_registry = LabelRegistry()
    label_engine = LabelEngine(label_registry)

    # Compute forward return label
    print("\n[1a] Computing ret_10m label...")
    df_labeled = label_engine.attach_label(
        df.copy(),
        label_id="ret_10m",
    )
    print(f"✓ Added column: ret_10m")
    print(f"  Sample values: {df_labeled['ret_10m'].head(5).values}")

    # Compute triple barrier label
    print("\n[1b] Computing tb_10m_atr1 label...")
    df_labeled = label_engine.attach_label(
        df_labeled,
        label_id="tb_10m_atr1",
    )
    print(f"✓ Added column: tb_10m_atr1")
    print(f"  Sample values: {df_labeled['tb_10m_atr1'].head(5).values}")

    print(f"\n✓ Labels computed successfully")
    print(f"  Total columns: {len(df_labeled.columns)}")
    print(f"  Shape: {df_labeled.shape}")

    return df_labeled


def step_2_create_sandbox_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Step 2: Create safe sandbox features.

    This allows LLM-proposed features with AST validation.
    """
    print("\n" + "="*80)
    print("STEP 2: Create Sandbox Features")
    print("="*80)

    # Define sandbox features (these could come from LLM)
    sandbox_defs = [
        {
            "alias": "close_volume_ratio",
            "expr": "close / (volume / 1000)",
        },
        {
            "alias": "high_low_range",
            "expr": "(high - low) / close",
        },
        {
            "alias": "close_over_open",
            "expr": "close / open - 1",
        },
        {
            "alias": "log_volume",
            "expr": "log(volume + 1)",
        },
    ]

    print(f"\n[2a] Applying {len(sandbox_defs)} sandbox features...")

    # Apply sandbox features
    df_with_features, applied = apply_sandbox_features(
        df,
        sandbox_defs,
        available_feature_cols=["close", "high", "low", "open", "volume", "atr_14"],
    )

    print(f"✓ Successfully applied {len(applied)} features:")
    for feat in applied:
        print(f"  - {feat['alias']}: {feat['expr']}")

    return df_with_features


def step_3_label_sanity_checks(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Step 3: Run label sanity checks.

    This ensures labels are healthy before training.
    """
    print("\n" + "="*80)
    print("STEP 3: Label Sanity Checks")
    print("="*80)

    # Check regression label
    print("\n[3a] Checking ret_10m label...")
    label_sanity_reg = run_label_sanity_checks(
        df,
        label_id="ret_10m",
        value_col="ret_10m",
        task_type="reg",
        cfg=LabelSanityConfig(min_samples=1000),  # Lower threshold for demo
    )

    print(f"✓ Regression label sanity:")
    print(f"  - Samples: {label_sanity_reg.n_samples}")
    print(f"  - NaN ratio: {label_sanity_reg.nan_ratio:.2%}")
    print(f"  - Mean: {label_sanity_reg.mean:.6f}")
    print(f"  - Std: {label_sanity_reg.std:.6f}")
    print(f"  - Status: {'✓ OK' if label_sanity_reg.is_ok else '✗ ISSUES'}")

    if label_sanity_reg.issues:
        print(f"  - Issues: {label_sanity_reg.issues}")

    # Check classification label
    print("\n[3b] Checking tb_10m_atr1 label...")

    # Create side column for classification
    tb_values = df["tb_10m_atr1"].values
    side = np.where(tb_values > 0, 1, np.where(tb_values < 0, -1, 0))
    df["tb_10m_atr1_side"] = side

    label_sanity_cls = run_label_sanity_checks(
        df,
        label_id="tb_10m_atr1",
        value_col="tb_10m_atr1",
        side_col="tb_10m_atr1_side",
        task_type="cls",
        cfg=LabelSanityConfig(min_samples=1000),
    )

    print(f"✓ Classification label sanity:")
    print(f"  - Samples: {label_sanity_cls.n_samples}")
    print(f"  - Positive: {label_sanity_cls.n_pos}")
    print(f"  - Negative: {label_sanity_cls.n_neg}")
    print(f"  - Neutral: {label_sanity_cls.n_zero}")
    print(f"  - Majority ratio: {label_sanity_cls.majority_ratio:.2%}")
    print(f"  - Status: {'✓ OK' if label_sanity_cls.is_ok else '✗ ISSUES'}")

    return {
        "regression": label_sanity_reg.to_dict(),
        "classification": label_sanity_cls.to_dict(),
    }


def step_4_dataset_gate_decision(label_sanity: Dict[str, Any]) -> BrainTrainingDecision:
    """
    Step 4: Get AI brain decision via Dataset Gate.

    This is a simplified version - real implementation would use LLM.
    """
    print("\n" + "="*80)
    print("STEP 4: Dataset Gate Decision")
    print("="*80)

    # Create dataset manifest
    dataset_manifest = {
        "dataset_sanity": {
            "ok": True,
            "n_rows": 10000,
            "overall_nan_ratio": 0.01,
        },
        "feature_sanity": {
            "ok": True,
            "bad_features": [],
        },
        "label_sanity": label_sanity["regression"],
    }

    # Create HPO grid
    hpo_grid = {
        "model_type": ["xgb", "lasso"],
        "feature_subset": [["close", "volume", "close_volume_ratio"]],
        "task": ["reg"],
    }

    print("\n[4a] Preparing context for AI brain...")
    print(f"  - Dataset: 10000 rows, {label_sanity['regression']['n_samples']} valid labels")
    print(f"  - Label: {label_sanity['regression']['label_id']}")
    print(f"  - Task: {label_sanity['regression']['task_type']}")

    # For demo, create a simple rule-based decision
    # In production, this would call DatasetGate.decide() with LLM
    label_ok = label_sanity["regression"]["is_ok"]

    if label_ok:
        decision = BrainTrainingDecision(
            action="train",
            reasons="Dataset and label quality checks passed. Proceeding with training.",
            severity="info",
            adjusted_hpo_grid=None,
            notes={"auto_decision": True},
        )
        print("\n✓ Brain Decision: TRAIN")
    else:
        decision = BrainTrainingDecision(
            action="abort",
            reasons=f"Label quality issues detected: {label_sanity['regression'].get('issues')}",
            severity="error",
            adjusted_hpo_grid=None,
            notes={"auto_decision": True},
        )
        print("\n✗ Brain Decision: ABORT")

    print(f"  - Action: {decision.action}")
    print(f"  - Severity: {decision.severity}")
    print(f"  - Reasons: {decision.reasons}")

    return decision


def step_5_experiment_audit(decision: BrainTrainingDecision, label_sanity: Dict[str, Any]):
    """
    Step 5: Create experiment audit trail.

    This logs everything for future analysis.
    """
    print("\n" + "="*80)
    print("STEP 5: Experiment Audit Trail")
    print("="*80)

    # Create experiment logger
    exp_logger = start_experiment_run(
        log_dir="logs",
        symbol="DEMO",
        table="demo_data",
        freq="1m",
        goal="Predict 10-minute forward return",
        horizon="10m",
        label_id="ret_10m",
        label_manifest=label_sanity["regression"],
        feature_recipe_id="demo_recipe_001",
        feature_recipe={
            "base_features": ["close", "volume"],
            "sandbox_derived": [
                {"alias": "close_volume_ratio", "expr": "close / (volume / 1000)"}
            ],
        },
        dataset_manifest={
            "label_sanity": label_sanity["regression"],
        },
        brain_decision={
            "action": decision.action,
            "reasons": decision.reasons,
            "severity": decision.severity,
        },
        hpo_grid={
            "model_type": ["xgb", "lasso"],
            "task": ["reg"],
        },
    )

    print(f"\n✓ Experiment logger created")
    print(f"  - Run ID: {exp_logger.run_id}")
    print(f"  - Log path: logs/experiments.jsonl")

    # Simulate logging some trials
    print("\n[5a] Logging simulated HPO trials...")

    for trial_id in range(3):
        metrics = {
            "sharpe": np.random.uniform(0.5, 2.0),
            "ic": np.random.uniform(0.01, 0.10),
            "trades": np.random.randint(50, 200),
            "win_rate": np.random.uniform(0.4, 0.6),
        }

        # Run trial guard
        ok, reasons = guard_trial_metrics(metrics)
        status = "ok" if ok else "skipped"

        exp_logger.log_trial(
            trial_id=trial_id,
            params={"model_type": "xgb" if trial_id % 2 == 0 else "lasso"},
            metrics=metrics,
            status=status,
            notes=";".join(reasons) if reasons else None,
        )

        print(f"  - Trial {trial_id}: {status} (Sharpe={metrics['sharpe']:.2f}, IC={metrics['ic']:.3f})")

    # Set summary metrics
    exp_logger.set_summary_metrics({
        "sharpe": 1.85,
        "ic": 0.075,
        "trades": 150,
    })

    # End experiment
    exp_logger.end_run(status="completed", error=None)

    print(f"\n✓ Experiment logged successfully")
    print(f"  - Status: completed")
    print(f"  - Trials: 3")

    return exp_logger


def main():
    """Run complete ML flow example."""
    print("\n" + "="*80)
    print(" COMPLETE ML FLOW EXAMPLE - END-TO-END INTEGRATION")
    print("="*80)
    print("\nThis example demonstrates:")
    print("  1. Label Engine: Compute standardized labels")
    print("  2. Sandbox Executor: Create safe features")
    print("  3. Label Sanity: Quality checks")
    print("  4. Dataset Gate: AI brain decision")
    print("  5. Experiment Audit: Complete traceability")
    print("\n" + "="*80)

    # Create sample data
    print("\n[0] Creating sample data...")
    df = create_sample_data(n_rows=10000)
    print(f"✓ Created {len(df)} rows of sample OHLCV data")

    # Step 1: Compute labels
    df_labeled = step_1_compute_labels(df)

    # Step 2: Create sandbox features
    df_with_features = step_2_create_sandbox_features(df_labeled)

    # Step 3: Label sanity checks
    label_sanity = step_3_label_sanity_checks(df_with_features)

    # Step 4: Dataset gate decision
    decision = step_4_dataset_gate_decision(label_sanity)

    # Step 5: Experiment audit
    if decision.action in ("train", "adjust_hpo"):
        exp_logger = step_5_experiment_audit(decision, label_sanity)
    else:
        print("\n✗ Training aborted by Dataset Gate - no experiment logged")

    # Final summary
    print("\n" + "="*80)
    print(" EXECUTION COMPLETE")
    print("="*80)
    print("\n✓ All steps executed successfully!")
    print("\nNext steps:")
    print("  - Integrate with existing wf_hpo.py for actual training")
    print("  - Add real LLM calls to DatasetGate")
    print("  - Connect to ClickHouse for real data")
    print("  - Run end-to-end integration tests")
    print("\n" + "="*80 + "\n")


if __name__ == "__main__":
    main()
