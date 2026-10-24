#!/usr/bin/env python3
"""
手动触发 1day K线回补脚本

用法:
    # 回补最近5天的1day数据
    python scripts/trigger_1day_backfill.py

    # 回补指定天数
    python scripts/trigger_1day_backfill.py --days 10

    # 回补指定日期
    python scripts/trigger_1day_backfill.py --date 2025-10-30
"""

import asyncio
import sys
from datetime import datetime, date
from pathlib import Path
import argparse

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.market.daily_eod_backfill_service import (
    DailyEODBackfillService,
    DailyEODBackfillConfig,
)
from libs.logger import get_logger
from zoneinfo import ZoneInfo

logger = get_logger(__name__)
NY_TZ = ZoneInfo("America/New_York")


async def manual_backfill(target_date: date = None, lookback_days: int = None):
    """手动触发1day回补"""

    # 创建配置
    cfg = DailyEODBackfillConfig(
        enabled=True,
        lookback_days=lookback_days or 5,  # 默认回补5天
        request_topic="md.backfill.request.1d.v1",
        bar_size="1 day",
        use_rth=True,
    )

    service = DailyEODBackfillService(cfg)

    # 启动服务（仅初始化Kafka producer）
    await service.start()

    try:
        if target_date:
            # 回补指定日期
            logger.info(f"🎯 手动触发单日回补", date=target_date.isoformat())
            await service.trigger_manual_backfill(target_date)
        else:
            # 回补最近N天
            logger.info(f"🎯 手动触发批量回补", lookback_days=cfg.lookback_days)
            await service._trigger_daily_backfill()

        logger.info("✅ 回补请求已发送到Kafka")
        logger.info("💡 查看消费进度: docker logs -f backfill_consumer_1d")
        logger.info(
            "💡 查看ClickHouse数据: clickhouse-client --query \"SELECT symbol, ts, open, close FROM lake.bars_1d ORDER BY ts DESC LIMIT 10 FORMAT Pretty\""
        )

    finally:
        await service.stop()


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="手动触发 1day K线回补",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 回补最近5天（默认）
  python scripts/trigger_1day_backfill.py

  # 回补最近10天
  python scripts/trigger_1day_backfill.py --days 10

  # 回补指定日期
  python scripts/trigger_1day_backfill.py --date 2025-10-30

  # 回补指定日期范围（通过days参数从date往前推）
  python scripts/trigger_1day_backfill.py --date 2025-10-30 --days 5
        """,
    )

    parser.add_argument("--date", type=str, help="目标日期 (YYYY-MM-DD)，默认为今天")

    parser.add_argument("--days", type=int, default=5, help="回补天数，默认5天")

    return parser.parse_args()


def main():
    """主函数"""
    args = parse_args()

    target_date = None
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print("❌ 日期格式错误，请使用 YYYY-MM-DD 格式", file=sys.stderr)
            sys.exit(1)

    asyncio.run(manual_backfill(target_date=target_date, lookback_days=args.days))


if __name__ == "__main__":
    main()
