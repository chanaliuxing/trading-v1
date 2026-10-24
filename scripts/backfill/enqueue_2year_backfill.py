#!/usr/bin/env python3
"""
2年历史数据回补 - 入队脚本

功能:
- 按月切分时间范围（避免单次任务过大）
- 调用 ml_scheduler.enqueue_job() 入队 bulk_backfill 作业
- 输出预估完成时间和监控命令

用法:
    # 回补 TSLA/AAPL/NVDA 2年的 1m bars
    python scripts/backfill/enqueue_2year_backfill.py \
        --symbols TSLA AAPL NVDA \
        --start-date 2023-11-01 \
        --end-date 2025-11-01 \
        --bar-size 1m \
        --priority 70

    # 回补所有配置的 universe symbols
    python scripts/backfill/enqueue_2year_backfill.py \
        --use-universe \
        --start-date 2023-01-01 \
        --end-date 2025-01-01 \
        --bar-size 1m
"""

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ai_trader.ml_scheduler import enqueue_job
from libs.config import config as _cfg
from libs.logger import get_logger

logger = get_logger(__name__)


def parse_date(date_str: str) -> date:
    """解析日期字符串 (YYYY-MM-DD)"""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}. Expected YYYY-MM-DD")


def month_range(start: date, end: date) -> List[tuple]:
    """
    生成按月切分的日期范围列表

    Args:
        start: 开始日期
        end: 结束日期

    Returns:
        [(start1, end1), (start2, end2), ...]
        每个元组代表一个月的范围（前闭后开）
    """
    from dateutil.relativedelta import relativedelta

    ranges = []
    current = start

    while current < end:
        # 计算当前月的最后一天
        next_month = current + relativedelta(months=1)
        month_end = min(next_month - timedelta(days=1), end - timedelta(days=1))

        ranges.append((current, month_end))
        current = next_month

    return ranges


def enqueue_backfill_jobs(
    symbols: List[str],
    start_date: date,
    end_date: date,
    bar_size: str = "1m",
    priority: int = 70,
    max_days_per_run: int = 31,
    sleep_sec_between_days: float = 0.25,
    dry_run: bool = False,
) -> int:
    """
    入队多个 bulk_backfill 作业（按月切分）

    Args:
        symbols: 股票列表
        start_date: 开始日期
        end_date: 结束日期
        bar_size: Bar 大小 (1m/5m/1h/1d)
        priority: 优先级（70=bulk_backfill 默认）
        max_days_per_run: 单次运行最大天数（安全限制）
        sleep_sec_between_days: 每天间隔（速率控制）
        dry_run: 仅打印，不实际入队

    Returns:
        入队的作业数量
    """
    ranges = month_range(start_date, end_date)

    print("=" * 70)
    print("  2年历史数据回补 - 作业入队")
    print("=" * 70)
    print(f"📊 股票列表: {', '.join(symbols)}")
    print(f"📅 时间范围: {start_date} ~ {end_date} ({(end_date - start_date).days} 天)")
    print(f"⏰ Bar 大小: {bar_size}")
    print(f"📦 分月切分: {len(ranges)} 个作业")
    print(f"🔢 优先级: {priority}")
    print(f"⚙️  最大天数/次: {max_days_per_run}")
    print(f"⏱️  速率控制: {sleep_sec_between_days}s/天")
    print("=" * 70)
    print()

    enqueued_count = 0
    for idx, (month_start, month_end) in enumerate(ranges, start=1):
        job = {
            "job_type": "bulk_backfill",
            "priority": priority,
            "params": {
                "symbols": symbols,
                "start_date": str(month_start),
                "end_date": str(month_end),
                "bar_size": bar_size,
                "max_days_per_run": max_days_per_run,
                "sleep_sec_between_days": sleep_sec_between_days,
            },
        }

        days_in_range = (month_end - month_start).days + 1

        if dry_run:
            print(f"[DRY-RUN] Job {idx}/{len(ranges)}: {month_start} ~ {month_end} ({days_in_range} 天)")
            print(f"           Params: {job['params']}")
        else:
            try:
                job_id = enqueue_job(job)
                print(f"✅ Job {idx}/{len(ranges)}: {job_id}")
                print(f"   📅 {month_start} ~ {month_end} ({days_in_range} 天)")
                enqueued_count += 1
            except Exception as e:
                print(f"❌ Job {idx}/{len(ranges)} failed: {e}", file=sys.stderr)
                logger.error("enqueue_failed", month=str(month_start), error=str(e), exc_info=True)

    print()
    print("=" * 70)
    print(f"✅ 入队完成: {enqueued_count}/{len(ranges)} 个作业")
    print("=" * 70)

    # 预估完成时间
    est_time_per_job = 15  # 秒（保守估计）
    est_total_sec = enqueued_count * est_time_per_job
    est_total_min = est_total_sec / 60

    print()
    print("📊 预估信息:")
    print(f"   ⏱️  单作业耗时: ~{est_time_per_job} 秒")
    print(f"   ⏳ 总预估时间: ~{est_total_min:.1f} 分钟")
    print(f"   ⚠️  注意: 如果 ML 槽繁忙，实际时间会更长")
    print()

    print("📺 监控命令:")
    print(f"   # 查看队列状态")
    print(f"   tail -f runs/ai_trader/queue/ml_jobs.jsonl | jq 'select(.job_type==\"bulk_backfill\")'")
    print()
    print(f"   # 查看执行日志")
    print(f"   tail -f runs/ai_trader/{date.today().isoformat()}/ml_events.jsonl | jq 'select(.job_type==\"bulk_backfill\")'")
    print()
    print(f"   # 或使用监控脚本")
    print(f"   python scripts/backfill/monitor_backfill.py")
    print()

    return enqueued_count


def main():
    parser = argparse.ArgumentParser(
        description="2年历史数据回补 - 入队脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 回补 TSLA/AAPL/NVDA 2年的 1m bars
  python scripts/backfill/enqueue_2year_backfill.py \\
      --symbols TSLA AAPL NVDA \\
      --start-date 2023-11-01 \\
      --end-date 2025-11-01 \\
      --bar-size 1m

  # 回补所有 universe symbols
  python scripts/backfill/enqueue_2year_backfill.py \\
      --use-universe \\
      --start-date 2023-01-01 \\
      --end-date 2025-01-01

  # 仅查看将要入队的作业（不实际执行）
  python scripts/backfill/enqueue_2year_backfill.py \\
      --symbols TSLA \\
      --start-date 2024-01-01 \\
      --end-date 2024-12-31 \\
      --dry-run
        """,
    )

    parser.add_argument(
        "--symbols",
        nargs="+",
        help="股票列表（空格分隔）",
    )

    parser.add_argument(
        "--use-universe",
        action="store_true",
        help="使用配置文件中的 universe symbols",
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
        "--bar-size",
        type=str,
        default="1m",
        choices=["1m", "5m", "10m", "15m", "30m", "1h", "1d"],
        help="Bar 大小（默认: 1m）",
    )

    parser.add_argument(
        "--priority",
        type=int,
        default=70,
        help="作业优先级（默认: 70）",
    )

    parser.add_argument(
        "--max-days-per-run",
        type=int,
        default=31,
        help="单次运行最大天数（默认: 31）",
    )

    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=0.25,
        help="每天间隔秒数（默认: 0.25）",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印作业信息，不实际入队",
    )

    args = parser.parse_args()

    # 确定股票列表
    if args.use_universe:
        try:
            symbols = [str(s) for s in (_cfg.universe or [])]
            if not symbols:
                print("❌ 配置文件中未找到 universe symbols", file=sys.stderr)
                sys.exit(1)
            print(f"📊 使用 universe symbols: {', '.join(symbols)}")
        except Exception as e:
            print(f"❌ 读取 universe 失败: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.symbols:
        symbols = [s.upper().strip() for s in args.symbols]
    else:
        print("❌ 必须指定 --symbols 或 --use-universe", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    # 解析日期
    try:
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    # 验证日期范围
    if end_date <= start_date:
        print("❌ 结束日期必须晚于开始日期", file=sys.stderr)
        sys.exit(1)

    days_total = (end_date - start_date).days
    if days_total > 1000:
        print(f"⚠️  警告: 回补范围过大（{days_total} 天），建议分批执行", file=sys.stderr)
        confirm = input("是否继续？(y/N): ")
        if confirm.lower() != "y":
            print("❌ 已取消")
            sys.exit(0)

    # 执行入队
    try:
        count = enqueue_backfill_jobs(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            bar_size=args.bar_size,
            priority=args.priority,
            max_days_per_run=args.max_days_per_run,
            sleep_sec_between_days=args.sleep_sec,
            dry_run=args.dry_run,
        )

        if not args.dry_run:
            print()
            print("🎉 回补作业已全部入队！ML 调度器将自动处理。")
            print()
            print("📝 后续步骤:")
            print("   1. 监控回补进度（使用上方监控命令）")
            print("   2. 验证数据完整性（python scripts/backfill/verify_backfill.py）")
            print("   3. 生成特征（enqueue feature_build 作业）")
            print()

    except KeyboardInterrupt:
        print("\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 入队失败: {e}", file=sys.stderr)
        logger.error("enqueue_error", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
