#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IBKR VIX 1m Bar Ingestor → Kafka
--------------------------------
- 连接 IB Gateway (默认端口 4002)
- 抓取 VIX 指数每分钟数据 (whatToShow="TRADES")
- 每 55 秒更新一次，写入 Kafka 主题 md.bar.1m.ibkr
- 确保 downstream ClickHouse (ReplacingMergeTree) 幂等写入
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from aiokafka import AIOKafkaProducer
from ib_insync import IB, Index, util

from libs.config import config
from libs.logger import get_logger
from services.ibkr.client_id_registry import get_client_id_registry

# Kafka 连接配置
kafka_cfg = getattr(config, "kafka_config", {}) or {}
KAFKA_BOOTSTRAP = kafka_cfg.get("bootstrap_servers", "localhost:9092")
TOPIC = "md.bar.1m.ibkr"

# IB Gateway 配置
ib_cfg = config.ib_config
IB_HOST = ib_cfg.get("host", "127.0.0.1")
IB_PORT = int(ib_cfg.get("port", 4002))
IB_CLIENT_ID = int(get_client_id_registry().resolve("vix"))

# 日志初始化
log = get_logger(__name__)


def to_msg(bar):
    """转换 IB BarData -> Kafka 消息 JSON (schema_v=2 format)"""
    ts = util.parseIBDatetime(bar.date)
    ts = ts.replace(tzinfo=timezone.utc)
    ts_start_us = int(ts.timestamp() * 1_000_000)
    ts_end_us = ts_start_us + 60_000_000  # +1 minute

    return {
        "schema_v": 2,
        "metadata": {
            "ts_us": int(time.time() * 1_000_000),
            "producer": "vix_ingestor"
        },
        "data": {
            "symbol": "VIX",
            "ts_start_us": ts_start_us,
            "ts_end_us": ts_end_us,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "vwap": float(bar.close),  # VIX doesn't have volume-weighted price
            "volume": float(getattr(bar, "volume", 0) or 0),
            "trades": 0,
            "quality": "LIVE",
            "is_backfill": 0
        }
    }


async def create_ib_connection() -> IB:
    """通过共享连接管理器获取 market session。"""
    from services.ibkr.ib_connection_manager import get_ib_connection_manager

    for attempt in range(5):
        try:
            handle = await get_ib_connection_manager().get_market_ib_handle(host=IB_HOST, port=IB_PORT)
            log.info(
                f"✅ Connected to IB Gateway at {IB_HOST}:{IB_PORT} "
                f"(market_epoch={handle.epoch}, client_id={handle.client_id})"
            )
            return handle.ib
        except Exception as e:
            log.warning(f"❌ IB connect failed (attempt {attempt+1}/5): {e}")
            await asyncio.sleep(5)
    raise RuntimeError("❌ Could not connect to IB Gateway.")


async def find_vix_contract(ib: IB) -> Index:
    """尝试从多个交易所查找 VIX 合约"""
    for ex in ("CBOE", "CBOEIND"):
        vix = Index("VIX", ex, "USD")
        cds = ib.reqContractDetails(vix)
        if cds:
            ib.qualifyContracts(vix)
            log.info(f"✅ Found VIX on {ex} (conId={cds[0].contract.conId})")
            return vix
        else:
            log.warning(f"⚠️  No VIX contract found on {ex}")
    raise RuntimeError("❌ 无法找到 VIX 合约（请检查是否开通 CBOE 指数数据包）")


async def main():
    ib = await create_ib_connection()
    vix = await find_vix_contract(ib)

    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda v: v.encode("utf-8"),
        linger_ms=5, acks="all", retries=3
    )
    await producer.start()

    last_ts = None
    try:
        while True:
            try:
                bars = ib.reqHistoricalData(
                    vix,
                    endDateTime='',
                    durationStr='1 D',
                    barSizeSetting='1 min',
                    whatToShow='TRADES',  # ✅ TRADES 可以拿到 VIX 数据
                    useRTH=True,
                    formatDate=2,
                    keepUpToDate=False,
                    timeout=30,
                )

                if not bars:
                    log.warning("⚠️ No VIX bars returned.")
                else:
                    b = bars[-1]
                    ts = str(b.date)
                    if ts != last_ts:
                        msg = to_msg(b)
                        await producer.send_and_wait(TOPIC, key="VIX", value=msg)
                        last_ts = ts
                        log.info(f"📤 Sent VIX 1m bar: {msg['data']['ts_start']}  close={b.close}")
                    else:
                        log.debug("No new VIX bar yet.")
            except Exception as e:
                log.error(f"❌ Error during fetch/send: {e}")
                # 重连 IB
                ib.disconnect()
                ib = await create_ib_connection()
                vix = await find_vix_contract(ib)

            await asyncio.sleep(55)  # 避免踩分钟边界 & IB 配额
    finally:
        await producer.stop()
        ib.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 VIX ingestor stopped by user")
