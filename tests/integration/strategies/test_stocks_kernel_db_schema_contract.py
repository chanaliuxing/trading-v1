import pytest


@pytest.mark.asyncio
async def test_stocks_kernel_lifecycle_reconcile_uses_real_order_schema() -> None:
    from libs.db.postgres import PostgresDB

    pg = PostgresDB(min_conn=1, max_conn=1)
    try:
        await pg.connect()
    except Exception as exc:
        pytest.skip(f"postgres unavailable: {exc}")
    try:
        rows = await pg.fetchall(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'core'
              AND table_name IN ('core_order', 'core_order_event')
              AND column_name IN ('decision_id', 'note', 'order_ref', 'broker_order_id', 'ib_perm_id')
            """
        )
        columns = {(str(row["table_name"]), str(row["column_name"])) for row in rows}
        assert ("core_order", "decision_id") not in columns
        assert ("core_order", "note") in columns
        assert ("core_order", "order_ref") in columns
        assert ("core_order_event", "decision_id") in columns
    finally:
        await pg.close()
