#!/usr/bin/env python3
"""
IC Integration Verification Script

This script verifies that the complete IC integration is working correctly:
1. IC calculator functions
2. ClickHouse schema and views
3. Memory module functions
4. Data flow from wf_hpo → ClickHouse → memory

Usage:
    python tools/verify_ic_integration.py
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd


def test_ic_calculator():
    """Test 1: Verify IC calculator works"""
    print("\n" + "=" * 70)
    print("TEST 1: IC Calculator")
    print("=" * 70)

    try:
        from tools.oof_ic_calculator import compute_fold_ic, aggregate_oof_summaries_with_ic

        # Create sample data with known correlation
        np.random.seed(42)
        n = 100
        actual_returns = np.random.randn(n) * 0.02
        predictions = actual_returns + np.random.randn(n) * 0.01

        df_test = pd.DataFrame({
            'y_pred': predictions,
            'y_true': actual_returns
        })

        ic = compute_fold_ic(df_test)
        print(f"✅ Single fold IC: {ic:.4f}")

        # Test aggregation
        fold1 = {
            'sharpe': 1.5, 'profit_factor': 1.8, 'max_drawdown': -0.12,
            'win_rate': 0.62, 'total_return': 0.25, 'trades': 100,
            'df_oos': df_test
        }
        fold2 = {
            'sharpe': 1.3, 'profit_factor': 1.6, 'max_drawdown': -0.15,
            'win_rate': 0.58, 'total_return': 0.20, 'trades': 95,
            'df_oos': df_test
        }

        oos_summary = aggregate_oof_summaries_with_ic([fold1, fold2])
        print(f"✅ Aggregated oos_avg_ic: {oos_summary['oos_avg_ic']:.4f}")

        return True

    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False


def test_clickhouse_schema():
    """Test 2: Verify ClickHouse schema is deployed"""
    print("\n" + "=" * 70)
    print("TEST 2: ClickHouse Schema")
    print("=" * 70)

    try:
        from libs.db.clickhouse import ClickHouseDB

        ch = ClickHouseDB()

        # Check if tables exist
        tables = ch.query("SHOW TABLES FROM trading LIKE 'ml_%'")
        table_names = {row.get('name') for row in tables}

        required_tables = {'ml_eval_flat', 'ml_eval_with_delta', 'ml_model_stats_30d'}

        if required_tables.issubset(table_names):
            print(f"✅ All required tables exist: {required_tables}")
        else:
            missing = required_tables - table_names
            print(f"❌ Missing tables: {missing}")
            return False

        # Check if oos_avg_ic column exists in ml_eval_flat
        columns = ch.query("DESCRIBE trading.ml_eval_flat")
        column_names = {row.get('name') for row in columns}

        if 'oos_avg_ic' in column_names:
            print("✅ oos_avg_ic column exists in ml_eval_flat")
        else:
            print("❌ oos_avg_ic column missing in ml_eval_flat")
            return False

        # Check if delta_ic and is_success columns exist in ml_eval_with_delta
        columns = ch.query("DESCRIBE trading.ml_eval_with_delta")
        column_names = {row.get('name') for row in columns}

        required_cols = {'delta_ic', 'is_success', 'is_big_success'}
        if required_cols.issubset(column_names):
            print(f"✅ All required columns exist in ml_eval_with_delta: {required_cols}")
        else:
            missing = required_cols - column_names
            print(f"❌ Missing columns: {missing}")
            return False

        ch.close()
        return True

    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_memory_functions():
    """Test 3: Verify memory.py ClickHouse query functions"""
    print("\n" + "=" * 70)
    print("TEST 3: Memory Functions")
    print("=" * 70)

    try:
        from scripts.ai_trader.brain.policy.memory import (
            get_model_success_rate_from_ch,
            get_model_fail_streak_from_ch,
            get_model_stats_summary_from_ch
        )

        # Test with dummy parameters (will return 0.5 if no data)
        success_rate = get_model_success_rate_from_ch(
            symbol="TEST",
            model_family="transformer",
            horizon="1h",
            window_days=60
        )
        print(f"✅ get_model_success_rate_from_ch() works (returned: {success_rate:.2f})")

        fail_streak = get_model_fail_streak_from_ch(
            symbol="TEST",
            model_family="transformer",
            horizon="1h",
            window_days=60
        )
        print(f"✅ get_model_fail_streak_from_ch() works (returned: {fail_streak})")

        stats = get_model_stats_summary_from_ch(
            symbol="TEST",
            model_family="transformer",
            horizon="1h",
            window_days=60
        )
        print(f"✅ get_model_stats_summary_from_ch() works (keys: {list(stats.keys())})")

        return True

    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_wf_hpo_integration():
    """Test 4: Verify wf_hpo.py has IC integration"""
    print("\n" + "=" * 70)
    print("TEST 4: wf_hpo.py Integration")
    print("=" * 70)

    try:
        wf_hpo_path = PROJECT_ROOT / "wf_hpo.py"

        if not wf_hpo_path.exists():
            print("❌ wf_hpo.py not found")
            return False

        content = wf_hpo_path.read_text()

        # Check for IC calculator import
        if "from tools.oof_ic_calculator import compute_fold_ic" in content:
            print("✅ IC calculator import found")
        else:
            print("❌ IC calculator import missing")
            return False

        # Check for df_oos_for_ic
        if "df_oos_for_ic" in content:
            print("✅ df_oos_for_ic prediction capture found")
        else:
            print("❌ df_oos_for_ic missing")
            return False

        # Check for oos_avg_ic in summary
        if '"oos_avg_ic":' in content:
            print("✅ oos_avg_ic in summary dict found")
        else:
            print("❌ oos_avg_ic in summary dict missing")
            return False

        return True

    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False


def test_alpha_loop_integration():
    """Test 5: Verify alpha_loop.py has IC integration"""
    print("\n" + "=" * 70)
    print("TEST 5: alpha_loop.py Integration")
    print("=" * 70)

    try:
        alpha_loop_path = PROJECT_ROOT / "alpha_loop.py"

        if not alpha_loop_path.exists():
            print("❌ alpha_loop.py not found")
            return False

        content = alpha_loop_path.read_text()

        # Check for IC in checkpoint payload
        if '"oos_avg_ic"' in content:
            print("✅ oos_avg_ic in checkpoint payload found")
        else:
            print("❌ oos_avg_ic in checkpoint payload missing")
            return False

        return True

    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False


def main():
    """Run all verification tests"""
    print("\n" + "=" * 70)
    print("IC INTEGRATION VERIFICATION")
    print("=" * 70)
    print("\nVerifying all components of IC integration...")

    results = []

    # Run all tests
    results.append(("IC Calculator", test_ic_calculator()))
    results.append(("ClickHouse Schema", test_clickhouse_schema()))
    results.append(("Memory Functions", test_memory_functions()))
    results.append(("wf_hpo.py Integration", test_wf_hpo_integration()))
    results.append(("alpha_loop.py Integration", test_alpha_loop_integration()))

    # Print summary
    print("\n" + "=" * 70)
    print("VERIFICATION SUMMARY")
    print("=" * 70)

    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status:10} {test_name}")

    all_passed = all(result[1] for result in results)

    print("\n" + "=" * 70)
    if all_passed:
        print("🎉 ALL TESTS PASSED - IC Integration is complete!")
        print("=" * 70)
        return 0
    else:
        print("❌ SOME TESTS FAILED - Please review the errors above")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
