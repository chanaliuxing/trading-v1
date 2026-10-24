#!/usr/bin/env python3
"""
测试 C: Features vs Bars 覆盖率检查

验证 features_1m 与 bars_1m_ibkr 的行数对齐，确保特征计算完整。

Usage:
    venv/bin/python tools/test_features_coverage.py \
      --date 2025-11-24 \
      --symbols TSLA,SPY,NVDA \
      --threshold 0.98
"""
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.ch_pool import get_client


def check_features_coverage(
    symbols: List[str],
    date: str,
    threshold: float = 0.98,
    features_table: str = "trading.features_1m"
) -> Dict[str, Any]:
    """
    检查特征表与 bars 表的覆盖率对齐

    Returns:
        {
            symbol: {
                'bars': int,
                'features': int,
                'coverage': float,
                'ok': bool,
                'note': str
            }
        }
    """
    client = get_client()
    results = {}

    print(f"Checking {len(symbols)} symbols for date {date}...")
    print()

    for symbol in symbols:
        print(f"Checking {symbol}...")

        try:
            # 1. 查询 bars 数量
            bars_query = """
            SELECT count(*) as cnt
            FROM trading.bars_1m_ibkr
            WHERE symbol = %(symbol)s
              AND toDate(ts_end) = %(date)s
              AND (toHour(ts_end) * 60 + toMinute(ts_end)) >= 570
              AND (toHour(ts_end) * 60 + toMinute(ts_end)) <= 960
              AND bar_sec = 60
            """

            bars_result = client.query(bars_query, {'symbol': symbol, 'date': date})
            bars_count = bars_result[0]['cnt'] if bars_result else 0

            # 2. 查询 features 数量（使用 FINAL）
            features_query = f"""
            SELECT count(*) as cnt
            FROM {features_table} FINAL
            WHERE symbol = %(symbol)s
              AND toDate(ts) = %(date)s
              AND (toHour(ts) * 60 + toMinute(ts)) >= 570
              AND (toHour(ts) * 60 + toMinute(ts)) <= 960
            """

            features_result = client.query(features_query, {'symbol': symbol, 'date': date})
            features_count = features_result[0]['cnt'] if features_result else 0

            # 3. 计算覆盖率
            if bars_count > 0:
                coverage = features_count / bars_count
            else:
                coverage = 0.0

            # 4. 判断是否通过
            ok = coverage >= threshold and bars_count > 0

            # 5. 生成说明
            if bars_count == 0:
                note = "no_bars_data"
            elif features_count == 0:
                note = "no_features_data"
            elif coverage >= 1.0:
                note = "perfect_alignment"
            elif coverage >= threshold:
                note = "acceptable"
            else:
                note = f"below_threshold ({coverage:.2%} < {threshold:.0%})"

            results[symbol] = {
                'bars': bars_count,
                'features': features_count,
                'coverage': coverage,
                'ok': ok,
                'note': note
            }

            # 打印结果
            status = "✅" if ok else "❌"
            print(f"  {status} Bars: {bars_count}, Features: {features_count}, Coverage: {coverage:.2%}")

        except Exception as e:
            results[symbol] = {
                'bars': 0,
                'features': 0,
                'coverage': 0.0,
                'ok': False,
                'note': f"query_error: {str(e)}"
            }
            print(f"  ❌ Error: {str(e)}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Test C: Features vs Bars coverage checker")
    parser.add_argument('--date', required=True, help='Date to check (YYYY-MM-DD)')
    parser.add_argument('--symbols', required=True, help='Comma-separated symbols (e.g., TSLA,SPY,NVDA)')
    parser.add_argument('--threshold', type=float, default=0.98, help='Minimum coverage threshold (default: 0.98)')
    parser.add_argument('--features-table', default='trading.features_1m', help='Features table name')
    parser.add_argument('--output', help='Output JSON file path (optional)')

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',')]

    print("=" * 80)
    print("Test C: Features vs Bars Coverage Check")
    print("=" * 80)
    print(f"Date: {args.date}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Threshold: {args.threshold:.0%}")
    print(f"Features table: {args.features_table}")
    print("=" * 80)
    print()

    results = check_features_coverage(
        symbols=symbols,
        date=args.date,
        threshold=args.threshold,
        features_table=args.features_table
    )

    # 统计
    total = len(results)
    passed = sum(1 for r in results.values() if r['ok'])
    failed = total - passed

    # 汇总输出
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total symbols: {total}")
    print(f"Passed: {passed} ({passed/total*100:.1f}%)")
    print(f"Failed: {failed} ({failed/total*100:.1f}%)")
    print()

    # 显示失败的检查
    failures = {sym: r for sym, r in results.items() if not r['ok']}
    if failures:
        print("FAILURES:")
        for sym, r in failures.items():
            print(f"  ❌ {sym}: {r['bars']} bars → {r['features']} features ({r['note']})")
        print()

    # 显示详细结果表格
    print("DETAILED RESULTS:")
    print(f"{'Symbol':<8} {'Bars':>6} {'Features':>8} {'Coverage':>10} {'Status':<10}")
    print("-" * 50)
    for sym, r in sorted(results.items()):
        status = "✅ PASS" if r['ok'] else "❌ FAIL"
        print(f"{sym:<8} {r['bars']:>6} {r['features']:>8} {r['coverage']:>9.1%} {status:<10}")
    print()

    # 输出 JSON
    output_data = {
        'test': 'features_coverage',
        'timestamp': datetime.now().isoformat(),
        'config': {
            'date': args.date,
            'symbols': symbols,
            'threshold': args.threshold,
            'features_table': args.features_table
        },
        'summary': {
            'total': total,
            'passed': passed,
            'failed': failed,
            'pass_rate': passed / total if total > 0 else 0
        },
        'results': results
    }

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output_data, indent=2))
        print(f"Results written to: {output_path}")
    else:
        print(json.dumps(output_data, indent=2))

    # 退出码
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
