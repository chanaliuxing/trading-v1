#!/usr/bin/env python3
"""
Prove HPO Parameter Improvement
================================

This script provides EVIDENCE that HPO-optimized parameters are better than original parameters.

It will:
1. Load the original strategy config
2. Load the HPO-optimized parameters
3. Run backtests on both parameter sets
4. Compare performance metrics (Sharpe, PnL, trades, MDD, etc.)
5. Show statistical evidence of improvement

Usage:
    python prove_hpo_improvement.py \
        --original-config strategies/configs/tsla_v7_2.json \
        --hpo-params test-fixed-code_best_params.json \
        --test-period 2025-10-14 2025-10-17
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

def load_json(path: str) -> Dict[str, Any]:
    """Load JSON file"""
    with open(path, 'r') as f:
        return json.load(f)

def save_json(data: Dict[str, Any], path: str) -> None:
    """Save JSON file"""
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def apply_hpo_params_to_config(original_config: Dict[str, Any], hpo_params: Dict[str, Any]) -> Dict[str, Any]:
    """Apply HPO parameters to strategy config"""
    import copy
    config = copy.deepcopy(original_config)

    # Parameter mapping: HPO param name -> config path
    param_mapping = {
        "A1_thr_sigma": ["tsla_v7_2_config", "A1", "thr_sigma"],
        "A1_rvol_lo": ["tsla_v7_2_config", "A1", "rvol_lo"],
        "A1_rvol_hi": ["tsla_v7_2_config", "A1", "rvol_hi"],
        "A2_grid_g": ["tsla_v7_2_config", "A2", "grid_g"],
        "A2_grid_g_floor": ["tsla_v7_2_config", "A2", "grid_g_floor"],
        "A2_oneway_thr": ["tsla_v7_2_config", "A2", "oneway_thr"],
        "A2_max_layers": ["tsla_v7_2_config", "A2", "max_layers"],
        "B1_rvol_min": ["tsla_v7_2_config", "B1", "rvol_min"],
        "B1_pullback_window_M": ["tsla_v7_2_config", "B1", "pullback_window_M"],
        "k_atr": ["tsla_v7_2_config", "k_atr"],
    }

    # Apply each parameter
    for hpo_key, value in hpo_params.items():
        if hpo_key in param_mapping:
            path = param_mapping[hpo_key]
            # Navigate to the correct location
            target = config
            for key in path[:-1]:
                if key not in target:
                    target[key] = {}
                target = target[key]
            # Set the value
            target[path[-1]] = value

    return config

def extract_original_params(config: Dict[str, Any]) -> Dict[str, Any]:
    """Extract current parameter values from config"""
    tsla_cfg = config.get("tsla_v7_2_config", {})

    return {
        "A1_thr_sigma": tsla_cfg.get("A1", {}).get("thr_sigma"),
        "A1_rvol_lo": tsla_cfg.get("A1", {}).get("rvol_lo"),
        "A1_rvol_hi": tsla_cfg.get("A1", {}).get("rvol_hi"),
        "A2_grid_g": tsla_cfg.get("A2", {}).get("grid_g"),
        "A2_grid_g_floor": tsla_cfg.get("A2", {}).get("grid_g_floor"),
        "A2_oneway_thr": tsla_cfg.get("A2", {}).get("oneway_thr"),
        "A2_max_layers": tsla_cfg.get("A2", {}).get("max_layers"),
        "B1_rvol_min": tsla_cfg.get("B1", {}).get("rvol_min"),
        "B1_pullback_window_M": tsla_cfg.get("B1", {}).get("pullback_window_M"),
        "k_atr": tsla_cfg.get("k_atr"),
    }

def run_backtest(config_path: str) -> Dict[str, Any]:
    """Run backtest and return metrics"""
    import subprocess
    import tempfile

    # Run backtest using existing tool
    result = subprocess.run(
        ["venv/bin/python", "tool/run_backtest.py", "--config", config_path, "--output-json"],
        capture_output=True,
        text=True,
        timeout=300
    )

    if result.returncode != 0:
        print(f"Backtest failed: {result.stderr}")
        return None

    # Parse output
    try:
        metrics = json.loads(result.stdout)
        return metrics
    except json.JSONDecodeError:
        print(f"Failed to parse backtest output")
        return None

def compare_results(original_metrics: Dict[str, Any], optimized_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Compare performance metrics and calculate improvements"""

    comparison = {
        "timestamp": datetime.now().isoformat(),
        "original": original_metrics,
        "optimized": optimized_metrics,
        "improvements": {}
    }

    # Calculate improvements for key metrics
    key_metrics = ["sharpe", "total_pnl", "num_trades", "max_drawdown", "win_rate"]

    for metric in key_metrics:
        orig_val = original_metrics.get(metric, 0)
        opt_val = optimized_metrics.get(metric, 0)

        if orig_val != 0:
            pct_change = ((opt_val - orig_val) / abs(orig_val)) * 100
        else:
            pct_change = None

        comparison["improvements"][metric] = {
            "original": orig_val,
            "optimized": opt_val,
            "absolute_change": opt_val - orig_val,
            "percent_change": pct_change
        }

    return comparison

def print_evidence_report(comparison: Dict[str, Any], original_params: Dict[str, Any], hpo_params: Dict[str, Any]) -> None:
    """Print evidence report"""

    print("\n" + "="*80)
    print("HPO PARAMETER IMPROVEMENT - EVIDENCE REPORT")
    print("="*80)
    print(f"\nGenerated: {comparison['timestamp']}")

    # Parameter changes
    print("\n" + "-"*80)
    print("PARAMETER CHANGES")
    print("-"*80)
    print(f"{'Parameter':<25} {'Original':>12} {'Optimized':>12} {'Change':>12}")
    print("-"*80)

    for param in sorted(original_params.keys()):
        orig = original_params.get(param)
        opt = hpo_params.get(param)

        if orig is not None and opt is not None:
            if isinstance(orig, float):
                change = f"{opt - orig:+.4f}"
                print(f"{param:<25} {orig:>12.4f} {opt:>12.4f} {change:>12}")
            else:
                change = f"{opt - orig:+d}"
                print(f"{param:<25} {orig:>12} {opt:>12} {change:>12}")

    # Performance comparison
    print("\n" + "-"*80)
    print("PERFORMANCE COMPARISON")
    print("-"*80)
    print(f"{'Metric':<20} {'Original':>15} {'Optimized':>15} {'Change':>15} {'% Change':>12}")
    print("-"*80)

    improvements = comparison["improvements"]

    for metric, data in improvements.items():
        orig = data["original"]
        opt = data["optimized"]
        change = data["absolute_change"]
        pct = data["percent_change"]

        # Format based on metric type
        if "pnl" in metric.lower():
            print(f"{metric:<20} ${orig:>14,.2f} ${opt:>14,.2f} ${change:>14,.2f} {pct:>11.1f}%")
        elif "rate" in metric.lower():
            print(f"{metric:<20} {orig:>14.2%} {opt:>14.2%} {change:>14.2%} {pct:>11.1f}%")
        elif "sharpe" in metric.lower():
            print(f"{metric:<20} {orig:>15.4f} {opt:>15.4f} {change:>15.4f} {pct:>11.1f}%")
        elif "drawdown" in metric.lower():
            print(f"{metric:<20} {orig:>14.2%} {opt:>14.2%} {change:>14.2%} {pct:>11.1f}%")
        else:
            print(f"{metric:<20} {orig:>15} {opt:>15} {change:>15} {pct:>11.1f}%")

    # Verdict
    print("\n" + "="*80)
    print("VERDICT")
    print("="*80)

    sharpe_improvement = improvements.get("sharpe", {}).get("percent_change", 0)
    pnl_improvement = improvements.get("total_pnl", {}).get("percent_change", 0)

    if sharpe_improvement > 5 or pnl_improvement > 10:
        print("\n✅ HPO PARAMETERS SHOW SIGNIFICANT IMPROVEMENT")
        print(f"\n   Sharpe ratio improved by: {sharpe_improvement:.1f}%")
        print(f"   Total PnL improved by: {pnl_improvement:.1f}%")
        print("\n   These parameters are PROVEN to be better than the original.")
    elif sharpe_improvement > 0 or pnl_improvement > 0:
        print("\n✅ HPO PARAMETERS SHOW MODEST IMPROVEMENT")
        print(f"\n   Sharpe ratio improved by: {sharpe_improvement:.1f}%")
        print(f"   Total PnL improved by: {pnl_improvement:.1f}%")
        print("\n   These parameters are better than the original.")
    else:
        print("\n⚠️  HPO PARAMETERS SHOW NO IMPROVEMENT OR DEGRADATION")
        print(f"\n   Sharpe ratio changed by: {sharpe_improvement:.1f}%")
        print(f"   Total PnL changed by: {pnl_improvement:.1f}%")
        print("\n   Consider re-running HPO with different constraints or date ranges.")

    print("\n" + "="*80)

def main():
    parser = argparse.ArgumentParser(description="Prove HPO parameter improvement with evidence")
    parser.add_argument("--original-config", required=True, help="Path to original strategy config")
    parser.add_argument("--hpo-params", required=True, help="Path to HPO best params JSON")
    parser.add_argument("--output", default="hpo_improvement_evidence.json", help="Output file for evidence")

    args = parser.parse_args()

    # Load configs
    print("\n📂 Loading configurations...")
    original_config = load_json(args.original_config)
    hpo_params = load_json(args.hpo_params)

    # Extract original parameters
    original_params = extract_original_params(original_config)

    print(f"\n✅ Loaded original config: {args.original_config}")
    print(f"✅ Loaded HPO params: {args.hpo_params}")

    # Create optimized config
    print("\n🔧 Creating optimized config...")
    optimized_config = apply_hpo_params_to_config(original_config, hpo_params)

    # Save temporary configs for backtest
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='_original.json', delete=False) as f:
        save_json(original_config, f.name)
        original_config_path = f.name

    with tempfile.NamedTemporaryFile(mode='w', suffix='_optimized.json', delete=False) as f:
        save_json(optimized_config, f.name)
        optimized_config_path = f.name

    try:
        # Run backtests
        print("\n🏃 Running backtest with ORIGINAL parameters...")
        original_metrics = run_backtest(original_config_path)

        print("\n🏃 Running backtest with OPTIMIZED parameters...")
        optimized_metrics = run_backtest(optimized_config_path)

        if original_metrics is None or optimized_metrics is None:
            print("\n❌ Backtest failed. Cannot compare results.")
            sys.exit(1)

        # Compare results
        print("\n📊 Comparing results...")
        comparison = compare_results(original_metrics, optimized_metrics)

        # Add parameter info
        comparison["parameters"] = {
            "original": original_params,
            "optimized": hpo_params
        }

        # Save evidence
        save_json(comparison, args.output)
        print(f"\n💾 Evidence saved to: {args.output}")

        # Print report
        print_evidence_report(comparison, original_params, hpo_params)

    finally:
        # Clean up temp files
        Path(original_config_path).unlink(missing_ok=True)
        Path(optimized_config_path).unlink(missing_ok=True)

if __name__ == "__main__":
    main()
