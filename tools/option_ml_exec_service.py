"""
tools/option_ml_exec_service.py

Option ML execution loop:
- Polls latest per-symbol signals from ClickHouse.
- De-dupes by (symbol, ts) to avoid repeat orders.
- Executes via services.exec.options_long_adapter.process_signals_rows().
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Dict, List
import logging
import os

from libs.db.ch_pool import get_client
from libs.logger import configure_logging, get_logger
from services.exec.options_long_adapter import process_signals_rows

logger = get_logger(__name__)

def _get_system_logger():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S')
    s_log = logging.getLogger("option_ml_system")
    if not s_log.handlers:
        s_log.setLevel(logging.INFO)
        s_log.propagate = False
        fh_s = logging.FileHandler(os.path.join(log_dir, "option_ml_system.log"), encoding='utf-8')
        fh_s.setFormatter(formatter)
        s_log.addHandler(fh_s)
    return s_log

system_logger = _get_system_logger()


def _fetch_latest_signals(max_age_sec: int) -> List[dict]:
    client = get_client()
    sql = f"""
    SELECT *
    FROM trading.option_intraday_signals
    WHERE ts >= now('America/New_York') - INTERVAL {int(max_age_sec)} SECOND
    ORDER BY ts DESC
    LIMIT 1 BY symbol
    """
    return client.query(sql)


class OptionMLExecService:
    def __init__(
        self,
        *,
        poll_sec: int = 60,
        strategy_name: str = "intraday_options_long",
        max_age_sec: int = 300,
    ) -> None:
        self.poll_sec = int(poll_sec)
        self.strategy_name = str(strategy_name)
        self.max_age_sec = int(max_age_sec)
        self._running = False
        self._last_seen: Dict[str, object] = {}

    async def start(self) -> None:
        logger.info(
            "Starting OptionMLExecService",
            poll_sec=self.poll_sec,
            strategy_name=self.strategy_name,
            max_age_sec=self.max_age_sec,
        )
        self._running = True

    async def stop(self) -> None:
        logger.info("Stopping OptionMLExecService")
        self._running = False

    def _collect_new_rows(self, rows: List[dict]) -> List[dict]:
        new_rows = []
        for row in rows:
            sym = row.get("symbol")
            ts = row.get("ts")
            if not sym or not ts:
                continue
            if self._last_seen.get(sym) == ts:
                continue
            self._last_seen[sym] = ts
            if row.get("action") == "NO_TRADE":
                continue
            new_rows.append(row)
        return new_rows

    async def run(self) -> None:
        await self.start()
        try:
            while self._running:
                try:
                    # Offload blocking ClickHouse query to thread
                    rows = await asyncio.to_thread(_fetch_latest_signals, self.max_age_sec)
                    new_rows = self._collect_new_rows(rows)
                    
                    if rows or new_rows:
                        system_logger.info(f"Exec Scan: Fetched {len(rows)} signals, {len(new_rows)} new.")
                    
                    if new_rows:
                        # Offload blocking processing/execution to thread
                        await asyncio.to_thread(process_signals_rows, new_rows, strategy_name=self.strategy_name)
                except Exception as exc:  # noqa: BLE001
                    msg = f"option_ml_exec_loop_error: {str(exc)}"
                    logger.error(msg, exc_info=True)
                    system_logger.error(msg)
                await asyncio.sleep(self.poll_sec)
        except asyncio.CancelledError:
            pass
        await self.stop()


async def _main_async(args: argparse.Namespace) -> None:
    configure_logging(level="INFO")
    service = OptionMLExecService(
        poll_sec=int(args.poll_sec),
        strategy_name=str(args.strategy_name),
        max_age_sec=int(args.max_age_sec),
    )
    await service.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Option ML execution loop")
    parser.add_argument("--poll-sec", type=int, default=60)
    parser.add_argument("--strategy-name", default="intraday_options_long")
    parser.add_argument("--max-age-sec", type=int, default=300)
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
