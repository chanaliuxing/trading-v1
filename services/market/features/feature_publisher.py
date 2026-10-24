"""
Feature Publisher
Publishes computed features from ClickHouse to Kafka
Based on doc/58.md specification

Publishes to: md.features.5s.v1 and md.features.10s.v1
"""
import asyncio
import json
import time
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from aiokafka import AIOKafkaProducer

from libs.db.ch_pool import get_client
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)


class FeaturePublisher:
    """
    Publishes features from ClickHouse to Kafka

    Features:
    - Incremental publishing (tracks last published timestamp)
    - Batch publishing for efficiency
    - Handles late arrivals (publishes updated versions)
    """

    def __init__(
        self,
        resolution: str = "5s",
        topic_prefix: str = "md.features",
        poll_interval_sec: float = 1.0,
        batch_size: int = 1000
    ):
        """
        Initialize feature publisher

        Args:
            resolution: Bar resolution ('5s' or '10s')
            topic_prefix: Kafka topic prefix
            poll_interval_sec: How often to poll for new features
            batch_size: Max features per batch
        """
        self.resolution = resolution
        self.topic_prefix = topic_prefix
        self.poll_interval_sec = poll_interval_sec
        self.batch_size = batch_size

        # Kafka topic
        self.topic = f"{topic_prefix}.{resolution}.v1"

        # ClickHouse - use global connection pool
        self.ch = get_client()

        # Kafka producer
        kafka_cfg = config.kafka_config
        self.producer = AIOKafkaProducer(
            bootstrap_servers=kafka_cfg['bootstrap_servers'],
            acks='all',
            linger_ms=5,
            compression_type='lz4',
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            key_serializer=lambda k: k.encode('utf-8') if k else None
        )

        # Table names
        if resolution == "5s":
            self.feature_table = "features_5s"
            self.bar_sec = 5
        elif resolution == "10s":
            self.feature_table = "features_10s"
            self.bar_sec = 10
        else:
            raise ValueError(f"Invalid resolution: {resolution}")

        # State
        self.last_published_ts: Optional[datetime] = None
        self._running = False

        logger.info("FeaturePublisher initialized",
                    resolution=resolution,
                    topic=self.topic)

    async def start(self):
        """Start feature publisher"""
        await self.producer.start()
        self._running = True

        # Initialize last published timestamp
        await self._initialize_watermark()

        # Start publishing loop
        asyncio.create_task(self._publish_loop())

        logger.info("FeaturePublisher started", last_ts=self.last_published_ts)

    async def stop(self):
        """Stop feature publisher"""
        self._running = False

        if self.producer:
            try:
                await asyncio.wait_for(self.producer.stop(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Feature publisher stop timed out (degraded Kafka), forcing close")
            except Exception as e:
                logger.warning("Feature publisher stop failed", error=str(e))

        # Note: ClickHouse client uses global connection pool - no close needed

        logger.info("FeaturePublisher stopped")

    async def _initialize_watermark(self):
        """Initialize last published timestamp from ClickHouse"""
        try:
            sql = f"""
            SELECT max(ts) as max_ts
            FROM {self.feature_table} FINAL
            WHERE ts < now() - INTERVAL 60 SECOND
            """

            result = await asyncio.to_thread(lambda: get_client().query(sql))
            if result and result[0]['max_ts']:
                self.last_published_ts = result[0]['max_ts']
            else:
                # Default to 1 hour ago
                self.last_published_ts = datetime.now() - timedelta(hours=1)

        except Exception as e:
            logger.warning("Failed to initialize watermark", error=str(e))
            self.last_published_ts = datetime.now() - timedelta(hours=1)

    async def _publish_loop(self):
        """Main publishing loop"""
        while self._running:
            try:
                await self._publish_batch()
            except Exception as e:
                logger.error("Publish loop error", error=str(e))

            await asyncio.sleep(self.poll_interval_sec)

    async def _publish_batch(self):
        """Publish a batch of features"""
        try:
            # Query features newer than last published
            sql = f"""
            SELECT
                symbol,
                ts,
                close,
                vwap,
                atr_14,
                atr_5,
                ema_10,
                ema_20,
                ema_50,
                volume,
                vol_ema_20,
                vol_spike,
                z_price_20,
                z_vol_20,
                spread_bps,
                slope_20,
                r2_20,
                ver
            FROM {self.feature_table} FINAL
            WHERE ts > {{last_ts:DateTime}}
            ORDER BY ts ASC
            LIMIT {self.batch_size}
            """

            params = {
                'last_ts': self.last_published_ts or datetime.now() - timedelta(hours=1)
            }

            features = await asyncio.to_thread(
                lambda: get_client().query(sql, parameters=params)
            )

            if not features:
                return

            # Publish each feature
            for feature in features:
                await self._publish_feature(feature)
                self.last_published_ts = max(
                    self.last_published_ts or feature['ts'],
                    feature['ts']
                )

            logger.debug("Features published",
                        resolution=self.resolution,
                        count=len(features),
                        last_ts=self.last_published_ts)

        except Exception as e:
            logger.error("Failed to publish batch",
                        error=str(e),
                        resolution=self.resolution)

    async def _publish_feature(self, feature: Dict[str, Any]):
        """Publish single feature to Kafka"""
        try:
            # Convert to Kafka message format
            message = self._to_kafka_message(feature)

            # Publish
            await self.producer.send(
                topic=self.topic,
                key=feature['symbol'],
                value=message
            )

        except Exception as e:
            logger.error("Failed to publish feature",
                        error=str(e),
                        symbol=feature.get('symbol'))

    def _to_kafka_message(self, feature: Dict[str, Any]) -> Dict[str, Any]:
        """Convert ClickHouse row to Kafka message format"""
        ts_epoch_sec = int(feature['ts'].timestamp())

        return {
            "schema_v": 1,
            "metadata": {
                "ts_us": int(time.time() * 1e6),
                "producer": f"feature_publisher_{self.resolution}"
            },
            "data": {
                "symbol": feature['symbol'],
                "bar_sec": self.bar_sec,
                "ts_start": ts_epoch_sec,
                "ts_end": ts_epoch_sec + self.bar_sec,
                "ver": int(feature['ver']),
                "is_backfill": False,

                # OHLCV
                "close": round(feature['close'], 4),
                "vwap": round(feature['vwap'], 4) if feature['vwap'] else None,
                "volume": feature['volume'],

                # Features
                "atr_14": round(feature['atr_14'], 4) if feature['atr_14'] else None,
                "atr_5": round(feature['atr_5'], 4) if feature['atr_5'] else None,
                "ema_10": round(feature['ema_10'], 4) if feature['ema_10'] else None,
                "ema_20": round(feature['ema_20'], 4) if feature['ema_20'] else None,
                "ema_50": round(feature['ema_50'], 4) if feature['ema_50'] else None,
                "vol_ema_20": round(feature['vol_ema_20'], 4) if feature['vol_ema_20'] else None,
                "vol_spike": round(feature['vol_spike'], 4) if feature['vol_spike'] else None,
                "z_price_20": round(feature['z_price_20'], 4) if feature['z_price_20'] else None,
                "z_vol_20": round(feature['z_vol_20'], 4) if feature['z_vol_20'] else None,
                "spread_bps": round(feature['spread_bps'], 2) if feature['spread_bps'] else None,
                "slope_20": round(feature['slope_20'], 6) if feature['slope_20'] else None,
                "r2_20": round(feature['r2_20'], 4) if feature['r2_20'] else None,

                "qscore": 100  # Placeholder - would come from quality assessment
            }
        }

    def health_check(self) -> bool:
        """Check if publisher is healthy"""
        return self.ch.health_check()
