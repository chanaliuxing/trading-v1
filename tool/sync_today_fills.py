import asyncio
import sys
from datetime import date
from pathlib import Path

from ib_insync import ExecutionFilter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.config import config
from libs.db.postgres import PostgresDB
from libs.logger import get_logger
from services.exec.adapters.ib_adapter import IBInsyncAdapter

logger = get_logger("fill_sync_tool")

async def sync_today_fills() -> None:
    pg = PostgresDB()
    await pg.connect()

    ib_cfg = config.ib_config
    adapter = IBInsyncAdapter(
        host=ib_cfg["host"],
        port=ib_cfg["port"],
        client_id=998  # 专用同步 ID
    )

    try:
        await adapter.start()
        logger.info("已连接至 IBKR，开始获取今日成交...")

        # Ensure synthetic strategy exists in case we need to adopt unknown orders.
        await pg.execute(
            """
            INSERT INTO core.core_strategy (strategy_id, name, capital_limit_usd, reserve_buffer_bps, is_enabled, created_at)
            VALUES ('adopted_reconciler', 'Adopted by Fill Sync', 1000000, 10, true, now())
            ON CONFLICT (strategy_id) DO NOTHING
            """
        )

        ib_fills = await adapter.ib.reqExecutionsAsync(ExecutionFilter())
        logger.info(f"IBKR 返回 Execution 数: {len(ib_fills)}")

        synced_count = 0
        skipped_count = 0
        for fill in ib_fills:
            ex = fill.execution
            exec_id = str(getattr(ex, "execId", "") or "").strip()
            if not exec_id:
                skipped_count += 1
                continue

            ts = getattr(ex, "time", None)
            if ts is None or ts.date() != date.today():
                continue

            # Skip if execution is already persisted.
            existed = await pg.fetchone(
                "SELECT 1 AS ok FROM core.core_fill WHERE exec_id = $1 LIMIT 1",
                exec_id,
            )
            if existed:
                continue

            order_id = int(getattr(ex, "orderId", 0) or 0)
            perm_id = int(getattr(ex, "permId", 0) or 0)
            symbol = str(getattr(fill.contract, "symbol", "") or "UNKNOWN")
            side = str(getattr(ex, "side", "BOT") or "BOT")
            qty = float(getattr(ex, "shares", 0.0) or 0.0)
            price = float(getattr(ex, "price", 0.0) or 0.0)
            exchange = str(getattr(ex, "exchange", "") or "")
            commission = float(getattr(fill.commissionReport, "commission", 0.0) or 0.0)

            if qty <= 0 or price <= 0:
                skipped_count += 1
                continue

            # 1) Resolve order row by broker/perm id; pick newest row if reused.
            order_row = await pg.fetchone(
                """
                SELECT order_id
                FROM core.core_order
                WHERE broker_order_id = $1 OR ib_perm_id = $2
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(order_id),
                perm_id if perm_id > 0 else None,
            )

            if not order_row:
                logger.warning(f"发现失忆订单成交: broker_order_id={order_id}, exec_id={exec_id}, 正在补录订单...")
                order_row = await pg.fetchone(
                    """
                    INSERT INTO core.core_order (
                        order_id, strategy_id, symbol, idempotency_key,
                        broker_order_id, ib_perm_id, side, order_type, qty, state, note, created_at, updated_at
                    ) VALUES (
                        gen_random_uuid(), 'adopted_reconciler', $1, $2,
                        $3, $4, $5, 'MKT', $6, 'FILLED', 'sync_backfill', $7, now()
                    )
                    RETURNING order_id
                    """,
                    symbol,
                    f"sync:{exec_id}",
                    str(order_id) if order_id > 0 else None,
                    perm_id if perm_id > 0 else None,
                    "BUY" if side.upper().startswith("B") else "SELL",
                    qty,
                    ts,
                )

            order_uuid = order_row["order_id"]

            # 2) Insert fill (dedupe by table's unique key + exec_id unique index).
            try:
                await pg.execute(
                    """
                    INSERT INTO core.core_fill (
                        fill_id, order_id, ts, qty, price,
                        liquidity, commission_usd, exchange, exec_id, created_at
                    ) VALUES (
                        gen_random_uuid(), $1, $2, $3, $4,
                        'UNKNOWN', $5, $6, $7, now()
                    )
                    ON CONFLICT (order_id, ts, qty, price) DO NOTHING
                    """,
                    order_uuid,
                    ts,
                    qty,
                    price,
                    commission,
                    exchange,
                    exec_id,
                )
                synced_count += 1
            except Exception as exc:
                logger.error(f"同步 Fill {exec_id} 失败: {exc}")
                continue

            # 3) Best-effort: mark order terminal after sync.
            try:
                await pg.execute(
                    """
                    UPDATE core.core_order
                    SET state = CASE WHEN state IN ('NEW','SUBMITTED','PARTIAL') THEN 'FILLED' ELSE state END,
                        updated_at = now()
                    WHERE order_id = $1
                    """,
                    order_uuid,
                )
            except Exception:
                pass

        logger.info(f"同步完成，新增 fills={synced_count}，跳过异常/无效记录={skipped_count}")

    except Exception as e:
        logger.error(f"同步过程发生错误: {e}")
    finally:
        await adapter.stop()
        await pg.close()

if __name__ == "__main__":
    asyncio.run(sync_today_fills())
