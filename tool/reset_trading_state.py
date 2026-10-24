import asyncio
import os
from pathlib import Path
from libs.db.postgres import PostgresDB
from libs.logger import get_logger

logger = get_logger("reset_tool")

async def reset_system_state():
    # 1. 清理数据库
    pg = PostgresDB()
    await pg.connect()
    
    tables = [
        "core.core_order",
        "core.core_fill",
        "core.core_position",
        "core.core_order_reservation",
        "core.core_strategy_ledger"
    ]
    
    try:
        logger.info("正在清理数据库表...")
        for table in tables:
            # 使用 CASCADE 确保外键关联表也一并清理
            await pg.execute(f"TRUNCATE {table} CASCADE")
            logger.info(f"   ✅ 已清空: {table}")
        
        # 2. 清理策略本地状态文件
        # 根据代码逻辑，路径通常在 runtime/state/
        state_files = [
            "runtime/state/qqq_intraday_qqq_intraday_001.json",
            "runtime/state/qqq_intraday_qqq_intraday_001.json.tmp"
        ]
        
        logger.info("正在清理本地状态文件...")
        for f_path in state_files:
            p = Path(f_path)
            if p.exists():
                p.unlink()
                logger.info(f"   ✅ 已删除: {f_path}")
            else:
                logger.info(f"   ℹ️  文件不存在 (无需删除): {f_path}")

        logger.info("=" * 50)
        logger.info("✨ 系统状态重置成功！")
        logger.info("现在您可以安全地重启 run_system.py 了。")
        logger.info("=" * 50)

    except Exception as e:
        logger.error(f"❌ 重置过程中发生错误: {e}")
    finally:
        await pg.close()

if __name__ == "__main__":
    asyncio.run(reset_system_state())
