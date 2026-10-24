#!/usr/bin/env python3
"""
测试 F: Label 取值合理性检查

验证生成的 label 值没有极端异常（如全为 NaN、全为 0、超大值）

Usage:
    venv/bin/python tools/test_label_sanity.py \
      --date 2025-11-24 \
      --symbols TSLA,SPY \
      --label-table trading.features_1m_with_labels \
      --label-columns label_h5,label_h15,label_h30
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


def check_label_sanity(
    symbols: List[str],
    date: str,
    label_table: str = "trading.features_1m_with_labels",
    label_columns: List[str] = None,
    min_non_null_ratio: float = 0.95,
    mean_range: tuple = (-0.05, 0.05),
    min_std: float = 0.0,
    max_extreme_value: float = 0.5,
) -> Dict[str, Any]:
    """
    检查 label 列的取值合理性

    Returns:
        {
            symbol: {
                label_col: {
                    'total_count': int,
                    'non_null_count': int,
                    'non_null_ratio': float,
                    'mean': float,
                    'std': float,
                    'min': float,
                    'max': float,
                    'extreme_count': int,
                    'checks': {
                        'non_null_ok': bool,
                        'mean_ok': bool,
                        'std_ok': bool,
                        'extreme_ok': bool
                    },
                    'ok': bool,
                    'note': str
                }
            }
        }
    """
    if label_columns is None:
        label_columns = ['label_h5', 'label_h15', 'label_h30']

    client = get_client()
    results = {}

    print(f"Checking {len(symbols)} symbols × {len(label_columns)} labels for date {date}...")
    print()

    for symbol in symbols:
        results[symbol] = {}
        print(f"Checking {symbol}...")

        for label_col in label_columns:
            try:
                # 查询 label 列的统计信息
                query = f"""
                SELECT
                    count(*) as total_count,
                    countIf({label_col} IS NOT NULL) as non_null_count,
                    round(countIf({label_col} IS NOT NULL) / count(*), 4) as non_null_ratio,
                    round(avg({label_col}), 6) as mean,
                    round(stddevPop({label_col}), 6) as std,
                    round(min({label_col}), 6) as min_val,
                    round(max({label_col}), 6) as max_val,
                    countIf(abs({label_col}) > {max_extreme_value}) as extreme_count
                FROM {label_table} FINAL
                WHERE symbol = %(symbol)s
                  AND toDate(ts) = %(date)s
                """

                result = client.query(query, {'symbol': symbol, 'date': date})

                if not result:
                    results[symbol][label_col] = {
                        'total_count': 0,
                        'non_null_count': 0,
                        'non_null_ratio': 0.0,
                        'mean': None,
                        'std': None,
                        'min': None,
                        'max': None,
                        'extreme_count': 0,
                        'checks': {
                            'non_null_ok': False,
                            'mean_ok': False,
                            'std_ok': False,
                            'extreme_ok': False
                        },
                        'ok': False,
                        'note': 'no_data'
                    }
                    print(f"  ❌ {label_col}: No data")
                    continue

                row = result[0]
                total_count = row['total_count']
                non_null_count = row['non_null_count']
                non_null_ratio = row['non_null_ratio']
                mean = row['mean']
                std = row['std']
                min_val = row['min_val']
                max_val = row['max_val']
                extreme_count = row['extreme_count']

                # 检查 1: 非空率
                non_null_ok = non_null_ratio >= min_non_null_ratio

                # 检查 2: 均值在合理范围
                mean_ok = (mean is not None and
                          mean_range[0] <= mean <= mean_range[1])

                # 检查 3: 标准差 > 0
                std_ok = (std is not None and std > min_std)

                # 检查 4: 无极端值
                extreme_ok = extreme_count == 0

                # 综合判断
                ok = non_null_ok and mean_ok and std_ok and extreme_ok

                # 生成说明
                failures = []
                if not non_null_ok:
                    failures.append(f"non_null={non_null_ratio:.1%}<{min_non_null_ratio:.0%}")
                if not mean_ok:
                    failures.append(f"mean={mean:.4f} out of [{mean_range[0]}, {mean_range[1]}]")
                if not std_ok:
                    failures.append(f"std={std:.6f}<=0")
                if not extreme_ok:
                    failures.append(f"extreme_count={extreme_count}")

                if ok:
                    note = "all_checks_passed"
                else:
                    note = "; ".join(failures)

                results[symbol][label_col] = {
                    'total_count': total_count,
                    'non_null_count': non_null_count,
                    'non_null_ratio': non_null_ratio,
                    'mean': mean,
                    'std': std,
                    'min': min_val,
                    'max': max_val,
                    'extreme_count': extreme_count,
                    'checks': {
                        'non_null_ok': non_null_ok,
                        'mean_ok': mean_ok,
                        'std_ok': std_ok,
                        'extreme_ok': extreme_ok
                    },
                    'ok': ok,
                    'note': note
                }

                # 打印结果
                status = "✅" if ok else "❌"
                print(f"  {status} {label_col}: "
                      f"non_null={non_null_ratio:.1%}, "
                      f"mean={mean:.4f}, "
                      f"std={std:.4f}, "
                      f"range=[{min_val:.4f}, {max_val:.4f}]")
                if not ok:
                    print(f"     FAIL: {note}")

            except Exception as e:
                results[symbol][label_col] = {
                    'total_count': 0,
                    'non_null_count': 0,
                    'non_null_ratio': 0.0,
                    'mean': None,
                    'std': None,
                    'min': None,
                    'max': None,
                    'extreme_count': 0,
                    'checks': {
                        'non_null_ok': False,
                        'mean_ok': False,
                        'std_ok': False,
                        'extreme_ok': False
                    },
                    'ok': False,
                    'note': f"query_error: {str(e)}"
                }
                print(f"  ❌ {label_col}: Error: {str(e)}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Test F: Label sanity checker")
    parser.add_argument('--date', required=True, help='Date to check (YYYY-MM-DD)')
    parser.add_argument('--symbols', required=True, help='Comma-separated symbols (e.g., TSLA,SPY)')
    parser.add_argument('--label-table', default='trading.features_1m_with_labels',
                       help='Label table name (default: trading.features_1m_with_labels)')
    parser.add_argument('--label-columns',
                       default='label_h5,label_h15,label_h30',
                       help='Comma-separated label columns (default: label_h5,label_h15,label_h30)')
    parser.add_argument('--min-non-null-ratio', type=float, default=0.95,
                       help='Minimum non-null ratio (default: 0.95)')
    parser.add_argument('--mean-range', default='-0.05,0.05',
                       help='Acceptable mean range (default: -0.05,0.05)')
    parser.add_argument('--min-std', type=float, default=0.0,
                       help='Minimum std deviation (default: 0.0)')
    parser.add_argument('--max-extreme-value', type=float, default=0.5,
                       help='Maximum extreme value threshold (default: 0.5)')
    parser.add_argument('--output', help='Output JSON file path (optional)')

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',')]
    label_columns = [c.strip() for c in args.label_columns.split(',')]
    mean_range = tuple(float(x.strip()) for x in args.mean_range.split(','))

    print("=" * 80)
    print("Test F: Label Sanity Check")
    print("=" * 80)
    print(f"Date: {args.date}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Label table: {args.label_table}")
    print(f"Label columns: {', '.join(label_columns)}")
    print(f"Min non-null ratio: {args.min_non_null_ratio:.0%}")
    print(f"Mean range: {mean_range}")
    print(f"Min std: {args.min_std}")
    print(f"Max extreme value: {args.max_extreme_value}")
    print("=" * 80)
    print()

    results = check_label_sanity(
        symbols=symbols,
        date=args.date,
        label_table=args.label_table,
        label_columns=label_columns,
        min_non_null_ratio=args.min_non_null_ratio,
        mean_range=mean_range,
        min_std=args.min_std,
        max_extreme_value=args.max_extreme_value
    )

    # 统计
    total_checks = sum(len(label_results) for label_results in results.values())
    passed_checks = sum(
        sum(1 for label_result in label_results.values() if label_result['ok'])
        for label_results in results.values()
    )
    failed_checks = total_checks - passed_checks

    # 汇总输出
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total checks: {total_checks} (symbols × labels)")
    print(f"Passed: {passed_checks} ({passed_checks/total_checks*100:.1f}%)")
    print(f"Failed: {failed_checks} ({failed_checks/total_checks*100:.1f}%)")
    print()

    # 显示失败的检查
    failures = []
    for sym, label_results in results.items():
        for label_col, label_result in label_results.items():
            if not label_result['ok']:
                failures.append((sym, label_col, label_result))

    if failures:
        print("FAILURES:")
        for sym, label_col, label_result in failures:
            print(f"  ❌ {sym}.{label_col}: {label_result['note']}")
        print()

    # 按 symbol 汇总
    print("BY SYMBOL:")
    for sym in sorted(results.keys()):
        label_results = results[sym]
        passed = sum(1 for r in label_results.values() if r['ok'])
        total = len(label_results)
        status = "✅" if passed == total else "❌"
        print(f"  {status} {sym}: {passed}/{total} labels passed")
    print()

    # 输出 JSON
    output_data = {
        'test': 'label_sanity',
        'timestamp': datetime.now().isoformat(),
        'config': {
            'date': args.date,
            'symbols': symbols,
            'label_table': args.label_table,
            'label_columns': label_columns,
            'min_non_null_ratio': args.min_non_null_ratio,
            'mean_range': mean_range,
            'min_std': args.min_std,
            'max_extreme_value': args.max_extreme_value
        },
        'summary': {
            'total_checks': total_checks,
            'passed_checks': passed_checks,
            'failed_checks': failed_checks,
            'pass_rate': passed_checks / total_checks if total_checks > 0 else 0
        },
        'results': results
    }

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output_data, indent=2, default=str))
        print(f"Results written to: {output_path}")
    else:
        print(json.dumps(output_data, indent=2, default=str))

    # 退出码
    sys.exit(0 if failed_checks == 0 else 1)


if __name__ == "__main__":
    main()
