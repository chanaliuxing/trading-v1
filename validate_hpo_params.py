#!/usr/bin/env python3
"""
验证 HPO 参数 - OOS 测试
=======================

这个脚本用于验证 HPO 优化的参数在 Out-of-Sample (OOS) 数据上的表现

Usage:
    python validate_hpo_params.py \
        --config config/hpo_tsla_v7_2.json \
        --params test-fixed-code_best_params.json \
        --fold 0 \
        --period OOS
"""

import argparse
import json
import sys
from typing import Dict, Any

def load_json(path: str) -> Dict[str, Any]:
    """Load JSON file"""
    with open(path, 'r') as f:
        return json.load(f)

def main():
    parser = argparse.ArgumentParser(description="Validate HPO parameters on OOS data")
    parser.add_argument("--config", required=True, help="HPO config file")
    parser.add_argument("--params", required=True, help="Parameters file (JSON)")
    parser.add_argument("--fold", type=int, default=0, help="Fold index (default: 0)")
    parser.add_argument("--period", choices=["IS", "OOS"], default="OOS", help="Period to test (default: OOS)")

    args = parser.parse_args()

    print("\n" + "="*80)
    print("HPO PARAMETER VALIDATION - OOS Test")
    print("="*80)

    # Load config and params
    config = load_json(args.config)
    params = load_json(args.params)

    print(f"\nConfig: {args.config}")
    print(f"Parameters: {args.params}")
    print(f"Fold: {args.fold}")
    print(f"Period: {args.period}")

    # Get fold info
    folds = config.get("folds", [])
    if args.fold >= len(folds):
        print(f"\n❌ ERROR: Fold {args.fold} not found in config (only {len(folds)} folds)")
        sys.exit(1)

    fold = folds[args.fold]

    # Determine time range
    if args.period == "IS":
        start = fold["is_start"]
        end = fold["is_end"]
        print(f"\n📊 Testing on IN-SAMPLE data:")
    else:
        start = fold["oos_start"]
        end = fold["oos_end"]
        print(f"\n📊 Testing on OUT-OF-SAMPLE data:")

    print(f"   Period: {start} to {end}")

    # Show parameters
    print(f"\n🔧 Parameters to test:")
    for k, v in params.items():
        print(f"   {k:<25} = {v}")

    # Import and run evaluation
    print(f"\n🏃 Running backtest...")
    print(f"   (This will take a moment...)")

    try:
        # Import the evaluation function
        from wf_hpo import evaluate_trial

        # Prepare fold dict for evaluation
        fold_dict = {
            "fold_id": fold["fold_id"],
            "oos_start": start,
            "oos_end": end
        }

        # Run evaluation
        results = evaluate_trial(config, params, fold_dict)

        # Print results
        print(f"\n" + "="*80)
        print("RESULTS")
        print("="*80)

        metrics = [
            ("Sharpe Ratio", "sharpe", ".4f"),
            ("Total PnL", "pnl", ",.2f"),
            ("Number of Trades", "trades", "d"),
            ("Max Drawdown", "mdd", ".2%"),
            ("Win Rate", "win_rate", ".2%"),
            ("Sortino Ratio", "sortino", ".4f"),
            ("Omega Ratio", "omega", ".4f"),
            ("Calmar Ratio", "calmar", ".4f"),
            ("Profit Factor", "profit_factor", ".4f"),
        ]

        for label, key, fmt in metrics:
            value = results.get(key)
            if value is not None:
                if "%" in fmt:
                    print(f"{label:<20} = {value:{fmt}}")
                elif "$" in label or "PnL" in label:
                    print(f"{label:<20} = ${value:{fmt}}")
                else:
                    print(f"{label:<20} = {value:{fmt}}")

        print("="*80)

        # Interpretation
        sharpe = results.get("sharpe", 0)
        trades = results.get("trades", 0)
        mdd = results.get("mdd", 0)

        print(f"\n📝 Interpretation:")
        if sharpe > 1.5:
            print(f"   ✅ Excellent Sharpe ratio ({sharpe:.2f}) - parameters work well!")
        elif sharpe > 1.0:
            print(f"   ✅ Good Sharpe ratio ({sharpe:.2f}) - parameters are useful")
        elif sharpe > 0.5:
            print(f"   ⚠️  Modest Sharpe ratio ({sharpe:.2f}) - parameters show some value")
        else:
            print(f"   ❌ Low Sharpe ratio ({sharpe:.2f}) - parameters may not be robust")

        if trades < 5:
            print(f"   ⚠️  Only {trades} trades - sample size is small")
        else:
            print(f"   ✅ {trades} trades - reasonable sample size")

        if mdd > 0.10:
            print(f"   ⚠️  High drawdown ({mdd:.1%}) - risk management may need adjustment")
        else:
            print(f"   ✅ Acceptable drawdown ({mdd:.1%})")

        print("\n" + "="*80)

        # Save results
        output_file = f"{args.params.replace('.json', '')}_{args.period}_results.json"
        with open(output_file, 'w') as f:
            json.dump({
                "config": args.config,
                "params_file": args.params,
                "parameters": params,
                "fold": fold,
                "period": args.period,
                "time_range": {"start": start, "end": end},
                "results": results
            }, f, indent=2)

        print(f"💾 Results saved to: {output_file}")

    except ImportError as e:
        print(f"\n❌ ERROR: Cannot import evaluation function")
        print(f"   {e}")
        print(f"\n   Make sure wf_hpo.py and backtest_engine.py are in the current directory")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERROR during backtest:")
        print(f"   {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
