#!/usr/bin/env python3
"""
Mock Market Data Injector - 模拟真实IBKR市场数据流

不需要真实IBKR连接，直接向Kafka注入模拟bar数据
用于测试整个交易系统pipeline: Kafka → ClickHouse → Features → Strategy

Usage:
    python3 mock_data_injector.py --symbol TSLA --bars 100
"""

import asyncio
import json
import time
import random
from datetime import datetime, timedelta
from aiokafka import AIOKafkaProducer
import argparse


class MockDataGenerator:
    """生成逼真的市场数据"""

    def __init__(self, symbol: str, base_price: float = 250.0):
        self.symbol = symbol
        self.price = base_price
        self.volume_base = 1000

    def generate_bar(self, ts_ms: int) -> dict:
        """生成一个5秒bar，模拟真实市场波动"""
        # 随机游走价格
        change_pct = random.gauss(0, 0.0005)  # 0.05%标准差
        self.price *= (1 + change_pct)

        # 生成OHLCV
        open_price = self.price
        high_price = open_price * (1 + abs(random.gauss(0, 0.0003)))
        low_price = open_price * (1 - abs(random.gauss(0, 0.0003)))
        close_price = random.uniform(low_price, high_price)
        volume = int(self.volume_base * random.uniform(0.5, 2.0))

        return {
            "symbol": self.symbol,
            "ts_ms": ts_ms,
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": volume,
            "bar_count": random.randint(50, 150),
            "vwap": round((high_price + low_price + close_price) / 3, 2),
            "source": "mock_injector"
        }


async def inject_mock_data(
    symbol: str,
    num_bars: int = 100,
    kafka_bootstrap: str = "localhost:9092",
    topic: str = "md.bar.5s.v2",
    interval_ms: int = 5000
):
    """
    注入模拟市场数据到Kafka

    Args:
        symbol: 股票代码 (e.g., 'TSLA', 'AAPL')
        num_bars: 要生成的bar数量
        kafka_bootstrap: Kafka地址
        topic: Kafka topic
        interval_ms: Bar间隔 (毫秒)
    """

    print(f"🚀 Mock Data Injector Starting...")
    print(f"   Symbol: {symbol}")
    print(f"   Bars: {num_bars}")
    print(f"   Topic: {topic}")
    print(f"   Interval: {interval_ms}ms")
    print()

    # 创建Kafka producer
    producer = AIOKafkaProducer(
        bootstrap_servers=kafka_bootstrap,
        value_serializer=lambda v: json.dumps(v).encode('utf-8'),
        compression_type='lz4',
        linger_ms=5,
        acks='all',
        request_timeout_ms=10000  # 10s timeout
    )

    await producer.start()
    print(f"✅ Connected to Kafka: {kafka_bootstrap}\n")

    try:
        generator = MockDataGenerator(symbol)

        # 从当前时间开始，向后生成历史数据
        current_ts = int(time.time() * 1000)
        start_ts = current_ts - (num_bars * interval_ms)

        print(f"📊 Injecting {num_bars} bars...")
        print(f"   Time range: {datetime.fromtimestamp(start_ts/1000)} → {datetime.fromtimestamp(current_ts/1000)}")
        print()

        for i in range(num_bars):
            ts_ms = start_ts + (i * interval_ms)
            bar = generator.generate_bar(ts_ms)

            # 发送到Kafka
            await producer.send_and_wait(
                topic,
                key=symbol.encode('utf-8'),
                value=bar
            )

            if (i + 1) % 20 == 0:
                print(f"   Sent {i+1}/{num_bars} bars... (price: ${bar['close']:.2f})")

            # 模拟实时速度 (可选，可以注释掉以快速注入)
            # await asyncio.sleep(0.1)

        print(f"\n✅ Successfully injected {num_bars} bars for {symbol}")
        print(f"   Latest price: ${bar['close']:.2f}")
        print(f"   Latest volume: {bar['volume']}")

    finally:
        await producer.stop()


async def inject_multiple_symbols(symbols: list, bars_per_symbol: int = 100):
    """同时为多个symbol注入数据"""
    tasks = [
        inject_mock_data(symbol, bars_per_symbol)
        for symbol in symbols
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock market data injector")
    parser.add_argument("--symbol", default="TSLA", help="Stock symbol (default: TSLA)")
    parser.add_argument("--bars", type=int, default=100, help="Number of bars to generate (default: 100)")
    parser.add_argument("--kafka", default="localhost:9092", help="Kafka bootstrap server")
    parser.add_argument("--topic", default="md.bar.5s.v2", help="Kafka topic")
    parser.add_argument("--multi", nargs="+", help="Inject multiple symbols (e.g., --multi TSLA AAPL NVDA)")

    args = parser.parse_args()

    if args.multi:
        print(f"🎯 Multi-symbol mode: {', '.join(args.multi)}\n")
        asyncio.run(inject_multiple_symbols(args.multi, args.bars))
    else:
        asyncio.run(inject_mock_data(
            symbol=args.symbol,
            num_bars=args.bars,
            kafka_bootstrap=args.kafka,
            topic=args.topic
        ))
