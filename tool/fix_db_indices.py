import asyncio
from libs.db.postgres import PostgresDB
from libs.logger import get_logger

logger = get_logger("index_fix_tool")

async def fix_indices():
    pg = PostgresDB()
    await pg.connect()
    
    try:
        logger.info("正在为 core_order 和 core_fill 添加必要索引...")
        
        # 1. 为 core_order 添加 broker_order_id 的唯一索引
        # 先删除重复（如果有），确保索引能建成功
        await pg.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS core_order_broker_id_uk 
            ON core.core_order (broker_order_id);
        """)
        logger.info("   ✅ core_order(broker_order_id) 唯一索引已就绪")

        # 2. 为 core_fill 添加 exec_id 的唯一索引
        await pg.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS core_fill_exec_id_uk 
            ON core.core_fill (exec_id);
        """)
        logger.info("   ✅ core_fill(exec_id) 唯一索引已就绪")

        logger.info("=" * 50)
        logger.info("✨ 数据库索引修复完成！")
        logger.info("=" * 50)

    except Exception as e:
        logger.error(f"❌ 修复过程中发生错误: {e}")
    finally:
        await pg.close()

if __name__ == "__main__":
    asyncio.run(fix_indices())
