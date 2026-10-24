#!/usr/bin/env python3
"""
每日更新 20 日分钟均值维表（收盘后运行）

功能:
- 计算每个 (symbol, mod) 最近 20 个交易日的分钟均值和标准差
- 使用 etl_ver = YYYYMMDD 版本控制
- ReplacingMergeTree 自动保留最新版本

调度方式:
    crontab（每日 4:10 PM ET）
    10 16 * * 1-5 cd . && python scripts/maintenance/update_minute_volume_tod_20d.py
"""
import asyncio
from datetime import datetime, timedelta
from libs.db import ch_pool
from libs.logger import get_logger

logger = get_logger(__name__)


async def update_tod_20d():
    """
    计算并更新 20 日分钟均值
    """
    ch = ch_pool.get_client()

    # 生成 etl_ver (YYYYMMDD)
    etl_ver = int(datetime.now().strftime('%Y%m%d'))

    # 计算 20 日分钟均值的 SQL
    # 思路:
    # 1. 从 minute_volume_intraday 获取最近 30 个交易日的数据（容错）
    # 2. 按 (symbol, mod) 分组，计算最近 20 天的均值和标准差
    # 3. 插入到 minute_volume_tod_20d（ReplacingMergeTree 会自动去重）

    update_sql = f"""
    INSERT INTO trading.minute_volume_tod_20d
    (symbol, mod, avg_vol_tod, std_vol_tod, count_days, last_updated, etl_ver)
    WITH
    -- 最近 30 个交易日的分钟级数据
    recent_minutes AS (
        SELECT
            symbol,
            mod,
            ts_min,
            trade_date,
            vol_min
        FROM trading.minute_volume_intraday
        WHERE trade_date >= today() - INTERVAL 30 DAY
          AND trade_date < today()  -- 不包含今天（盘中数据不完整）
          AND mod BETWEEN 570 AND 959  -- RTH only
    ),
    -- 每个 symbol 的最近 20 个交易日
    symbol_dates AS (
        SELECT
            symbol,
            trade_date,
            row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
        FROM (SELECT DISTINCT symbol, trade_date FROM recent_minutes)
        WHERE rn <= 20
    ),
    -- 过滤出最近 20 天的数据
    last_20d AS (
        SELECT
            rm.symbol,
            rm.mod,
            rm.vol_min
        FROM recent_minutes rm
        INNER JOIN symbol_dates sd
          ON rm.symbol = sd.symbol
         AND rm.trade_date = sd.trade_date
    )
    -- 计算均值和标准差
    SELECT
        symbol,
        mod,
        avg(vol_min) AS avg_vol_tod,
        stddevPop(vol_min) AS std_vol_tod,
        count(DISTINCT CAST(vol_min AS UInt64)) AS count_days,  -- 近似天数
        now() AS last_updated,
        {etl_ver} AS etl_ver
    FROM last_20d
    GROUP BY symbol, mod
    HAVING count_days >= 5  -- 至少 5 天数据才计算
    """

    try:
        start_time = datetime.now()

        logger.info("Starting 20-day TOD update", etl_ver=etl_ver)

        ch.command(update_sql)

        duration = (datetime.now() - start_time).total_seconds()

        # 检查结果
        count_query = f"""
        SELECT count() AS cnt
        FROM trading.minute_volume_tod_20d
        WHERE etl_ver = {etl_ver}
        """
        result = ch.query(count_query)
        row_count = result.result_rows[0][0]

        logger.info("20-day TOD update completed",
                    rows_inserted=row_count,
                    duration_sec=duration,
                    etl_ver=etl_ver)

        # 记录到维护日志
        log_sql = f"""
        INSERT INTO trading.minute_volume_maintenance_log
        (task_type, execution_time, rows_processed, duration_ms, status, etl_ver)
        VALUES
        ('daily_tod_update', now(), {row_count}, {int(duration * 1000)}, 'success', {etl_ver})
        """
        ch.command(log_sql)

        # 清理旧版本（保留最近 7 天的版本）
        cleanup_cutoff = int((datetime.now() - timedelta(days=7)).strftime('%Y%m%d'))
        cleanup_sql = f"""
        ALTER TABLE trading.minute_volume_tod_20d
        DELETE WHERE etl_ver < {cleanup_cutoff}
        """
        ch.command(cleanup_sql)
        logger.info("Cleaned up old versions", cutoff_ver=cleanup_cutoff)

        return row_count

    except Exception as e:
        logger.error("20-day TOD update failed", error=str(e), exc_info=True)
        # 记录失败
        error_msg = str(e).replace("'", "\\'")
        log_sql = f"""
        INSERT INTO trading.minute_volume_maintenance_log
        (task_type, execution_time, rows_processed, duration_ms, status, error_msg, etl_ver)
        VALUES
        ('daily_tod_update', now(), 0, 0, 'error', '{error_msg}', {etl_ver})
        """
        try:
            ch.command(log_sql)
        except:
            pass
        raise

    finally:
        ch.close()


if __name__ == '__main__':
    asyncio.run(update_tod_20d())
