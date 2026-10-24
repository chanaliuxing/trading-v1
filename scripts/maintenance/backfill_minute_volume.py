#!/usr/bin/env python3
"""
回填分钟级体量到 minute_volume_intraday 表
用于初始化或重建历史数据

使用方法:
    python scripts/maintenance/backfill_minute_volume.py --start-date 2025-10-01 --end-date 2025-10-28
    python scripts/maintenance/backfill_minute_volume.py --days 30  # 最近 30 天
"""
import asyncio
from datetime import datetime, timedelta
import argparse
from typing import List

from libs.db import ch_pool
from libs.logger import get_logger

logger = get_logger(__name__)


async def backfill_minute_volume(start_date: str, end_date: str, symbols: List[str] = None):
    """
    从 bars_5s_current 回填分钟级体量到 minute_volume_intraday

    Args:
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        symbols: 指定 symbol 列表；若为 None 则处理所有
    """
    ch = ch_pool.get_client()

    # 构建 symbol 过滤条件
    symbol_filter = ""
    if symbols:
        symbol_list = "', '".join(symbols)
        symbol_filter = f"AND symbol IN ('{symbol_list}')"

    # 回填 SQL（从 bars_5s_current 聚合到分钟级）
    backfill_sql = f"""
    INSERT INTO trading.minute_volume_intraday
    (symbol, ts_min, mod, vol_min, trade_date)
    SELECT
        symbol,
        toStartOfMinute(ts_start) AS ts_min,
        toHour(toTimeZone(ts_start, 'America/New_York')) * 60
          + toMinute(toTimeZone(ts_start, 'America/New_York')) AS mod,
        sum(volume) AS vol_min,
        toDate(toTimeZone(ts_start, 'America/New_York')) AS trade_date
    FROM trading.bars_5s_current
    WHERE toDate(ts_start) >= '{start_date}'
      AND toDate(ts_start) <= '{end_date}'
      {symbol_filter}
    GROUP BY symbol, ts_min, mod, trade_date
    """

    try:
        logger.info("Starting backfill", start_date=start_date, end_date=end_date, symbols=symbols)
        start_time = datetime.now()

        ch.command(backfill_sql)

        duration = (datetime.now() - start_time).total_seconds()

        # 检查插入结果
        count_query = f"""
        SELECT count() AS cnt
        FROM trading.minute_volume_intraday
        WHERE trade_date >= '{start_date}'
          AND trade_date <= '{end_date}'
          {symbol_filter}
        """
        result = ch.query(count_query)
        row_count = result.result_rows[0][0]

        logger.info("Backfill completed",
                    rows_inserted=row_count,
                    duration_sec=duration,
                    start_date=start_date,
                    end_date=end_date)

        # 记录到维护日志
        log_sql = f"""
        INSERT INTO trading.minute_volume_maintenance_log
        (task_type, execution_time, rows_processed, duration_ms, status)
        VALUES
        ('backfill', now(), {row_count}, {int(duration * 1000)}, 'success')
        """
        ch.command(log_sql)

        return row_count

    except Exception as e:
        logger.error("Backfill failed", error=str(e), exc_info=True)
        # 记录失败
        error_msg = str(e).replace("'", "\\'")
        log_sql = f"""
        INSERT INTO trading.minute_volume_maintenance_log
        (task_type, execution_time, rows_processed, duration_ms, status, error_msg)
        VALUES
        ('backfill', now(), 0, 0, 'error', '{error_msg}')
        """
        try:
            ch.command(log_sql)
        except:
            pass
        raise

    finally:
        ch.close()


def main():
    parser = argparse.ArgumentParser(description='Backfill minute volume data')
    parser.add_argument('--start-date', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--days', type=int, help='Backfill last N days (alternative to start/end)')
    parser.add_argument('--symbols', type=str, nargs='+', help='Specific symbols (optional)')

    args = parser.parse_args()

    if args.days:
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=args.days)).strftime('%Y-%m-%d')
    elif args.start_date and args.end_date:
        start_date = args.start_date
        end_date = args.end_date
    else:
        parser.error("Must provide either --days or both --start-date and --end-date")

    asyncio.run(backfill_minute_volume(start_date, end_date, args.symbols))


if __name__ == '__main__':
    main()
