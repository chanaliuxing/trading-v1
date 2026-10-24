#!/usr/bin/env python3
"""
Backfill today's 1m and 5m bars for all symbols in universe
This will populate the bars_1m_ibkr and bars_5m_ibkr tables via backfill service
"""
import sys
import asyncio
from pathlib import Path
from datetime import datetime, time
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.config import config
from libs.logger import get_logger
from aiokafka import AIOKafkaProducer
import json

logger = get_logger(__name__)

async def send_backfill_request(producer, topic, symbol, bar_size, from_ts, to_ts):
    """Send backfill request to Kafka"""
    request = {
        "symbol": symbol,
        "bar_size": bar_size,
        "from_ts": int(from_ts.timestamp()),
        "to_ts": int(to_ts.timestamp()),
        "use_rth": True,
        "what_to_show": "TRADES"
    }

    message = json.dumps(request).encode('utf-8')
    await producer.send(topic, value=message, key=symbol.encode('utf-8'))
    logger.info(f"Sent backfill request", symbol=symbol, bar_size=bar_size, topic=topic)

async def main():
    """Backfill today's bars for all symbols"""

    # Get symbols from config
    symbols = config.universe if hasattr(config, 'universe') and config.universe else ['TSLA', 'SPY', 'QQQ', 'VIX']

    # Get today's trading session times (9:30 AM - current time ET)
    now = datetime.now()
    today_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    today_end = now

    logger.info("Starting backfill for today's session",
                symbols=symbols,
                from_time=today_start,
                to_time=today_end)

    # Initialize Kafka producer
    kafka_bootstrap = config.kafka_config.get('bootstrap_servers', 'localhost:9092')
    producer = AIOKafkaProducer(
        bootstrap_servers=kafka_bootstrap,
        value_serializer=None,  # We'll encode manually
        key_serializer=None
    )

    await producer.start()

    try:
        # Send 1m backfill requests
        logger.info("Requesting 1m bars...")
        for symbol in symbols:
            await send_backfill_request(
                producer,
                topic="md.backfill.request.1m.v1",
                symbol=symbol,
                bar_size="1 min",
                from_ts=today_start,
                to_ts=today_end
            )
            await asyncio.sleep(0.1)  # Small delay to avoid overwhelming the service

        # Send 5m backfill requests
        logger.info("Requesting 5m bars...")
        for symbol in symbols:
            await send_backfill_request(
                producer,
                topic="md.backfill.request.5m.v1",
                symbol=symbol,
                bar_size="5 mins",
                from_ts=today_start,
                to_ts=today_end
            )
            await asyncio.sleep(0.1)

        # Flush to ensure all messages are sent
        await producer.flush()

        logger.info(f"✅ Backfill requests sent for {len(symbols)} symbols (1m + 5m)")
        logger.info("Backfill service will process these requests and populate bars_1m_ibkr and bars_5m_ibkr")
        logger.info("Check progress: clickhouse-client --query \"SELECT symbol, COUNT(*) FROM trading.bars_1m_ibkr WHERE date(ts_start)=today() GROUP BY symbol\"")

    finally:
        await producer.stop()

if __name__ == "__main__":
    asyncio.run(main())
