#!/usr/bin/env python3
"""
盘中增量刷新分钟级体量（每 1-2 分钟运行）

功能:
- 从 bars_5s_current 聚合最近 2 小时的分钟级体量
- 使用 INSERT ... SELECT ... SETTINGS optimize_on_insert=1 自动合并

调度方式:
    方式1: crontab（每 2 分钟）
    */2 9-16 * * 1-5 cd . && python scripts/maintenance/refresh_minute_volume_intraday.py

    方式2: 集成到 run_system.py（推荐）
"""
import asyncio
from datetime import datetime
from libs.db import ch_pool
from libs.logger import get_logger

logger = get_logger(__name__)


async def refresh_intraday_minute_volume():
    """
    刷新最近 2 小时的分钟级体量
    SummingMergeTree 会自动合并相同 (symbol, mod, ts_min) 的行
    """
    ch = ch_pool.get_client()

    # 删除最近 2 小时的数据（防止重复累加）
    delete_sql = """
    ALTER TABLE trading.minute_volume_intraday
    DELETE WHERE ts_min >= now() - INTERVAL 2 HOUR
    """

    # 重新插入最近 2 小时的分钟级体量
    refresh_sql = """
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
    WHERE ts_start >= now() - INTERVAL 2 HOUR
      AND toHour(toTimeZone(ts_start, 'America/New_York')) * 60
          + toMinute(toTimeZone(ts_start, 'America/New_York')) BETWEEN 570 AND 959  -- RTH only
    GROUP BY symbol, ts_min, mod, trade_date
    SETTINGS optimize_on_insert=1  -- 自动合并
    """

    try:
        start_time = datetime.now()

        # 执行删除和插入
        ch.command(delete_sql)
        ch.command(refresh_sql)

        duration = (datetime.now() - start_time).total_seconds()

        # 检查结果
        count_query = """
        SELECT count() AS cnt
        FROM trading.minute_volume_intraday
        WHERE ts_min >= now() - INTERVAL 2 HOUR
        """
        result = ch.query(count_query)
        row_count = result.result_rows[0][0]

        logger.info("Intraday refresh completed",
                    rows_processed=row_count,
                    duration_sec=duration)

        # 记录到维护日志
        log_sql = f"""
        INSERT INTO trading.minute_volume_maintenance_log
        (task_type, execution_time, rows_processed, duration_ms, status)
        VALUES
        ('intraday_refresh', now(), {row_count}, {int(duration * 1000)}, 'success')
        """
        ch.command(log_sql)

        return row_count

    except Exception as e:
        logger.error("Intraday refresh failed", error=str(e), exc_info=True)
        # 记录失败
        error_msg = str(e).replace("'", "\\'")
        log_sql = f"""
        INSERT INTO trading.minute_volume_maintenance_log
        (task_type, execution_time, rows_processed, duration_ms, status, error_msg)
        VALUES
        ('intraday_refresh', now(), 0, 0, 'error', '{error_msg}')
        """
        try:
            ch.command(log_sql)
        except:
            pass
        raise

    finally:
        ch.close()


if __name__ == '__main__':
    asyncio.run(refresh_intraday_minute_volume())
