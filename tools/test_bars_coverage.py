#!/usr/bin/env python3
"""
测试 A: Bars 覆盖率检查

验证 trading.bars_1m_ibkr 是否按设计运行：
- RTH (09:30-16:00) 每个交易日应有 ≈390 条 bars
- DataHealer / bulk_backfill 补完后不应有大面积缺口

Usage:
    venv/bin/python tools/test_bars_coverage.py \
      --symbols TSLA,SPY,NVDA \
      --start 2025-11-20 \
      --end   2025-11-24 \
      --table trading.bars_1m_ibkr
"""
import sys
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.ch_pool import get_client


def check_bars_coverage(
    symbols: List[str],
    start_date: str,
    end_date: str,
    table: str = "trading.bars_1m_ibkr",
    expected_min: int = 380,  # Allow 10-bar tolerance
    expected_ideal: int = 390,
) -> List[Dict[str, Any]]:
    """
    检查每个 symbol × 日期的 bars 覆盖率

    Returns:
        List of dicts with: symbol, date, bars_count, expected_min, expected_ideal, ok, note
    """
    client = get_client()

    results = []

    # 生成日期范围
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')

    current = start
    dates = []
    while current <= end:
        dates.append(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)

    print(f"Checking {len(symbols)} symbols × {len(dates)} dates")
    print()

    for symbol in symbols:
        print(f"Checking {symbol}...")

        for date_str in dates:
            # 查询该 symbol 在该日期 RTH 时段的 bar 数量
            query = f"""
            SELECT count(*) as bars_count
            FROM {table}
            WHERE symbol = %(symbol)s
              AND toDate(ts_end) = %(date)s
              AND (toHour(ts_end) * 60 + toMinute(ts_end)) >= 570
              AND (toHour(ts_end) * 60 + toMinute(ts_end)) <= 960
              AND bar_sec = 60
            """

            try:
                result = client.query(query, {
                    'symbol': symbol,
                    'date': date_str
                })

                bars_count = result[0]['bars_count'] if result else 0

                # 判断是否符合预期
                ok = bars_count >= expected_min

                # 生成说明
                note = ""
                if bars_count == 0:
                    note = "no_data"
                elif bars_count < expected_min:
                    note = f"below_threshold (expect ≥{expected_min})"
                elif bars_count < expected_ideal:
                    note = f"acceptable (slightly below ideal {expected_ideal})"
                else:
                    note = "perfect"

                results.append({
                    'symbol': symbol,
                    'date': date_str,
                    'bars_count': bars_count,
                    'expected_min': expected_min,
                    'expected_ideal': expected_ideal,
                    'ok': ok,
                    'note': note
                })

            except Exception as e:
                results.append({
                    'symbol': symbol,
                    'date': date_str,
                    'bars_count': 0,
                    'expected_min': expected_min,
                    'expected_ideal': expected_ideal,
                    'ok': False,
                    'note': f"query_error: {str(e)}"
                })

    return results


def main():
    parser = argparse.ArgumentParser(description="Test A: Bars coverage checker")
    parser.add_argument('--symbols', required=True, help='Comma-separated symbols (e.g., TSLA,SPY,NVDA)')
    parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--table', default='trading.bars_1m_ibkr', help='ClickHouse table name')
    parser.add_argument('--expected-min', type=int, default=380, help='Minimum expected bars per day')
    parser.add_argument('--expected-ideal', type=int, default=390, help='Ideal bars per day')
    parser.add_argument('--output', help='Output JSON file path (optional)')

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',')]

    print("=" * 80)
    print("Test A: Bars Coverage Check")
    print("=" * 80)
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Period: {args.start} to {args.end}")
    print(f"Table: {args.table}")
    print(f"Expected: ≥{args.expected_min} bars/day (ideal: {args.expected_ideal})")
    print("=" * 80)
    print()

    results = check_bars_coverage(
        symbols=symbols,
        start_date=args.start,
        end_date=args.end,
        table=args.table,
        expected_min=args.expected_min,
        expected_ideal=args.expected_ideal
    )

    # 统计
    total = len(results)
    passed = sum(1 for r in results if r['ok'])
    failed = total - passed

    # 按 symbol 汇总
    symbol_stats = {}
    for r in results:
        sym = r['symbol']
        if sym not in symbol_stats:
            symbol_stats[sym] = {'total': 0, 'passed': 0, 'failed': 0}
        symbol_stats[sym]['total'] += 1
        if r['ok']:
            symbol_stats[sym]['passed'] += 1
        else:
            symbol_stats[sym]['failed'] += 1

    # 输出结果
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total checks: {total}")
    if total > 0:
        print(f"Passed: {passed} ({passed/total*100:.1f}%)")
        print(f"Failed: {failed} ({failed/total*100:.1f}%)")
    else:
        print("No checks performed (no dates in range)")
    print()

    print("By Symbol:")
    for sym, stats in sorted(symbol_stats.items()):
        status = "✅" if stats['failed'] == 0 else "❌"
        print(f"  {status} {sym}: {stats['passed']}/{stats['total']} passed")
    print()

    # 显示失败的检查
    failures = [r for r in results if not r['ok']]
    if failures:
        print("FAILURES:")
        for r in failures:
            print(f"  ❌ {r['symbol']} on {r['date']}: {r['bars_count']} bars ({r['note']})")
        print()

    # 输出 JSON
    output_data = {
        'test': 'bars_coverage',
        'timestamp': datetime.now().isoformat(),
        'config': {
            'symbols': symbols,
            'start': args.start,
            'end': args.end,
            'table': args.table,
            'expected_min': args.expected_min,
            'expected_ideal': args.expected_ideal
        },
        'summary': {
            'total': total,
            'passed': passed,
            'failed': failed,
            'pass_rate': passed / total if total > 0 else 0
        },
        'by_symbol': symbol_stats,
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
