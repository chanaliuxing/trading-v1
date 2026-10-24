#!/usr/bin/env python3
"""
Brain IC Integration Test - Validation Script

Checks if the test has met the four-dimensional success gates.

Usage:
    python tools/brain_test_validate.py --symbol TSLA
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def validate_clickhouse_gates(symbol: str) -> dict:
    """Query ClickHouse and validate against success gates."""
    try:
        from libs.db.clickhouse import ClickHouseDB
    except ImportError:
        print("❌ ClickHouse library not available")
        return None

    try:
        ch = ClickHouseDB()
    except Exception as e:
        print(f"❌ Cannot connect to ClickHouse: {e}")
        return None

    # Query latest evaluation
    query = f"""
    SELECT
        symbol,
        trial_id,
        oos_avg_sharpe,
        oos_avg_profit_factor,
        oos_avg_ic,
        oos_avg_max_drawdown,
        delta_sharpe,
        delta_ic,
        delta_max_dd,
        is_success,
        is_big_success,
        updated_at
    FROM trading.ml_eval_with_delta
    WHERE symbol = '{symbol}'
    ORDER BY updated_at DESC
    LIMIT 1
    """

    rows = ch.query(query)
    ch.close()

    if not rows or len(rows) == 0:
        print(f"❌ No evaluation data found for {symbol}")
        return None

    return rows[0]


def check_gates(data: dict) -> dict:
    """Check all success gates."""
    results = {}

    # Basic gates
    results['sharpe_ok'] = data.get('oos_avg_sharpe', 0) >= 1.2
    results['pf_ok'] = data.get('oos_avg_profit_factor', 0) >= 1.2
    results['ic_ok'] = data.get('oos_avg_ic', 0) >= 0.02

    # Delta gates
    delta_sharpe = data.get('delta_sharpe', 0)
    delta_ic = data.get('delta_ic', 0)
    results['delta_ok'] = (delta_sharpe >= 0.02) or (delta_ic >= 0.0)

    # DD gate
    results['dd_ok'] = data.get('delta_max_dd', 0) <= 0.03

    # Overall success
    results['is_success'] = data.get('is_success', 0) == 1

    # Stretch goal (big success)
    sharpe = data.get('oos_avg_sharpe', 0)
    pf = data.get('oos_avg_profit_factor', 0)
    ic = data.get('oos_avg_ic', 0)

    big_success_gates = (
        ((delta_sharpe >= 0.10) or (delta_ic >= 0.01))
        and (sharpe >= 1.5)
        and (pf >= 1.3)
        and (ic >= 0.03)
    )
    results['is_big_success'] = data.get('is_big_success', 0) == 1
    results['big_success_criteria_met'] = big_success_gates

    return results


def print_validation_report(symbol: str, data: dict, gates: dict):
    """Print comprehensive validation report."""
    print("\n" + "=" * 70)
    print("Brain IC Integration Test - Validation Report")
    print("=" * 70)
    print(f"\nSymbol: {symbol}")
    print(f"Trial ID: {data.get('trial_id', 'N/A')}")
    print(f"Updated: {data.get('updated_at', 'N/A')}")

    print("\n" + "-" * 70)
    print("Four-Dimensional Success Gates")
    print("-" * 70)

    def status_icon(passed: bool) -> str:
        return "✅" if passed else "❌"

    # Gate 1: Sharpe
    sharpe = data.get('oos_avg_sharpe', 0)
    print(f"{status_icon(gates['sharpe_ok'])} Sharpe ≥ 1.2")
    print(f"   Current: {sharpe:.4f}")

    # Gate 2: Profit Factor
    pf = data.get('oos_avg_profit_factor', 0)
    print(f"{status_icon(gates['pf_ok'])} Profit Factor ≥ 1.2")
    print(f"   Current: {pf:.4f}")

    # Gate 3: IC
    ic = data.get('oos_avg_ic', 0)
    print(f"{status_icon(gates['ic_ok'])} IC ≥ 0.02 ⭐")
    print(f"   Current: {ic:.4f}")

    # Gate 4: Delta improvement
    delta_sharpe = data.get('delta_sharpe', 0)
    delta_ic = data.get('delta_ic', 0)
    print(f"{status_icon(gates['delta_ok'])} (ΔSharpe ≥ 0.02 OR ΔIC ≥ 0.0)")
    print(f"   ΔSharpe: {delta_sharpe:.4f}")
    print(f"   ΔIC: {delta_ic:.4f}")

    # Gate 5: DD control
    delta_dd = data.get('delta_max_dd', 0)
    print(f"{status_icon(gates['dd_ok'])} ΔMaxDD ≤ 0.03")
    print(f"   Current: {delta_dd:.4f}")

    print("\n" + "-" * 70)
    print("Overall Result")
    print("-" * 70)

    is_success = gates['is_success']
    print(f"{status_icon(is_success)} is_success = {1 if is_success else 0}")

    if is_success:
        print("\n🎉 TEST PASSED - Four-dimensional success gates met!")
    else:
        print("\n❌ TEST NOT PASSED - Some gates failed")
        print("\nFailed gates:")
        for gate, passed in gates.items():
            if gate.endswith('_ok') and not passed:
                print(f"  - {gate.replace('_ok', '').replace('_', ' ').title()}")

    print("\n" + "-" * 70)
    print("Stretch Goal (Big Success)")
    print("-" * 70)

    is_big = gates['is_big_success']
    print(f"{status_icon(is_big)} is_big_success = {1 if is_big else 0}")

    if is_big:
        print("⭐ STRETCH GOAL ACHIEVED!")
        print("\nCriteria met:")
        print(f"  ✅ (ΔSharpe ≥ 0.10 OR ΔIC ≥ 0.01): ΔSharpe={delta_sharpe:.4f}, ΔIC={delta_ic:.4f}")
        print(f"  ✅ Sharpe ≥ 1.5: {sharpe:.4f}")
        print(f"  ✅ PF ≥ 1.3: {pf:.4f}")
        print(f"  ✅ IC ≥ 0.03: {ic:.4f}")
    else:
        print("Stretch goal not yet achieved")

    print("\n" + "=" * 70)


def count_cycles(symbol: str) -> int:
    """Count number of cycles from ml_actions.jsonl."""
    from datetime import date

    actions_file = Path("runs/ai_trader") / date.today().isoformat() / "ml_actions.jsonl"

    if not actions_file.exists():
        return 0

    import json
    count = 0

    with actions_file.open() as f:
        for line in f:
            try:
                action = json.loads(line)
                if action.get('symbol') == symbol and action.get('status') == 'ok':
                    count += 1
            except Exception:
                continue

    return count


def main():
    parser = argparse.ArgumentParser(description='Validate Brain IC Integration Test')
    parser.add_argument('--symbol', default='TSLA', help='Symbol to validate (default: TSLA)')
    args = parser.parse_args()

    print(f"\nValidating Brain IC Integration Test for {args.symbol}...")

    # Get data from ClickHouse
    data = validate_clickhouse_gates(args.symbol)

    if data is None:
        print("\n❌ Validation failed - no data available")
        return 1

    # Check gates
    gates = check_gates(data)

    # Print report
    print_validation_report(args.symbol, data, gates)

    # Count cycles
    cycles = count_cycles(args.symbol)
    print(f"\nCycles completed: {cycles}")

    if cycles > 6:
        print(f"⚠️  WARNING: Exceeded 6 cycles (actual: {cycles})")

    # Return exit code
    if gates['is_success']:
        print("\n✅ VALIDATION PASSED")
        return 0
    else:
        print("\n❌ VALIDATION FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
