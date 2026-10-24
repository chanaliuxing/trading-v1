"""
tools/ingest_options_quotes.py

Fetch option chains (IBKR/stub) and ingest into trading.options_quotes_1m.

Usage:
  venv/bin/python tools/ingest_options_quotes.py \
    --provider ibkr \
    --symbols SPY,QQQ \
    --dte 5,7,10 \
    --right C \
    --max-contracts 500
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, date
from zoneinfo import ZoneInfo
from pathlib import Path
import sys
import os
import fcntl
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger, configure_logging
from services.market.options_data import create_options_provider, OptionContract
from services.market.options_quote_live_state import update_from_ingest_rows as update_live_state_from_ingest_rows
from services.market.options_quote_memory_cache import update_from_ingest_rows

logger = get_logger(__name__)
_RUN_GUARD_ACTIVE = False
_CH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="options_quotes_ch")

_OPTIONS_QUOTES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS trading.options_quotes_1m
(
    ts DateTime64(3, 'America/New_York'),
    underlying LowCardinality(String),
    expiry Date,
    strike Float64,
    right LowCardinality(String),
    dte Int16,
    bid Float64,
    ask Float64,
    mid Float64,
    last Float64,
    volume UInt64,
    open_interest UInt64,
    iv Float64,
    delta Float64,
    gamma Float64,
    theta Float64,
    vega Float64,
    spread_bps Float64,
    contracts_mult UInt16,
    source LowCardinality(String),
    created_at DateTime64(3, 'America/New_York')
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY toDate(ts)
ORDER BY (underlying, ts, expiry, right, strike)
SETTINGS index_granularity = 8192
"""


def ensure_options_quotes_table() -> None:
    client = get_client()
    client.command(_OPTIONS_QUOTES_TABLE_DDL)


async def _run_ch_blocking(fn):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_CH_EXECUTOR, fn)


def _parse_dte_list(dte_str: str) -> List[int]:
    return [int(x.strip()) for x in dte_str.split(",") if x.strip()]


def _calc_dte(expiry_str: str) -> int:
    try:
        expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        return max((expiry - date.today()).days, 0)
    except Exception:
        return 0


async def _fetch_chain(provider, underlying: str, dte_candidates: List[int], right: str | None) -> List[OptionContract]:
    return await provider.get_chain(underlying=underlying, dte_candidates=dte_candidates, right=right)


import math

def _safe_int(val) -> int:
    try:
        if val is None:
            return 0
        if isinstance(val, float) and math.isnan(val):
            return 0
        return int(val)
    except Exception:
        return 0

def _build_rows(contracts: List[OptionContract], provider_name: str) -> List[tuple]:
    rows = []
    now_et = datetime.now(ZoneInfo("America/New_York"))
    # Keep ts aligned to minute bucket so strategy queries using exact minute ts can match.
    ts_bucket = now_et.replace(second=0, microsecond=0)
    for c in contracts:
        expiry = c.expiry_date
        dte = _calc_dte(expiry)
        mid = c.mid
        spread_bps = c.spread_bps
        rows.append(
            (
                ts_bucket,
                c.underlying,
                datetime.strptime(expiry, "%Y-%m-%d").date(),
                float(c.strike),
                c.right,
                dte,
                float(c.bid or 0.0),
                float(c.ask or 0.0),
                float(mid or 0.0),
                float(c.last or 0.0) if c.last is not None else 0.0,
                _safe_int(c.volume),
                _safe_int(c.open_interest),
                float(c.implied_vol or 0.0) if c.implied_vol is not None else 0.0,
                float(c.delta or 0.0) if c.delta is not None else 0.0,
                float(c.gamma or 0.0) if c.gamma is not None else 0.0,
                float(c.theta or 0.0) if c.theta is not None else 0.0,
                float(c.vega or 0.0) if c.vega is not None else 0.0,
                float(spread_bps or 0.0),
                100,
                provider_name,
                now_et,
            )
        )
    return rows


def _summarize_contracts(
    underlying: str,
    contracts: List[OptionContract],
    dte_candidates: List[int],
    right: str | None,
) -> dict:
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    dte0 = 0
    expiry_today = 0
    with_nbbo = 0
    with_mid = 0
    rights = set()
    expiries = set()
    for contract in contracts:
        expiry = str(contract.expiry_date or "")
        expiries.add(expiry)
        rights.add(str(contract.right or ""))
        if expiry == today:
            expiry_today += 1
        if _calc_dte(expiry) == 0:
            dte0 += 1
        if float(contract.bid or 0.0) > 0.0 and float(contract.ask or 0.0) > 0.0:
            with_nbbo += 1
        if float(contract.mid or 0.0) > 0.0:
            with_mid += 1
    return {
        "underlying": underlying,
        "requested_dte": list(dte_candidates),
        "requested_right": right or "BOTH",
        "contracts_total": len(contracts),
        "contracts_dte0": dte0,
        "contracts_expiry_today": expiry_today,
        "contracts_with_nbbo": with_nbbo,
        "contracts_with_mid": with_mid,
        "rights_seen": sorted(x for x in rights if x),
        "expiry_sample": sorted(x for x in expiries if x)[:3],
    }


def _insert_rows(rows: List[tuple]) -> int:
    if not rows:
        return 0
    client = get_client()
    client.insert(
        "trading.options_quotes_1m",
        rows,
        column_names=[
            "ts",
            "underlying",
            "expiry",
            "strike",
            "right",
            "dte",
            "bid",
            "ask",
            "mid",
            "last",
            "volume",
            "open_interest",
            "iv",
            "delta",
            "gamma",
            "theta",
            "vega",
            "spread_bps",
            "contracts_mult",
            "source",
            "created_at",
        ],
    )
    # Only publish into the in-process cache after persistence succeeds.
    # This keeps the memory path and replay/audit storage on the same truth.
    update_from_ingest_rows(rows)
    # Optional cross-process realtime state layer (e.g., Redis backend).
    update_live_state_from_ingest_rows(rows)
    return len(rows)


async def fetch_and_insert_quotes(
    provider,
    symbols: List[str],
    dte_candidates: List[int],
    right: str | None,
    *,
    provider_name: str,
    max_contracts: int = 0,
) -> tuple[int, List[dict]]:
    rows: List[tuple] = []
    diagnostics: List[dict] = []
    for sym in symbols:
        contracts = await _fetch_chain(provider, sym, dte_candidates, right)
        if max_contracts:
            contracts = contracts[:max_contracts]
        diagnostics.append(_summarize_contracts(sym, contracts, dte_candidates, right))
        rows.extend(_build_rows(contracts, provider_name))

    if not rows:
        return 0, diagnostics
    return _insert_rows(rows), diagnostics


class OptionQuotesIngestService:
    def __init__(
        self,
        *,
        provider: str = "ibkr",
        symbols: List[str] | None = None,
        dte: List[int] | None = None,
        right: str | None = None,
        poll_sec: int = 120,
        max_contracts: int = 0,
        ibkr_host: str = "127.0.0.1",
        ibkr_port: int = 4002,
        ibkr_client_id: int = 7,
    ) -> None:
        self.provider_name = provider
        self.symbols = symbols or ["SPY", "QQQ"]
        self.dte_candidates = dte or [7, 14, 21, 30]
        self.right = right if right in ("C", "P") else None
        self.poll_sec = int(poll_sec)
        self.max_contracts = int(max_contracts)
        self.ibkr_host = ibkr_host
        self.ibkr_port = int(ibkr_port)
        self.ibkr_client_id = int(ibkr_client_id)
        self._running = False
        self._provider = None
        self._rth_only = str(os.getenv("RTH_ONLY", "true")).lower() in ("1", "true", "yes", "on")
        self._rth_start = os.getenv("RTH_START", "09:30")
        self._rth_end = os.getenv("RTH_END", "16:00")
        # Avoid duplicate inserts within the same minute bucket. Retry is still allowed
        # when a prior attempt in that minute returned no rows.
        self._last_successful_bucket: Optional[datetime] = None
        self._failure_streak: int = 0
        self._max_backoff_multiplier: int = 6
        self._lock_fp = None

    def _is_rth(self) -> bool:
        try:
            now_et = datetime.now(ZoneInfo("America/New_York"))
            start_h, start_m = [int(x) for x in self._rth_start.split(":")]
            end_h, end_m = [int(x) for x in self._rth_end.split(":")]
            start = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            end = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            is_rth = start <= now_et <= end and now_et.weekday() < 5
            return is_rth
        except Exception as exc:
            logger.warning("RTH check failed, defaulting to False (Paused)", error=str(exc))
            return False

    async def start(self) -> None:
        ensure_options_quotes_table()
        self._acquire_instance_lock()
        try:
            self._provider = create_options_provider(
                self.provider_name,
                host=self.ibkr_host,
                port=self.ibkr_port,
                client_id=self.ibkr_client_id,
            )
            self._running = True
            logger.info(
                "Starting OptionQuotesIngestService",
                provider=self.provider_name,
                symbols=self.symbols,
                dte=self.dte_candidates,
                right=self.right,
                poll_sec=self.poll_sec,
            )
        except Exception:
            # Ensure lock is not leaked when provider init/startup fails.
            self._release_instance_lock()
            self._provider = None
            raise

    async def stop(self) -> None:
        self._running = False
        if self._provider and hasattr(self._provider, "disconnect"):
            try:
                await self._provider.disconnect()
            except Exception:
                pass
        self._provider = None
        self._release_instance_lock()
        logger.info("Stopping OptionQuotesIngestService")

    def _acquire_instance_lock(self) -> None:
        if self._lock_fp is not None:
            return
        lock_path = Path(os.getenv("OPTIONS_QUOTES_LOCK_PATH", "runtime/options_quotes_ingest.lock"))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fp = open(lock_path, "a+")
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fp.close()
            raise RuntimeError(f"options_quotes_ingest lock busy: {lock_path}") from exc
        fp.seek(0)
        fp.truncate(0)
        fp.write(f"{os.getpid()}\n")
        fp.flush()
        self._lock_fp = fp

    def _release_instance_lock(self) -> None:
        if self._lock_fp is None:
            return
        try:
            fcntl.flock(self._lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._lock_fp.close()
        except Exception:
            pass
        self._lock_fp = None

    def _bucket_has_rows(self, bucket: datetime) -> bool:
        dte_in = ",".join(str(int(x)) for x in sorted(set(self.dte_candidates or [])))
        symbols_in = ",".join(f"'{s}'" for s in sorted(set(self.symbols or [])))
        if not dte_in or not symbols_in:
            return False
        required_symbols = len(set(self.symbols or []))
        required_sides = 1 if self.right in ("C", "P") else 2
        required_symbol_side = required_symbols * required_sides
        q = f"""
        SELECT
          count() AS c,
          uniqExact(underlying) AS u_sym,
          uniqExact(tuple(underlying, `right`)) AS u_side
        FROM trading.options_quotes_1m
        WHERE ts = {{ts:DateTime64(3, 'America/New_York')}}
          AND source = {{source:String}}
          AND underlying IN ({symbols_in})
          AND dte IN ({dte_in})
        """
        rows = get_client().query(
            q,
            parameters={"ts": bucket, "source": self.provider_name},
        )
        if not rows:
            return False
        row = rows[0]
        count_rows = int(row.get("c") or 0)
        uniq_symbols = int(row.get("u_sym") or 0)
        uniq_symbol_side = int(row.get("u_side") or 0)
        return (
            count_rows >= required_symbol_side
            and uniq_symbols >= required_symbols
            and uniq_symbol_side >= required_symbol_side
        )

    async def _bucket_has_rows_async(self, bucket: datetime) -> bool:
        return await _run_ch_blocking(lambda: self._bucket_has_rows(bucket))

    async def run(self) -> None:
        global _RUN_GUARD_ACTIVE
        if _RUN_GUARD_ACTIVE:
            logger.error("OptionQuotesIngestService already active in process; refusing duplicate run()")
            return
        _RUN_GUARD_ACTIVE = True
        # Lazy start: don't connect immediately to avoid off-hours timeouts
        self._running = True 
        try:
            while self._running:
                loop_ok = True
                try:
                    now_et = datetime.now(ZoneInfo("America/New_York"))
                    bucket = now_et.replace(second=0, microsecond=0)

                    if self._rth_only and not self._is_rth():
                        logger.info("Options quotes ingest paused (outside RTH)")
                        # Disconnect if connected to save resources/avoid heartbeat errors
                        if self._provider:
                            await self.stop()
                            self._provider = None
                            self._running = True # restore running flag cleared by stop()
                        await asyncio.sleep(self.poll_sec)
                        continue
                    
                    if not self._provider:
                        await self.start()

                    # Keep one successful write per minute bucket to match strategy's
                    # exact-minute reads and prevent duplicate rows per contract.
                    if self._last_successful_bucket is not None and bucket <= self._last_successful_bucket:
                        await asyncio.sleep(self.poll_sec)
                        continue
                    # Also guard against duplicates across restarts/processes.
                    if await self._bucket_has_rows_async(bucket):
                        self._last_successful_bucket = bucket
                        await asyncio.sleep(self.poll_sec)
                        continue

                    inserted, diagnostics = await fetch_and_insert_quotes(
                        self._provider,
                        self.symbols,
                        self.dte_candidates,
                        self.right,
                        provider_name=self.provider_name,
                        max_contracts=self.max_contracts,
                    )
                    if inserted:
                        self._last_successful_bucket = bucket
                        logger.info("Inserted option quotes", rows=inserted, diagnostics=diagnostics)
                    else:
                        loop_ok = False
                        logger.warning("No option quotes fetched", diagnostics=diagnostics)
                except Exception as exc:
                    loop_ok = False
                    logger.error("options_quotes_ingest_error", error=str(exc), exc_info=True)
                if loop_ok:
                    self._failure_streak = 0
                else:
                    self._failure_streak += 1
                backoff_mult = min(1 + self._failure_streak, self._max_backoff_multiplier)
                sleep_sec = float(self.poll_sec) * float(backoff_mult)
                if self._failure_streak > 0:
                    logger.warning(
                        "options_quotes_ingest_backoff_active",
                        failure_streak=self._failure_streak,
                        sleep_sec=round(sleep_sec, 2),
                        poll_sec=self.poll_sec,
                    )
                await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            pass
        try:
            await self.stop()
        finally:
            _RUN_GUARD_ACTIVE = False


async def main_async(args: argparse.Namespace) -> None:
    ensure_options_quotes_table()
    provider = create_options_provider(
        args.provider,
        host=args.ibkr_host,
        port=args.ibkr_port,
        client_id=args.ibkr_client_id,
    )
    dte_candidates = _parse_dte_list(args.dte)
    right = args.right if args.right in ("C", "P") else None

    inserted, diagnostics = await fetch_and_insert_quotes(
        provider,
        args.symbols,
        dte_candidates,
        right,
        provider_name=args.provider,
        max_contracts=args.max_contracts,
    )
    if inserted:
        logger.info("Inserted option quotes", rows=inserted, diagnostics=diagnostics)
    else:
        logger.warning("No option quotes fetched", diagnostics=diagnostics)
    if hasattr(provider, "disconnect"):
        await provider.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="stub", help="Data provider: stub|ibkr|clickhouse")
    parser.add_argument("--symbols", default="SPY,QQQ", help="Comma-separated underlyings")
    parser.add_argument("--dte", default="5,7,10", help="Comma-separated DTE candidates")
    parser.add_argument("--right", default="", help="C or P to filter; empty for both")
    parser.add_argument("--max-contracts", type=int, default=0, help="Optional cap per symbol")
    parser.add_argument("--ibkr-host", default="127.0.0.1")
    parser.add_argument("--ibkr-port", type=int, default=4002)
    parser.add_argument("--ibkr-client-id", type=int, default=0)
    args = parser.parse_args()

    args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    configure_logging(level="INFO")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
