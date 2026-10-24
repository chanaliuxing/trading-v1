#!/usr/bin/env python3
"""
测试 G: Label - Feature 时间对齐检查

验证 target_labels 表中的 ts_features 列与 features_1m.ts 严格对齐。

Usage:
    venv/bin/python tools/test_label_feature_alignment.py \
      --date 2025-11-24 \
      --symbols TSLA,SPY,NVDA \
      --label-table trading.target_labels \
      --features-table trading.features_1m \
      --orphan-threshold 0.01
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


def check_label_feature_alignment(
    symbols: List[str],
    date: str,
    label_table: str = "trading.target_labels",
    features_table: str = "trading.features_1m",
    orphan_threshold: float = 0.01,
) -> Dict[str, Any]:
    """
    检查 label 表中的 ts_features 是否与 features 表对齐

    Returns:
        {
            symbol: {
                'label_count': int,
                'matched': int,
                'orphaned': int,
                'orphan_timestamps': List[str],
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
            # 1. 查询 label 表中所有 ts_features
            label_query = f"""
            SELECT DISTINCT ts_features
            FROM {label_table}
            WHERE symbol = %(symbol)s
              AND toDate(ts_features) = %(date)s
            ORDER BY ts_features
            """

            label_result = client.query(label_query, {'symbol': symbol, 'date': date})
            label_timestamps = [row['ts_features'] for row in label_result]
            label_count = len(label_timestamps)

            if label_count == 0:
                results[symbol] = {
                    'label_count': 0,
                    'matched': 0,
                    'orphaned': 0,
                    'orphan_timestamps': [],
                    'coverage': 0.0,
                    'ok': False,
                    'note': 'no_label_data'
                }
                print(f"  ⚠️ No label data found")
                continue

            # 2. 查询 features 表中所有时间戳（使用 FINAL 去重）
            features_query = f"""
            SELECT DISTINCT ts
            FROM {features_table} FINAL
            WHERE symbol = %(symbol)s
              AND toDate(ts) = %(date)s
            ORDER BY ts
            """

            features_result = client.query(features_query, {'symbol': symbol, 'date': date})
            feature_timestamps = set(row['ts'] for row in features_result)

            if not feature_timestamps:
                results[symbol] = {
                    'label_count': label_count,
                    'matched': 0,
                    'orphaned': label_count,
                    'orphan_timestamps': [str(ts) for ts in label_timestamps[:10]],  # 只取前10个
                    'coverage': 0.0,
                    'ok': False,
                    'note': 'no_feature_data'
                }
                print(f"  ❌ No feature data found")
                continue

            # 3. 检查对齐
            matched = 0
            orphaned = 0
            orphan_timestamps = []

            for ts_label in label_timestamps:
                if ts_label in feature_timestamps:
                    matched += 1
                else:
                    orphaned += 1
                    if len(orphan_timestamps) < 10:  # 只保存前10个
                        orphan_timestamps.append(str(ts_label))

            # 4. 计算覆盖率
            coverage = matched / label_count if label_count > 0 else 0.0

            # 5. 判断是否通过
            orphan_ratio = orphaned / label_count if label_count > 0 else 0.0
            ok = orphan_ratio <= orphan_threshold

            # 6. 生成说明
            if coverage >= 1.0:
                note = "perfect_alignment"
            elif orphan_ratio <= orphan_threshold:
                note = f"acceptable (orphan_ratio={orphan_ratio:.2%} <= {orphan_threshold:.0%})"
            else:
                note = f"too_many_orphans (orphan_ratio={orphan_ratio:.2%} > {orphan_threshold:.0%})"

            results[symbol] = {
                'label_count': label_count,
                'matched': matched,
                'orphaned': orphaned,
                'orphan_timestamps': orphan_timestamps,
                'coverage': coverage,
                'ok': ok,
                'note': note
            }

            # 打印结果
            status = "✅" if ok else "❌"
            print(f"  {status} Labels: {label_count}, Matched: {matched}, Orphaned: {orphaned}, Coverage: {coverage:.2%}")

            if orphan_timestamps:
                print(f"     First orphaned timestamps: {', '.join(orphan_timestamps[:3])}")

        except Exception as e:
            results[symbol] = {
                'label_count': 0,
                'matched': 0,
                'orphaned': 0,
                'orphan_timestamps': [],
                'coverage': 0.0,
                'ok': False,
                'note': f"query_error: {str(e)}"
            }
            print(f"  ❌ Error: {str(e)}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Test G: Label-feature alignment checker")
    parser.add_argument('--date', required=True, help='Date to check (YYYY-MM-DD)')
    parser.add_argument('--symbols', required=True, help='Comma-separated symbols (e.g., TSLA,SPY,NVDA)')
    parser.add_argument('--label-table', default='trading.target_labels', help='Label table name')
    parser.add_argument('--features-table', default='trading.features_1m', help='Features table name')
    parser.add_argument('--orphan-threshold', type=float, default=0.01, help='Maximum acceptable orphan ratio (default: 0.01 = 1%)')
    parser.add_argument('--output', help='Output JSON file path (optional)')

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',')]

    print("=" * 80)
    print("Test G: Label-Feature Alignment Check")
    print("=" * 80)
    print(f"Date: {args.date}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Label table: {args.label_table}")
    print(f"Features table: {args.features_table}")
    print(f"Orphan threshold: {args.orphan_threshold:.0%}")
    print("=" * 80)
    print()

    results = check_label_feature_alignment(
        symbols=symbols,
        date=args.date,
        label_table=args.label_table,
        features_table=args.features_table,
        orphan_threshold=args.orphan_threshold
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
            print(f"  ❌ {sym}: {r['label_count']} labels → {r['matched']} matched, "
                  f"{r['orphaned']} orphaned ({r['note']})")
            if r['orphan_timestamps']:
                print(f"     Orphaned timestamps: {', '.join(r['orphan_timestamps'][:5])}")
        print()

    # 显示详细结果表格
    print("DETAILED RESULTS:")
    print(f"{'Symbol':<8} {'Labels':>7} {'Matched':>8} {'Orphaned':>9} {'Coverage':>10} {'Status':<10}")
    print("-" * 70)
    for sym, r in sorted(results.items()):
        status = "✅ PASS" if r['ok'] else "❌ FAIL"
        print(f"{sym:<8} {r['label_count']:>7} {r['matched']:>8} {r['orphaned']:>9} "
              f"{r['coverage']:>9.1%} {status:<10}")
    print()

    # 输出 JSON
    output_data = {
        'test': 'label_feature_alignment',
        'timestamp': datetime.now().isoformat(),
        'config': {
            'date': args.date,
            'symbols': symbols,
            'label_table': args.label_table,
            'features_table': args.features_table,
            'orphan_threshold': args.orphan_threshold
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
        output_path.write_text(json.dumps(output_data, indent=2, default=str))
        print(f"Results written to: {output_path}")
    else:
        print(json.dumps(output_data, indent=2, default=str))

    # 退出码
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
