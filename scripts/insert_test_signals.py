#!/usr/bin/env python3
"""
Insert test signal data for HedgingService testing.

Creates a simulated portfolio with:
- 3 strategies with long positions (AAPL, TSLA, NVDA)
- Total net delta ~$45,000 (below max_net_delta threshold)
- No existing hedge positions
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")


def insert_test_signals():
    """Insert test signal data."""
    ch = ClickHouseDB()

    now = datetime.now(ET)

    # Test portfolio:
    # Strategy A: Long AAPL, $20k position
    # Strategy B: Long TSLA, $15k position
    # Strategy C: Long NVDA, $10k position
    # Net delta: $45k (90% of max_net_delta=50k)
    # Should trigger hedge ratio ~0.9 * market_mult

    test_signals = [
        {
            "ts": now.isoformat(),
            "strategy_id": "test_strategy_a",
            "symbol": "AAPL",
            "signal_type": "LONG",
            "quantity": 100.0,
            "entry_price": 180.0,
            "current_price": 200.0,
            "pnl_pos_5s": 20000.0,  # 100 shares * $200
            "pnl_realized": 0.0,
            "pnl_unrealized": 2000.0,  # (200-180) * 100
        },
        {
            "ts": now.isoformat(),
            "strategy_id": "test_strategy_b",
            "symbol": "TSLA",
            "signal_type": "LONG",
            "quantity": 50.0,
            "entry_price": 250.0,
            "current_price": 300.0,
            "pnl_pos_5s": 15000.0,  # 50 shares * $300
            "pnl_realized": 0.0,
            "pnl_unrealized": 2500.0,  # (300-250) * 50
        },
        {
            "ts": now.isoformat(),
            "strategy_id": "test_strategy_c",
            "symbol": "NVDA",
            "signal_type": "LONG",
            "quantity": 80.0,
            "entry_price": 110.0,
            "current_price": 125.0,
            "pnl_pos_5s": 10000.0,  # 80 shares * $125
            "pnl_realized": 0.0,
            "pnl_unrealized": 1200.0,  # (125-110) * 80
        },
    ]

    # Build INSERT statement
    values = []
    for sig in test_signals:
        values.append(
            f"('{sig['ts']}', '{sig['strategy_id']}', '{sig['symbol']}', "
            f"'{sig['signal_type']}', {sig['quantity']}, {sig['entry_price']}, "
            f"{sig['current_price']}, {sig['pnl_pos_5s']}, {sig['pnl_realized']}, "
            f"{sig['pnl_unrealized']})"
        )

    sql = f"""
    INSERT INTO trading.signals_realtime
    (ts, strategy_id, symbol, signal_type, quantity, entry_price,
     current_price, pnl_pos_5s, pnl_realized, pnl_unrealized)
    VALUES {', '.join(values)}
    """

    try:
        ch.execute(sql)
        logger.info("✅ Inserted test signals", count=len(test_signals))

        # Verify
        verify_sql = """
        SELECT
            strategy_id,
            symbol,
            signal_type,
            pnl_pos_5s,
            pnl_unrealized
        FROM trading.signals_realtime
        WHERE ts >= now() - INTERVAL 5 MINUTE
        ORDER BY strategy_id, symbol
        """
        rows = ch.query(verify_sql)

        logger.info(f"\n{'='*60}")
        logger.info("Test Portfolio Summary")
        logger.info(f"{'='*60}")

        total_pos = 0.0
        total_pnl = 0.0
        for row in rows:
            pos = float(row["pnl_pos_5s"])
            pnl = float(row["pnl_unrealized"])
            total_pos += pos
            total_pnl += pnl
            logger.info(
                f"{row['strategy_id']:20} | {row['symbol']:6} | "
                f"{row['signal_type']:5} | ${pos:>10,.0f} | PnL: ${pnl:>8,.0f}"
            )

        logger.info(f"{'='*60}")
        logger.info(f"{'Total Position (Net Delta)':38} | ${total_pos:>10,.0f}")
        logger.info(f"{'Total Unrealized PnL':38} | ${total_pnl:>10,.0f}")
        logger.info(f"{'='*60}")
        logger.info(
            f"\nExposure Ratio: {total_pos / 50000:.1%} "
            f"(of max_net_delta=$50,000)"
        )

        return True

    except Exception as e:
        logger.error("Failed to insert test signals", error=str(e), exc_info=True)
        return False


def cleanup_test_signals():
    """Remove test signals."""
    ch = ClickHouseDB()

    sql = """
    DELETE FROM trading.signals_realtime
    WHERE strategy_id LIKE 'test_strategy_%'
    """

    try:
        ch.execute(sql)
        logger.info("✅ Cleaned up test signals")
        return True
    except Exception as e:
        logger.error("Failed to cleanup test signals", error=str(e), exc_info=True)
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Manage test signals for hedging")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove test signals instead of inserting",
    )
    args = parser.parse_args()

    if args.cleanup:
        logger.info("Cleaning up test signals...")
        return 0 if cleanup_test_signals() else 1
    else:
        logger.info("Inserting test signals...")
        return 0 if insert_test_signals() else 1


if __name__ == "__main__":
    sys.exit(main())
