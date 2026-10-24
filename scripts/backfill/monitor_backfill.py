#!/usr/bin/env python3
"""
回补进度监控脚本

功能:
- 实时监控 bulk_backfill 作业队列状态
- 显示 ClickHouse 数据量增长
- 检测异常和失败作业

用法:
    # 持续监控（每30秒刷新）
    python scripts/backfill/monitor_backfill.py

    # 指定刷新间隔
    python scripts/backfill/monitor_backfill.py --interval 60

    # 仅显示当前状态（不循环）
    python scripts/backfill/monitor_backfill.py --once
"""

import argparse
import json
import sys
import time
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)


def load_queue_status(queue_file: Optional[Path] = None) -> Dict:
    """
    从 ml_jobs.jsonl 加载队列状态

    Returns:
        {
            "queued": [...],
            "running": [...],
            "completed": [...],
            "failed": [...]
        }
    """
    if queue_file is None:
        queue_dir = Path("runs/ai_trader/queue")
        queue_file = queue_dir / "ml_jobs.jsonl"

    if not queue_file.exists():
        return {"queued": [], "running": [], "completed": [], "failed": []}

    status = {"queued": [], "running": [], "completed": [], "failed": []}

    try:
        with queue_file.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    job = json.loads(line)
                    if job.get("job_type") == "bulk_backfill":
                        status[job.get("status", "queued")].append(job)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error("queue_load_failed", error=str(e))

    return status


def get_backfill_stats(symbols: List[str], start_date: str, end_date: str) -> Dict:
    """
    查询 ClickHouse 中的回补数据统计

    Returns:
        {
            "TSLA": {"bars": 123456, "earliest": "2023-11-01", "latest": "2025-10-31"},
            ...
        }
    """
    ch = ClickHouseDB()
    stats = {}

    query = f"""
    SELECT
        symbol,
        count() AS bars,
        min(ts_start) AS earliest,
        max(ts_start) AS latest
    FROM trading.bars_1m_ibkr
    WHERE symbol IN ({','.join(f"'{s}'" for s in symbols)})
      AND ts_start >= '{start_date}'
      AND ts_start < '{end_date}'
    GROUP BY symbol
    """

    try:
        result = ch.client.query(query)
        for row in result.result_rows:
            symbol, bars, earliest, latest = row
            stats[symbol] = {
                "bars": bars,
                "earliest": str(earliest) if earliest else None,
                "latest": str(latest) if latest else None,
            }
    except Exception as e:
        logger.error("clickhouse_query_failed", error=str(e))

    return stats


def print_status(
    queue_status: Dict,
    ch_stats: Optional[Dict] = None,
    show_jobs: bool = True,
):
    """打印格式化的监控状态"""
    print("\n" + "=" * 80)
    print(f"  回补作业监控 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    # 队列统计
    print("\n📊 作业队列:")
    print(f"   ✅ 已完成: {len(queue_status['completed']):>3}")
    print(f"   🔄 运行中: {len(queue_status['running']):>3}")
    print(f"   ⏳ 排队中: {len(queue_status['queued']):>3}")
    print(f"   ❌ 失败:   {len(queue_status['failed']):>3}")

    # 显示运行中的作业
    if show_jobs and queue_status["running"]:
        print("\n🔄 运行中作业:")
        for job in queue_status["running"]:
            job_id = job.get("job_id", "N/A")
            params = job.get("params", {})
            symbols = params.get("symbols", [])
            start = params.get("start_date", "N/A")
            end = params.get("end_date", "N/A")
            print(f"   - {job_id[:12]}... | {start} ~ {end} | {', '.join(symbols)}")

    # 显示失败的作业（警告）
    if queue_status["failed"]:
        print("\n❌ 失败作业:")
        for job in queue_status["failed"][:5]:  # 最多显示5个
            job_id = job.get("job_id", "N/A")
            params = job.get("params", {})
            start = params.get("start_date", "N/A")
            end = params.get("end_date", "N/A")
            print(f"   - {job_id[:12]}... | {start} ~ {end}")
        if len(queue_status["failed"]) > 5:
            print(f"   ... 还有 {len(queue_status['failed']) - 5} 个失败作业")

    # ClickHouse 数据统计
    if ch_stats:
        print("\n📈 ClickHouse 数据量:")
        for symbol, stats in sorted(ch_stats.items()):
            bars = stats["bars"]
            earliest = stats["earliest"]
            latest = stats["latest"]
            print(f"   {symbol:>6}: {bars:>8,} bars | {earliest} ~ {latest}")

    # 进度条（估算）
    if queue_status["completed"] or queue_status["running"] or queue_status["queued"]:
        total = len(queue_status["completed"]) + len(queue_status["running"]) + len(queue_status["queued"]) + len(
            queue_status["failed"]
        )
        completed_count = len(queue_status["completed"])
        progress = completed_count / total * 100 if total > 0 else 0
        bar_length = 40
        filled = int(bar_length * progress / 100)
        bar = "█" * filled + "░" * (bar_length - filled)
        print(f"\n📊 总体进度: [{bar}] {progress:.1f}% ({completed_count}/{total})")

    print("\n" + "=" * 80)


def monitor_loop(interval: int = 30, symbols: Optional[List[str]] = None):
    """持续监控循环"""
    print("🔄 开始监控回补进度...")
    print(f"⏱️  刷新间隔: {interval} 秒")
    print("🛑 按 Ctrl+C 退出")

    try:
        while True:
            # 加载队列状态
            queue_status = load_queue_status()

            # 如果有运行中或队列中的作业，查询 ClickHouse 统计
            ch_stats = None
            if symbols and (queue_status["running"] or queue_status["queued"] or queue_status["completed"]):
                # 从作业中推断日期范围
                all_jobs = queue_status["completed"] + queue_status["running"] + queue_status["queued"]
                if all_jobs:
                    start_dates = [job.get("params", {}).get("start_date") for job in all_jobs if
                                   job.get("params", {}).get("start_date")]
                    end_dates = [job.get("params", {}).get("end_date") for job in all_jobs if
                                 job.get("params", {}).get("end_date")]
                    if start_dates and end_dates:
                        start_date = min(start_dates)
                        end_date = max(end_dates)
                        ch_stats = get_backfill_stats(symbols, start_date, end_date)

            # 打印状态
            print_status(queue_status, ch_stats)

            # 检查是否完成
            if (
                not queue_status["running"]
                and not queue_status["queued"]
                and queue_status["completed"]
            ):
                print("\n✅ 所有回补作业已完成！")
                break

            # 等待下次刷新
            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n\n⚠️  监控已停止")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description="回补进度监控脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="刷新间隔（秒，默认: 30）",
    )

    parser.add_argument(
        "--symbols",
        nargs="+",
        help="监控的股票列表（用于 ClickHouse 查询）",
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="仅显示一次状态（不循环）",
    )

    args = parser.parse_args()

    # 仅显示一次
    if args.once:
        queue_status = load_queue_status()
        ch_stats = None
        if args.symbols:
            # 尝试从队列中推断日期范围
            all_jobs = queue_status["completed"] + queue_status["running"] + queue_status["queued"]
            if all_jobs:
                start_dates = [job.get("params", {}).get("start_date") for job in all_jobs if
                               job.get("params", {}).get("start_date")]
                end_dates = [job.get("params", {}).get("end_date") for job in all_jobs if
                             job.get("params", {}).get("end_date")]
                if start_dates and end_dates:
                    start_date = min(start_dates)
                    end_date = max(end_dates)
                    ch_stats = get_backfill_stats(args.symbols, start_date, end_date)
        print_status(queue_status, ch_stats)
        sys.exit(0)

    # 持续监控
    try:
        monitor_loop(interval=args.interval, symbols=args.symbols)
    except Exception as e:
        print(f"\n❌ 监控失败: {e}", file=sys.stderr)
        logger.error("monitor_error", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
