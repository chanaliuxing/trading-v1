# services/obs/writers.py
"""
Minimal async writers for ClickHouse and Loki with batch support.
Used by observability queue to flush events.
"""
import asyncio
import datetime as dt
import os
from typing import Dict, List, Any

import aiohttp
try:
    import orjson as json
except ImportError:
    import json

from libs.logger import get_logger

logger = get_logger(__name__)


# --------------------------
# ClickHouse Batch Writer
# --------------------------
class ClickHouseWriter:
    """Minimal async writer using HTTP interface + JSONEachRow."""
    def __init__(self,
                 base_url: str,
                 database: str = "trading",
                 user: str | None = None,
                 password: str | None = None,
                 timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.auth = aiohttp.BasicAuth(user, password) if user else None
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
            logger.info(f"ClickHouseWriter started: {self.base_url}")

    async def close(self):
        if self._session:
            await self._session.close()
            await asyncio.sleep(0.05)
            self._session = None
            logger.info("ClickHouseWriter closed")

    async def write_batch(self, table: str, rows: List[Dict[str, Any]]):
        """Write a batch of rows to ClickHouse table using JSONEachRow format."""
        if not rows:
            return
        if not self._session:
            await self.start()

        # Build payload for JSONEachRow (newline-delimited JSON)
        payload_lines = []
        for r in rows:
            # Convert datetime objects to ClickHouse format
            # ClickHouse prefers "YYYY-MM-DD HH:MM:SS.fff" format (no timezone suffix)
            processed = {}
            for k, v in r.items():
                if isinstance(v, dt.datetime):
                    if v.tzinfo is None:
                        v = v.replace(tzinfo=dt.timezone.utc)
                    # Format as YYYY-MM-DD HH:MM:SS.fff (ClickHouse expects UTC, no TZ suffix)
                    processed[k] = v.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]  # millisecond precision
                else:
                    processed[k] = v

            if hasattr(json, 'dumps') and json.__name__ == 'orjson':
                payload_lines.append(json.dumps(processed).decode())
            else:
                payload_lines.append(json.dumps(processed))

        payload = "\n".join(payload_lines) + "\n"
        query = f"INSERT INTO {self.database}.{table} FORMAT JSONEachRow"
        url = f"{self.base_url}/?database={self.database}"

        try:
            async with self._session.post(url, data=query + "\n" + payload, auth=self.auth) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"ClickHouse insert failed for {table}: {resp.status} - {text[:500]}")
                    raise RuntimeError(f"CH insert failed: {resp.status} - {text}")
                else:
                    logger.debug(f"ClickHouse batch written: {table} ({len(rows)} rows)")
        except Exception as e:
            logger.error(f"ClickHouse write error for {table}: {e}", exc_info=True)
            raise


# --------------------------
# Loki Batch Writer
# --------------------------
class LokiWriter:
    """Minimal async Loki writer with JSON streams."""
    def __init__(self, loki_url: str, default_job: str = "trader", default_service: str = "app", timeout: int = 10):
        self.loki_url = loki_url.rstrip("/")
        self.default_job = default_job
        self.default_service = default_service
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
            logger.info(f"LokiWriter started: {self.loki_url}")

    async def close(self):
        if self._session:
            await self._session.close()
            await asyncio.sleep(0.05)
            self._session = None
            logger.info("LokiWriter closed")

    @staticmethod
    def _ns(ts: Any) -> str:
        """Convert timestamp to nanoseconds string for Loki."""
        if ts is None:
            return str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1_000_000_000))
        if isinstance(ts, (int, float)):
            # assume seconds
            return str(int(float(ts) * 1_000_000_000))
        if isinstance(ts, dt.datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            return str(int(ts.timestamp() * 1_000_000_000))
        # iso8601 string
        try:
            parsed = dt.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return str(int(parsed.timestamp() * 1_000_000_000))
        except Exception:
            return str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1_000_000_000))

    async def write_batch(self, events: List[Dict[str, Any]]):
        """Write a batch of events to Loki."""
        if not events:
            return
        if not self._session:
            await self.start()

        # Group by low-cardinality label set
        # IMPORTANT: DO NOT put high-cardinality fields (symbol, order_id) in labels
        from typing import Tuple
        streams: Dict[Tuple[str, str, str], List[List[str]]] = {}

        for ev in events:
            labels = {
                'job': ev.get('job', self.default_job),
                'service_name': ev.get('service_name', self.default_service),
                'event': ev.get('event', 'log')
            }
            key = (labels['job'], labels['service_name'], labels['event'])
            ts_ns = self._ns(ev.get('ts'))

            # Store full JSON in line
            if hasattr(json, 'dumps') and json.__name__ == 'orjson':
                line = json.dumps(ev).decode()
            else:
                line = json.dumps(ev)

            streams.setdefault(key, []).append([ts_ns, line])

        payload = {
            'streams': [
                { 'stream': { 'job': k[0], 'service_name': k[1], 'event': k[2] }, 'values': v }
                for k, v in streams.items()
            ]
        }

        url = f"{self.loki_url}/loki/api/v1/push"
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    logger.error(f"Loki push failed: {resp.status} - {text[:500]}")
                    raise RuntimeError(f"Loki push failed: {resp.status}")
                else:
                    logger.debug(f"Loki batch written ({len(events)} events)")
        except Exception as e:
            logger.error(f"Loki write error: {e}", exc_info=True)
            raise


# --------------------------
# Quick demo / manual test
# --------------------------
async def _demo():
    """Demo showing how to use the writers."""
    ch = ClickHouseWriter(
        base_url=os.getenv('CH_URL', 'http://localhost:8123'),
        database=os.getenv('CH_DB', 'trading'),
        user=os.getenv('CH_USER'),
        password=os.getenv('CH_PASS')
    )
    loki = LokiWriter(loki_url=os.getenv('LOKI_URL', 'http://localhost:3100'))

    await ch.start()
    await loki.start()

    now = dt.datetime.now(dt.timezone.utc)

    # ClickHouse: insert demo row into equity_pnl_snapshots
    try:
        await ch.write_batch('equity_pnl_snapshots', [
            {
                'ts': now,
                'account_id': 'DU123456',
                'equity': 1_000_000.0,
                'buying_power': 500_000.0,
                'margin_used_pct': 35.0,
                'day_pnl': 1200.5,
                'day_mdd': -800.2,
                'core_pct': 0.6,
                't_pct': 0.3,
                'hedge_pct': 0.1,
                'trace_id': 'demo-trace-1'
            }
        ])
        print("✓ ClickHouse demo write successful")
    except Exception as e:
        print(f"✗ ClickHouse demo write failed: {e}")

    # Loki: push demo events
    try:
        await loki.write_batch([
            {
                'event': 'service_heartbeat',
                'service_name': 'PortfolioService',
                'status': 'healthy',
                'ts': now.isoformat(),
                'trace_id': 'demo-trace-1'
            },
            {
                'event': 'order_lifecycle',
                'service_name': 'ExecService',
                'order_id': 'OID123',
                'stage': 'submitted',
                'symbol': 'AAPL',
                'qty': 100,
                'px': 175.2,
                'ts': now.isoformat(),
                'trace_id': 'demo-trace-1'
            }
        ])
        print("✓ Loki demo write successful")
    except Exception as e:
        print(f"✗ Loki demo write failed: {e}")

    await ch.close()
    await loki.close()


if __name__ == '__main__':
    asyncio.run(_demo())
