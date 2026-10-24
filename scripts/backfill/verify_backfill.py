#!/usr/bin/env python3
"""
回补数据验证脚本

功能:
- 检查时间连续性（缺口检测）
- 验证数据量是否合理
- 对比回补数据质量
- 生成验证报告

用法:
    # 验证 TSLA 的2年回补数据
    python scripts/backfill/verify_backfill.py \
        --symbols TSLA AAPL NVDA \
        --start-date 2023-11-01 \
        --end-date 2025-11-01 \
        --table bars_1m_ibkr

    # 生成详细报告
    python scripts/backfill/verify_backfill.py \
        --symbols TSLA \
        --start-date 2023-11-01 \
        --end-date 2025-11-01 \
        --report out.json

    # 仅检查缺口（快速）
    python scripts/backfill/verify_backfill.py \
        --symbols TSLA \
        --start-date 2024-01-01 \
        --end-date 2024-12-31 \
        --check-gaps-only
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)


def parse_date(date_str: str) -> str:
    """验证并返回日期字符串 (YYYY-MM-DD)"""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}. Expected YYYY-MM-DD")


def check_data_coverage(
    ch: ClickHouseDB,
    symbol: str,
    start_date: str,
    end_date: str,
    table: str = "bars_1m_ibkr",
) -> Dict:
    """
    检查数据覆盖情况

    Returns:
        {
            "symbol": "TSLA",
            "bars_total": 123456,
            "earliest": "2023-11-01 09:30:00",
            "latest": "2025-10-31 16:00:00",
            "days_covered": 500,
            "avg_bars_per_day": 246.9,
            "expected_bars_per_day": 390,
            "coverage_ratio": 0.63
        }
    """
    query = f"""
    SELECT
        symbol,
        count() AS bars_total,
        min(ts_start) AS earliest,
        max(ts_start) AS latest,
        countDistinct(toDate(ts_start)) AS days_covered,
        bars_total / days_covered AS avg_bars_per_day
    FROM trading.{table}
    WHERE symbol = '{symbol}'
      AND ts_start >= '{start_date}'
      AND ts_start < '{end_date}'
    GROUP BY symbol
    """

    result = ch.client.query(query)
    if not result.result_rows:
        return {
            "symbol": symbol,
            "bars_total": 0,
            "earliest": None,
            "latest": None,
            "days_covered": 0,
            "avg_bars_per_day": 0,
            "expected_bars_per_day": 390,
            "coverage_ratio": 0,
        }

    row = result.result_rows[0]
    bars_total = row[1]
    earliest = str(row[2]) if row[2] else None
    latest = str(row[3]) if row[3] else None
    days_covered = row[4]
    avg_bars_per_day = float(row[5]) if row[5] else 0

    expected_bars_per_day = 390  # RTH 6.5 hours × 60 minutes
    coverage_ratio = avg_bars_per_day / expected_bars_per_day if expected_bars_per_day > 0 else 0

    return {
        "symbol": symbol,
        "bars_total": bars_total,
        "earliest": earliest,
        "latest": latest,
        "days_covered": days_covered,
        "avg_bars_per_day": round(avg_bars_per_day, 1),
        "expected_bars_per_day": expected_bars_per_day,
        "coverage_ratio": round(coverage_ratio, 3),
    }


def detect_gaps(
    ch: ClickHouseDB,
    symbol: str,
    start_date: str,
    end_date: str,
    table: str = "bars_1m_ibkr",
    min_gap_minutes: int = 2,
) -> List[Dict]:
    """
    检测时间缺口

    Args:
        min_gap_minutes: 最小缺口阈值（分钟）

    Returns:
        [
            {"start": "2024-01-15 10:30:00", "end": "2024-01-15 10:45:00", "gap_minutes": 15},
            ...
        ]
    """
    query = f"""
    WITH
        data AS (
            SELECT
                symbol,
                ts_start,
                lagInFrame(ts_start) OVER (PARTITION BY symbol ORDER BY ts_start) AS prev_ts
            FROM trading.{table}
            WHERE symbol = '{symbol}'
              AND ts_start >= '{start_date}'
              AND ts_start < '{end_date}'
        )
    SELECT
        prev_ts AS gap_start,
        ts_start AS gap_end,
        dateDiff('minute', prev_ts, ts_start) AS gap_minutes
    FROM data
    WHERE gap_minutes > {min_gap_minutes}
    ORDER BY gap_start
    LIMIT 100
    """

    result = ch.client.query(query)
    gaps = []

    for row in result.result_rows:
        gap_start, gap_end, gap_minutes = row
        gaps.append({
            "start": str(gap_start),
            "end": str(gap_end),
            "gap_minutes": gap_minutes,
        })

    return gaps


def check_backfill_quality(
    ch: ClickHouseDB,
    symbol: str,
    start_date: str,
    end_date: str,
    table: str = "bars_1m_ibkr",
) -> Dict:
    """
    检查回补数据质量

    Returns:
        {
            "backfill_bars": 100000,
            "total_bars": 120000,
            "backfill_ratio": 0.833,
            "null_vwap_pct": 0.05,
            "zero_volume_pct": 0.02
        }
    """
    query = f"""
    SELECT
        countIf(is_backfill = 1) AS backfill_bars,
        count() AS total_bars,
        backfill_bars / total_bars AS backfill_ratio,
        countIf(vwap IS NULL) / total_bars AS null_vwap_pct,
        countIf(volume = 0) / total_bars AS zero_volume_pct
    FROM trading.{table}
    WHERE symbol = '{symbol}'
      AND ts_start >= '{start_date}'
      AND ts_start < '{end_date}'
    """

    result = ch.client.query(query)
    if not result.result_rows:
        return {
            "backfill_bars": 0,
            "total_bars": 0,
            "backfill_ratio": 0,
            "null_vwap_pct": 0,
            "zero_volume_pct": 0,
        }

    row = result.result_rows[0]
    return {
        "backfill_bars": row[0],
        "total_bars": row[1],
        "backfill_ratio": round(float(row[2]), 3) if row[2] else 0,
        "null_vwap_pct": round(float(row[3]), 3) if row[3] else 0,
        "zero_volume_pct": round(float(row[4]), 3) if row[4] else 0,
    }


def print_verification_report(
    symbol: str,
    coverage: Dict,
    gaps: List[Dict],
    quality: Dict,
):
    """打印格式化的验证报告"""
    print("\n" + "=" * 80)
    print(f"  数据验证报告 - {symbol}")
    print("=" * 80)

    # 覆盖情况
    print("\n📊 数据覆盖:")
    print(f"   总 bars:     {coverage['bars_total']:,}")
    print(f"   时间范围:    {coverage['earliest']} ~ {coverage['latest']}")
    print(f"   覆盖天数:    {coverage['days_covered']} 天")
    print(f"   平均 bars/天: {coverage['avg_bars_per_day']} (预期: {coverage['expected_bars_per_day']})")
    print(f"   覆盖率:      {coverage['coverage_ratio'] * 100:.1f}%", end="")
    if coverage['coverage_ratio'] < 0.5:
        print(" ⚠️  偏低")
    elif coverage['coverage_ratio'] < 0.8:
        print(" ⚠️  中等")
    else:
        print(" ✅")

    # 缺口检测
    print(f"\n🔍 缺口检测:")
    if not gaps:
        print("   ✅ 未发现明显缺口（>2分钟）")
    else:
        print(f"   ⚠️  发现 {len(gaps)} 个缺口:")
        for gap in gaps[:10]:  # 最多显示10个
            print(f"      - {gap['start']} ~ {gap['end']} ({gap['gap_minutes']} 分钟)")
        if len(gaps) > 10:
            print(f"      ... 还有 {len(gaps) - 10} 个缺口")

    # 数据质量
    print(f"\n🎯 数据质量:")
    print(f"   回补数据:    {quality['backfill_bars']:,} / {quality['total_bars']:,} ({quality['backfill_ratio'] * 100:.1f}%)")
    print(f"   VWAP 缺失:  {quality['null_vwap_pct'] * 100:.2f}%", end="")
    if quality['null_vwap_pct'] > 0.1:
        print(" ⚠️")
    else:
        print()
    print(f"   零成交量:    {quality['zero_volume_pct'] * 100:.2f}%", end="")
    if quality['zero_volume_pct'] > 0.05:
        print(" ⚠️")
    else:
        print()

    # 总体评估
    print("\n📋 总体评估:")
    issues = []
    if coverage['coverage_ratio'] < 0.5:
        issues.append("数据覆盖率过低")
    if len(gaps) > 50:
        issues.append(f"缺口过多（{len(gaps)}个）")
    if quality['null_vwap_pct'] > 0.1:
        issues.append("VWAP 缺失率高")
    if quality['zero_volume_pct'] > 0.05:
        issues.append("零成交量比例高")

    if not issues:
        print("   ✅ 数据质量良好，可用于训练")
    else:
        print("   ⚠️  发现以下问题:")
        for issue in issues:
            print(f"      - {issue}")
        print("   建议: 重新回补或清洗数据")

    print("=" * 80 + "\n")


def verify_all_symbols(
    symbols: List[str],
    start_date: str,
    end_date: str,
    table: str = "bars_1m_ibkr",
    check_gaps: bool = True,
    report_file: Optional[str] = None,
):
    """验证多个股票的回补数据"""
    ch = ClickHouseDB()
    all_reports = {}

    for idx, symbol in enumerate(symbols, start=1):
        print(f"\n🔄 [{idx}/{len(symbols)}] 验证 {symbol}...")

        # 数据覆盖
        coverage = check_data_coverage(ch, symbol, start_date, end_date, table)

        # 缺口检测
        gaps = []
        if check_gaps and coverage['bars_total'] > 0:
            gaps = detect_gaps(ch, symbol, start_date, end_date, table)

        # 数据质量
        quality = check_backfill_quality(ch, symbol, start_date, end_date, table)

        # 打印报告
        print_verification_report(symbol, coverage, gaps, quality)

        # 保存到报告
        all_reports[symbol] = {
            "coverage": coverage,
            "gaps": gaps,
            "quality": quality,
            "verified_at": datetime.now().isoformat(),
        }

    # 写入报告文件
    if report_file:
        report_path = Path(report_file)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w") as f:
            json.dump(all_reports, f, indent=2, ensure_ascii=False)
        print(f"✅ 验证报告已保存: {report_file}")

    # 总结
    print("\n" + "=" * 80)
    print("  验证总结")
    print("=" * 80)
    ok_count = sum(1 for rep in all_reports.values() if rep["coverage"]["coverage_ratio"] >= 0.8)
    warn_count = sum(1 for rep in all_reports.values() if 0.5 <= rep["coverage"]["coverage_ratio"] < 0.8)
    bad_count = sum(1 for rep in all_reports.values() if rep["coverage"]["coverage_ratio"] < 0.5)

    print(f"\n✅ 良好: {ok_count} 个股票")
    print(f"⚠️  中等: {warn_count} 个股票")
    print(f"❌ 差:   {bad_count} 个股票")
    print()

    return all_reports


def main():
    parser = argparse.ArgumentParser(
        description="回补数据验证脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 验证 TSLA 的2年回补数据
  python scripts/backfill/verify_backfill.py \\
      --symbols TSLA AAPL NVDA \\
      --start-date 2023-11-01 \\
      --end-date 2025-11-01

  # 生成详细报告
  python scripts/backfill/verify_backfill.py \\
      --symbols TSLA \\
      --start-date 2023-11-01 \\
      --end-date 2025-11-01 \\
      --report verification_report.json

  # 仅检查缺口（快速）
  python scripts/backfill/verify_backfill.py \\
      --symbols TSLA \\
      --start-date 2024-01-01 \\
      --end-date 2024-12-31 \\
      --check-gaps-only
        """,
    )

    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="股票列表（空格分隔）",
    )

    parser.add_argument(
        "--start-date",
        type=str,
        required=True,
        help="开始日期 (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--end-date",
        type=str,
        required=True,
        help="结束日期 (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--table",
        type=str,
        default="bars_1m_ibkr",
        help="ClickHouse 表名（默认: bars_1m_ibkr）",
    )

    parser.add_argument(
        "--check-gaps-only",
        action="store_true",
        help="仅检查缺口（跳过其他验证）",
    )

    parser.add_argument(
        "--skip-gaps",
        action="store_true",
        help="跳过缺口检测（加速验证）",
    )

    parser.add_argument(
        "--report",
        type=str,
        help="保存验证报告到文件（JSON 格式）",
    )

    args = parser.parse_args()

    # 验证日期
    try:
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    # 标准化股票列表
    symbols = [s.upper().strip() for s in args.symbols]

    # 执行验证
    try:
        check_gaps = not args.skip_gaps

        # 仅检查缺口模式
        if args.check_gaps_only:
            ch = ClickHouseDB()
            for symbol in symbols:
                print(f"\n🔍 检测 {symbol} 的缺口...")
                gaps = detect_gaps(ch, symbol, start_date, end_date, args.table)
                if not gaps:
                    print(f"   ✅ 未发现缺口")
                else:
                    print(f"   ⚠️  发现 {len(gaps)} 个缺口:")
                    for gap in gaps[:20]:
                        print(f"      - {gap['start']} ~ {gap['end']} ({gap['gap_minutes']} 分钟)")
            sys.exit(0)

        # 完整验证
        reports = verify_all_symbols(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            table=args.table,
            check_gaps=check_gaps,
            report_file=args.report,
        )

        # 检查是否有严重问题
        has_issues = any(
            rep["coverage"]["coverage_ratio"] < 0.5 or len(rep["gaps"]) > 50
            for rep in reports.values()
        )

        if has_issues:
            print("⚠️  部分数据存在严重问题，建议重新回补")
            sys.exit(1)
        else:
            print("✅ 验证完成，数据质量良好")
            sys.exit(0)

    except KeyboardInterrupt:
        print("\n⚠️  验证已中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 验证失败: {e}", file=sys.stderr)
        logger.error("verification_error", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
