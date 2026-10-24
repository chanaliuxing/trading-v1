import asyncio
import json
import os
from datetime import datetime
from libs.db.postgres import PostgresDB
from services.exec.adapters.ib_adapter import IBInsyncAdapter
from libs.config import config
from libs.logger import get_logger

# 配置日志
logger = get_logger("cleanup_tool")

async def cleanup_order_cache():
    pg = PostgresDB()
    await pg.connect()
    
    # 1. 初始化 IB 适配器
    ib_cfg = config.ib_config
    adapter = IBInsyncAdapter(
        host=ib_cfg['host'],
        port=ib_cfg['port'],
        client_id=999  # 使用专用清理 ID
    )
    
    try:
        await adapter.start()
        logger.info("已连接至 IBKR")

        # 2. 获取 IBKR 端的活跃订单
        ib_open_orders = await adapter.open_orders()
        ib_open_ids = {str(o.order_id) for o in ib_open_orders}
        logger.info(f"IBKR 当前活跃订单 ID: {ib_open_ids}")

        # 3. 查询数据库中所有“挂起”的订单
        db_pending_orders = await pg.fetchall(
            "SELECT order_id, broker_order_id, symbol, side, qty, state FROM core.core_order WHERE state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')"
        )
        
        logger.info(f"数据库中挂起订单总数: {len(db_pending_orders)}")

        cleaned_count = 0
        for order in db_pending_orders:
            b_id = order.get('broker_order_id')
            
            # 如果数据库有 broker_id，但 IBKR 活跃列表中没有，说明已经平掉或撤单了
            if b_id and b_id not in ib_open_ids:
                logger.warning(f"发现失效单: DB_ID={order['order_id']} Broker_ID={b_id} ({order['symbol']} {order['side']})")
                
                note = json.dumps({"cleanup_reason": "not_in_ib_open_orders", "ts": datetime.now().isoformat()})
                
                # 更新数据库状态
                await pg.execute(
                    "UPDATE core.core_order SET state = 'CANCELLED', note = $1, updated_at = now() WHERE order_id = $2",
                    note, order['order_id']
                )
                cleaned_count += 1
            
            # 如果数据库里连 broker_id 都没有（下单失败的残余），也一并清理
            elif not b_id:
                logger.warning(f"清理无 ID 残余单: DB_ID={order['order_id']}")
                note = json.dumps({"cleanup_reason": "no_broker_id_cleanup", "ts": datetime.now().isoformat()})
                await pg.execute(
                    "UPDATE core.core_order SET state = 'CANCELLED', note = $1, updated_at = now() WHERE order_id = $2",
                    note, order['order_id']
                )
                cleaned_count += 1

        logger.info(f"清理完成，共处理 {cleaned_count} 个失效订单记录。")

    except Exception as e:
        logger.error(f"清理过程中发生错误: {e}")
    finally:
        await adapter.stop()
        await pg.close()

if __name__ == "__main__":
    asyncio.run(cleanup_order_cache())
