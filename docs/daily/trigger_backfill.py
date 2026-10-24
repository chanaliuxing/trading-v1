#!/usr/bin/env python3
"""
手动触发 Backfill 脚本

用法:
    # 回补昨天的数据
    python trigger_backfill.py

    # 回补指定日期
    python trigger_backfill.py --date 2025-10-26

    # 回补指定股票
    python trigger_backfill.py --symbols AAPL TSLA

    # 回补特定 bar size
    python trigger_backfill.py --bar-size 5m
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from typing import List
import argparse

from aiokafka import AIOKafkaProducer
import sys
import os

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from libs.config import config as _cfg


def _topic_for_bar_size(bar_size: str) -> str:
    bs = bar_size.strip().lower()
    # Map to configured topics (see config.yml backfill.instances)
    mapping = {
        "1m": "md.backfill.request.1m.v1",
        "5m": "md.backfill.request.5m.v1",
        "10m": "md.backfill.request.10m.v1",
        "15m": "md.backfill.request.15m.v1",
        "30m": "md.backfill.request.30m.v1",
        "1h": "md.backfill.request.1h.v1",
        "2h": "md.backfill.request.2h.v1",
        "3h": "md.backfill.request.3h.v1",
        "4h": "md.backfill.request.4h.v1",
        "1d": "md.backfill.request.1d.v1",
        # 5s instance uses generic topic in current config
        "5s": "md.backfill.request.v1",
    }
    return mapping.get(bs, "md.backfill.request.1m.v1")


async def trigger_backfill(
    symbols: List[str],
    start_date: str,
    end_date: str,
    bar_size: str = "1m"
):
    """
    向 Kafka 发送 Backfill 请求

    Args:
        symbols: 股票列表
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        bar_size: Bar 大小 (1m, 5m, 15m, etc.)
    """
    bootstrap = ','.join(_cfg.kafka_config.get('bootstrap_servers', ['localhost:9092']))
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap,
        value_serializer=lambda v: json.dumps(v).encode('utf-8'),
        key_serializer=lambda k: k.encode('utf-8') if isinstance(k, str) else k
    )

    await producer.start()

    try:
        print(f"\n🔄 触发 Backfill 请求")
        print(f"  📅 日期范围: {start_date} 至 {end_date}")
        print(f"  📊 Bar 大小: {bar_size}")
        print(f"  🎯 股票列表: {', '.join(symbols)}")
        print()

        topic = _topic_for_bar_size(bar_size)
        for symbol in symbols:
            payload = {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "bar_size": bar_size,
                "source": "manual_trigger",
                "timestamp": datetime.now().isoformat()
            }

            await producer.send(topic, key=symbol, value=payload)

            print(f"  ✅ {symbol}: Backfill 请求已发送")

        print(f"\n✅ 所有请求已发送到 Kafka topic: {topic}")
        print(f"💡 BackfillService 将在后台处理这些请求")
        print(f"📊 查看进度: clickhouse-client --query \"SELECT * FROM trading.backfill_tasks ORDER BY created_at DESC LIMIT 10 FORMAT Pretty\"")

    except Exception as e:
        print(f"\n❌ 发送 Backfill 请求失败: {e}", file=sys.stderr)
        raise
    finally:
        await producer.stop()


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="手动触发 Backfill 数据回补",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 回补昨天的数据（默认）
  python trigger_backfill.py

  # 回补指定日期
  python trigger_backfill.py --date 2025-10-26

  # 回补日期范围
  python trigger_backfill.py --start-date 2025-10-20 --end-date 2025-10-26

  # 回补指定股票
  python trigger_backfill.py --symbols AAPL TSLA NVDA

  # 回补 5 分钟 Bar
  python trigger_backfill.py --bar-size 5m

  # 组合使用
  python trigger_backfill.py --date 2025-10-26 --symbols AAPL --bar-size 1m
        """
    )

    parser.add_argument(
        '--date',
        type=str,
        help='回补单个日期 (YYYY-MM-DD)，默认为昨天'
    )

    parser.add_argument(
        '--start-date',
        type=str,
        help='开始日期 (YYYY-MM-DD)'
    )

    parser.add_argument(
        '--end-date',
        type=str,
        help='结束日期 (YYYY-MM-DD)'
    )

    parser.add_argument(
        '--symbols',
        nargs='+',
        default=['AAPL', 'TSLA', 'NVDA', 'GOOGL', 'MSFT', 'META', 'AMZN'],
        help='股票列表（空格分隔），默认为常用股票'
    )

    parser.add_argument(
        '--bar-size',
        type=str,
        default='1m',
        choices=['1m', '5m', '15m', '30m', '1h', '1d'],
        help='Bar 大小，默认为 1m'
    )

    return parser.parse_args()


def main():
    """主函数"""
    args = parse_args()

    # 确定日期范围
    if args.date:
        start_date = args.date
        end_date = args.date
    elif args.start_date and args.end_date:
        start_date = args.start_date
        end_date = args.end_date
    elif args.start_date:
        start_date = args.start_date
        end_date = datetime.now().strftime('%Y-%m-%d')
    else:
        # 默认：昨天
        yesterday = datetime.now() - timedelta(days=1)
        start_date = yesterday.strftime('%Y-%m-%d')
        end_date = start_date

    # 验证日期格式
    try:
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError:
        print("❌ 日期格式错误，请使用 YYYY-MM-DD 格式", file=sys.stderr)
        sys.exit(1)

    # 运行 backfill
    asyncio.run(trigger_backfill(
        symbols=args.symbols,
        start_date=start_date,
        end_date=end_date,
        bar_size=args.bar_size
    ))


if __name__ == "__main__":
    main()
