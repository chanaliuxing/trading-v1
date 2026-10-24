"""
Feature Calculator Service
Computes comprehensive technical indicators from 5s/10s bars using ClickHouse window functions
Based on doc/58.md specification

Features computed:
- Price & Returns: ret_1, ret_5, log_ret, roc_5, roc_10
- Moving Averages: sma_5, sma_20, sma_60, std_20, std_60, z_price_60
- Bollinger Bands: bb_mid_20, bb_up_20, bb_dn_20, bb_bw_20, bb_pct_20
- Momentum: stoch_k_14, stoch_d_3, rsi_14
- Volatility: rv_20, parkinson_20, garman_klass_20
- Donchian Channel: donchian_hi_20, donchian_lo_20
- ADX/DMI: di_plus_14, di_minus_14, dx_14, adx_14
- Volume: vol_avg_20, rvol_roll_20, rvol_tod_20d
- Money Flow: cmf_20, mfi_14, obv_60
- VWAP: vwap_day, vwap_dev_bps, vwap_slope_20
- Microstructure: micro_mid, close_to_mid_bps
- Candlestick: body, upper_wick, lower_wick, range, range_pct
- Time: bar_sec, minute_of_day
- OHLC: open, high, low, vwap_bar, trades
"""
import asyncio
import os
import random
import time
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import sys
import os

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from libs.db import ch_pool  # Use shared global connection pool
from libs.logger import get_logger
from libs.config import config


# Columns emitted by feature computation - must match table schema exactly
# Schema order from positions 1-112 in features_5s table
_FEATURE_INSERT_COLUMNS = [
    # Position 1-4: Core identifiers and prices
    "symbol",          # 1
    "ts",              # 2
    "close",           # 3
    "vwap",            # 4

    # Position 5-17: Legacy/placeholder columns (some computed, some NULL)
    "atr_14",          # 5
    "atr_5",           # 6 - legacy, will be NULL
    "ema_10",          # 7 - legacy, will be NULL
    "ema_20",          # 8 - legacy, will be NULL
    "ema_50",          # 9 - legacy, will be NULL
    "volume",          # 10
    "vol_ema_20",      # 11 - legacy, will be NULL
    "vol_spike",       # 12 - legacy, will be NULL
    "z_price_20",      # 13 - legacy, will be NULL
    "z_vol_20",        # 14 - legacy, will be NULL
    "spread_bps",      # 15
    "slope_20",        # 16 - legacy, will be NULL
    "r2_20",           # 17 - legacy, will be NULL

    # Position 18-19: Metadata
    "ver",             # 18
    "created_at",      # 19

    # Position 20-24: Returns and momentum
    "ret_1",           # 20
    "ret_5",           # 21
    "log_ret",         # 22
    "roc_5",           # 23
    "roc_10",          # 24

    # Position 25-30: Moving averages and volatility
    "sma_5",           # 25
    "sma_20",          # 26
    "sma_60",          # 27
    "std_20",          # 28
    "std_60",          # 29
    "z_price_60",      # 30

    # Position 31-35: Bollinger Bands
    "bb_mid_20",       # 31
    "bb_up_20",        # 32
    "bb_dn_20",        # 33
    "bb_bw_20",        # 34
    "bb_pct_20",       # 35

    # Position 36-38: Stochastic and RSI
    "stoch_k_14",      # 36
    "stoch_d_3",       # 37
    "rsi_14",          # 38

    # Position 39-41: Volatility estimators
    "rv_20",           # 39
    "parkinson_20",    # 40
    "garman_klass_20", # 41

    # Position 42-43: Donchian Channel
    "donchian_hi_20",  # 42
    "donchian_lo_20",  # 43

    # Position 44-47: ADX/DMI
    "di_plus_14",      # 44
    "di_minus_14",     # 45
    "dx_14",           # 46
    "adx_14",          # 47

    # Position 48-50: Volume indicators
    "vol_avg_20",      # 48
    "rvol_roll_20",    # 49
    "rvol_tod_20d",    # 50

    # Position 51-53: Money flow
    "cmf_20",          # 51
    "mfi_14",          # 52
    "obv_60",          # 53

    # Position 54-56: VWAP
    "vwap_day",        # 54
    "vwap_dev_bps",    # 55
    "vwap_slope_20",   # 56

    # Position 57-58: Microstructure
    "micro_mid",       # 57
    "close_to_mid_bps",# 58

    # Position 59-63: Candlestick
    "body",            # 59
    "upper_wick",      # 60
    "lower_wick",      # 61
    "range",           # 62
    "range_pct",       # 63

    # Position 64-65: Time features
    "bar_sec",         # 64
    "minute_of_day",   # 65

    # Position 66-70: OHLC and trades
    "open",            # 66
    "high",            # 67
    "low",             # 68
    "vwap_bar",        # 69
    "trades",          # 70

    # Position 71-76: VIX features
    "vix",             # 71
    "vix_sma_20",      # 72
    "vix_z_20",        # 73
    "vix_chg_1m",      # 74
    "vix_chg_5m",      # 75
    "vix_rsi_14",      # 76

    # Position 77-80: NEW Tier-1 EMA features
    "ema_9",           # 77
    "ema_21",          # 78
    "ema_cross",       # 79
    "vix_diff",        # 80

    # Position 81: NEW Tier-1 ATR
    "atr_20",          # 81

    # Position 82-90: NEW Tier-1 Market context (SPY/QQQ)
    "spy_ret_1",       # 82
    "spy_ret_5",       # 83
    "qqq_ret_1",       # 84
    "qqq_ret_5",       # 85
    "beta_spy_20",     # 86
    "beta_spy_60",     # 87
    "corr_spy_20",     # 88
    "corr_spy_60",     # 89
    "corr_qqq_20",     # 90

    # Position 91-94: NEW Tier-1 Microstructure
    "tick_imbalance",  # 91
    "trade_count_ratio", # 92
    "avg_trade_size",  # 93
    "price_impact_5m", # 94

    # Position 95-105: NEW Tier-1 Statistical features
    "zscore_close_20", # 95
    "zscore_close_60", # 96
    "zscore_volume_20",# 97
    "zscore_volume_60",# 98
    "skew_20",         # 99
    "skew_60",         # 100
    "kurt_20",         # 101
    "kurt_60",         # 102
    "autocorr_ret_1",  # 103
    "autocorr_ret_5",  # 104
    "hurst_20",        # 105

    # Position 106-110: NEW Tier-1 Temporal/meta features
    "time_since_open", # 106
    "day_of_week",     # 107
    "hour_of_day",     # 108
    "is_first_30min",  # 109
    "is_last_30min",   # 110

    # Position 111-112: NEW Tier-1 Advanced features
    "kama_10",         # 111
    "entropy_20",      # 112
]

logger = get_logger(__name__)

# Shared thread pool for offloading blocking ClickHouse operations
# Max 4 workers to prevent thread pool saturation
_ch_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ch_writer")

async def _execute_ch_command_async(
    sql: str,
    send_receive_timeout_sec: Optional[float] = None,
) -> None:
    """
    Async ClickHouse command execution.

    Uses a thread pool to execute the blocking ClickHouse command while
    obtaining a fresh client instance for each invocation. The underlying
    HTTP connections are pooled globally by ch_pool, but clients/sessions
    are not shared across threads which avoids "concurrent queries within
    the same session" errors.
    """
    loop = asyncio.get_running_loop()
    def _run() -> None:
        client_kwargs: Dict[str, Any] = {
            # Raise connect timeout from the pool default (2 s) so transient
            # CH pressure does not immediately produce ConnectTimeoutError.
            "connect_timeout": 5.0,
        }
        if send_receive_timeout_sec is not None:
            client_kwargs["send_receive_timeout"] = float(send_receive_timeout_sec)
        ch_pool.get_client(**client_kwargs).command(sql)

    await loop.run_in_executor(_ch_executor, _run)


class FeatureCalculator:
    """
    Real-time feature calculator

    Features:
    - Computes features from bars using window functions
    - No look-ahead bias (uses lagInFrame for previous close)
    - Watermark-based emission (waits N seconds after bar end)
    - Recomputes recent windows to handle late arrivals
    - Quality gating (only compute for high-quality bars)
    """

    def __init__(
        self,
        resolution: str = "5s",
        horizon_minutes: int = 15,
        waterline_delay_sec: int = 2,
        qscore_threshold: int = 70,
        poll_interval_sec: float = 5.0,
        rvol_tod_days: int = 5,
        rvol_roll_window: int = 20
    ):
        """
        Initialize feature calculator

        Args:
            resolution: Bar resolution ('5s' or '10s')
            horizon_minutes: How far back to recompute (handles late arrivals)
            waterline_delay_sec: Wait N seconds after bar end before computing
            qscore_threshold: Minimum quality score required
            poll_interval_sec: How often to run computation
            rvol_tod_days: Days for time-of-day RVOL calculation
            rvol_roll_window: Window for rolling RVOL calculation
        """
        self.resolution = resolution
        self.horizon_minutes = horizon_minutes
        self.waterline_delay_sec = waterline_delay_sec
        self.qscore_threshold = qscore_threshold
        self.poll_interval_sec = poll_interval_sec
        self.rvol_tod_days = rvol_tod_days
        self.rvol_roll_window = rvol_roll_window
        self.vix_enabled = str(os.getenv('VIX_ENABLED', str(config.get('vix.enabled', True)))).lower() in (
            "1", "true", "yes", "on"
        )
        self.vix_fallback = float(os.getenv('VIX_FALLBACK', config.get('vix.fallback', 18.0)))

        # Batch processing configuration to prevent memory overflow
        self.batch_size = config.get('feature_calculator.batch_size', 5)
        self.max_memory_per_query = config.get('feature_calculator.max_memory_per_query', 1073741824)  # 1GB default
        self.max_bytes_before_external = config.get('feature_calculator.max_bytes_before_external', 500_000_000)
        self.max_temporary_data_on_disk = config.get('feature_calculator.max_temporary_data_on_disk', 10_000_000_000)
        self.max_threads = config.get('feature_calculator.max_threads', 4)
        self.ch_send_receive_timeout_sec = float(
            config.get('feature_calculator.ch_send_receive_timeout_sec', 8.0)
        )
        self.ch_retry_max_attempts = max(
            1, int(config.get('feature_calculator.ch_retry_max_attempts', 3))
        )
        self.ch_retry_backoff_sec = max(
            0.0, float(config.get('feature_calculator.ch_retry_backoff_sec', 0.35))
        )
        self.ch_retry_backoff_max_sec = max(
            0.0, float(config.get('feature_calculator.ch_retry_backoff_max_sec', 2.0))
        )
        self.ch_concurrency = max(1, int(config.get('feature_calculator.ch_concurrency', 2)))
        self._ch_semaphore = asyncio.Semaphore(self.ch_concurrency)
        self._compute_failure_streak = 0
        self._max_backoff_multiplier = max(
            1, int(config.get('feature_calculator.max_backoff_multiplier', 12))
        )
        # Circuit breaker: after this many consecutive failures, pause SQL
        # execution and only run lightweight health checks until CH recovers.
        self._circuit_breaker_threshold = max(
            2, int(config.get('feature_calculator.circuit_breaker_threshold', 10))
        )
        self._circuit_breaker_probe_interval_sec = max(
            5.0, float(config.get('feature_calculator.circuit_breaker_probe_interval_sec', 30.0))
        )
        self._circuit_open = False
        self.symbols = []  # Will be loaded from config.universe in start()

        # Allow horizon_minutes override from config (critical for memory management)
        config_horizon = config.get('feature_calculator.horizon_minutes')
        if config_horizon:
            self.horizon_minutes = config_horizon
            logger.info("Overriding horizon_minutes from config",
                       original=horizon_minutes,
                       configured=config_horizon)

        # ClickHouse - will be initialized in start()
        self.ch = None

        # Table names and bar_sec mapping
        # Note: Only IBKR resolutions with backfill configured have _ibkr tables
        resolution_map = {
            "5s": ("bars_5s", "features_5s", 5),
            "10s": ("bars_10s", "features_10s", 10),
            "15s": ("bars_15s", "features_15s", 15),
            "1m": ("bars_1m_ibkr", "features_1m", 60),
            "5m": ("bars_5m_ibkr", "features_5m", 300),
            "10m": ("bars_10m_ibkr", "features_10m", 600),
            "15m": ("bars_15m_ibkr", "features_15m", 900),
            "30m": ("bars_30m_ibkr", "features_30m", 1800),
            "1h": ("bars_1h", "features_60m", 3600),
            "60m": ("bars_1h", "features_60m", 3600),
            "2h": ("bars_2h_ibkr", "features_2h", 7200),
            "3h": ("bars_3h_ibkr", "features_3h", 10800),
            "4h": ("bars_4h_ibkr", "features_4h", 14400),
            "1d": ("bars_1d_ibkr", "features_1d", 86400),
        }

        if resolution not in resolution_map:
            raise ValueError(f"Invalid resolution: {resolution}. Supported: {', '.join(resolution_map.keys())}")

        self.bar_table, self.feature_table, self.bar_sec = resolution_map[resolution]

        # Derived lags for VIX change calculations
        self.vix_lag_1m = max(1, 60 // self.bar_sec)
        self.vix_lag_5m = max(1, 300 // self.bar_sec)

        # Window parameters
        self.atr_window = 14
        self.rsi_window = 14

        self._running = False

        logger.info("FeatureCalculator initialized",
                    resolution=resolution,
                    horizon_min=horizon_minutes,
                    waterline_sec=waterline_delay_sec,
                    rvol_tod_days=rvol_tod_days,
                    rvol_roll_window=rvol_roll_window,
                    ch_timeout_sec=self.ch_send_receive_timeout_sec,
                    ch_retry_max_attempts=self.ch_retry_max_attempts,
                    ch_concurrency=self.ch_concurrency,
                    circuit_breaker_threshold=self._circuit_breaker_threshold,
                    circuit_breaker_probe_interval_sec=self._circuit_breaker_probe_interval_sec)

    @staticmethod
    def _is_retryable_ch_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        retry_markers = (
            "read timed out",
            "timed out",
            "timeout",
            "connection reset",
            "temporarily unavailable",
            "connection aborted",
            "connection refused",
            "httpconnectionpool",
        )
        return any(marker in msg for marker in retry_markers)

    async def _execute_ch_command_with_retry(
        self,
        sql: str,
        *,
        context: Dict[str, Any],
    ) -> None:
        """Execute a ClickHouse command with bounded retry on transient faults."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.ch_retry_max_attempts + 1):
            try:
                async with self._ch_semaphore:
                    await _execute_ch_command_async(
                        sql,
                        send_receive_timeout_sec=self.ch_send_receive_timeout_sec,
                    )
                if attempt > 1:
                    logger.info(
                        "Batch computation recovered after retry",
                        attempt=attempt,
                        **context,
                    )
                return
            except Exception as exc:
                last_exc = exc
                retryable = self._is_retryable_ch_error(exc)
                is_last = attempt >= self.ch_retry_max_attempts
                if (not retryable) or is_last:
                    raise
                sleep_sec = min(
                    self.ch_retry_backoff_sec * (2 ** (attempt - 1)),
                    self.ch_retry_backoff_max_sec,
                )
                logger.warning(
                    "Batch computation retry scheduled",
                    attempt=attempt,
                    next_sleep_sec=round(sleep_sec, 3),
                    error=str(exc),
                    **context,
                )
                if sleep_sec > 0:
                    await asyncio.sleep(sleep_sec)

        if last_exc is not None:
            raise last_exc

    async def start(self):
        """Start feature calculator"""
        # Initialize ClickHouse client using shared pool
        self.ch = ch_pool.get_client()

        # Load symbols from config universe
        self.symbols = config.get('universe', [])
        if not self.symbols:
            logger.warning("No symbols found in config.universe, feature calculator will process all symbols")

        self._running = True
        asyncio.create_task(self._compute_loop())
        logger.info("FeatureCalculator started",
                   resolution=self.resolution,
                   symbols_count=len(self.symbols),
                   batch_size=self.batch_size)

    async def stop(self):
        """Stop feature calculator"""
        self._running = False
        if self.ch:
            self.ch.close()
        logger.info("FeatureCalculator stopped")

    async def _ch_health_check(self) -> bool:
        """Lightweight SELECT 1 to probe whether ClickHouse is reachable."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                _ch_executor,
                lambda: ch_pool.get_client(connect_timeout=5.0).command("SELECT 1"),
            )
            return True
        except Exception as exc:
            logger.debug("CH health check failed", resolution=self.resolution, error=str(exc))
            return False

    async def _compute_loop(self):
        """Main computation loop with circuit breaker and jittered backoff."""
        while self._running:
            # ----------------------------------------------------------------
            # Circuit breaker: OPEN state — skip SQL, run health check only
            # ----------------------------------------------------------------
            if self._circuit_open:
                healthy = await self._ch_health_check()
                if healthy:
                    self._circuit_open = False
                    self._compute_failure_streak = 0
                    logger.warning(
                        "Feature calculator circuit breaker CLOSED (CH recovered)",
                        resolution=self.resolution,
                    )
                else:
                    await asyncio.sleep(self._circuit_breaker_probe_interval_sec)
                    continue

            # ----------------------------------------------------------------
            # Normal execution path
            # ----------------------------------------------------------------
            ok = False
            try:
                ok = await self._compute_features()
            except Exception as e:
                logger.error("Feature computation error", error=str(e))
                ok = False

            if ok:
                self._compute_failure_streak = 0
                await asyncio.sleep(self.poll_interval_sec)
            else:
                self._compute_failure_streak += 1

                # Trip the circuit breaker after threshold consecutive failures
                if self._compute_failure_streak >= self._circuit_breaker_threshold:
                    self._circuit_open = True
                    logger.error(
                        "Feature calculator circuit breaker OPEN",
                        resolution=self.resolution,
                        failure_streak=self._compute_failure_streak,
                        probe_interval_sec=self._circuit_breaker_probe_interval_sec,
                    )
                    await asyncio.sleep(self._circuit_breaker_probe_interval_sec)
                    continue

                # Exponential backoff with jitter to avoid thundering herd
                backoff_mult = min(self._compute_failure_streak, self._max_backoff_multiplier)
                base_sleep = float(self.poll_interval_sec) * float(backoff_mult)
                # Jitter: ±25 % of base sleep so multiple resolution calculators
                # don't hammer CH in lock-step after a shared outage.
                jitter = base_sleep * 0.25 * (2.0 * random.random() - 1.0)
                sleep_sec = max(self.poll_interval_sec, base_sleep + jitter)
                logger.warning(
                    "Feature calculator backoff active",
                    resolution=self.resolution,
                    failure_streak=self._compute_failure_streak,
                    sleep_sec=round(sleep_sec, 2),
                    circuit_breaker_threshold=self._circuit_breaker_threshold,
                )
                await asyncio.sleep(sleep_sec)

    @staticmethod
    def _chunk_symbols(symbols: List[str], batch_size: int) -> List[List[str]]:
        """
        Split symbols list into batches

        Args:
            symbols: List of symbols to split
            batch_size: Number of symbols per batch

        Returns:
            List of symbol batches
        """
        if batch_size <= 0 or not symbols:
            return [symbols] if symbols else []

        return [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]

    async def _compute_features(self) -> bool:
        """Compute features for recent bars in batches to prevent memory overflow"""
        start_time = time.time()

        try:
            # If no symbols configured, compute for all (backward compatibility)
            if not self.symbols:
                sql = self._build_feature_sql(symbols_batch=None)
                await self._execute_ch_command_with_retry(
                    sql,
                    context={
                        "resolution": self.resolution,
                        "batch": "all",
                        "symbols": [],
                    },
                )

                elapsed_ms = (time.time() - start_time) * 1000
                logger.debug("Features computed (all symbols)",
                            resolution=self.resolution,
                            elapsed_ms=round(elapsed_ms, 2))
                return True

            # Split symbols into batches
            symbol_batches = self._chunk_symbols(self.symbols, self.batch_size)
            total_batches = len(symbol_batches)
            successful_batches = 0
            failed_batches = 0

            logger.debug("Starting batch computation",
                        resolution=self.resolution,
                        total_symbols=len(self.symbols),
                        total_batches=total_batches,
                        batch_size=self.batch_size)

            # Process each batch
            for batch_idx, batch in enumerate(symbol_batches, 1):
                batch_start = time.time()
                try:
                    # Build SQL for this batch
                    sql = self._build_feature_sql(symbols_batch=batch)

                    # Execute computation asynchronously with bounded retry
                    await self._execute_ch_command_with_retry(
                        sql,
                        context={
                            "resolution": self.resolution,
                            "batch": f"{batch_idx}/{total_batches}",
                            "symbols": batch,
                        },
                    )

                    batch_elapsed_ms = (time.time() - batch_start) * 1000
                    successful_batches += 1

                    logger.debug("Batch computed successfully",
                                resolution=self.resolution,
                                batch=f"{batch_idx}/{total_batches}",
                                symbols=batch,
                                elapsed_ms=round(batch_elapsed_ms, 2))

                except Exception as e:
                    failed_batches += 1
                    logger.error("Batch computation failed",
                                resolution=self.resolution,
                                batch=f"{batch_idx}/{total_batches}",
                                symbols=batch,
                                error=str(e))
                    # Continue processing other batches even if one fails

            # Log overall summary
            total_elapsed_ms = (time.time() - start_time) * 1000
            logger.info("Batch computation completed",
                       resolution=self.resolution,
                       total_batches=total_batches,
                       successful=successful_batches,
                       failed=failed_batches,
                       total_elapsed_ms=round(total_elapsed_ms, 2))
            return failed_batches == 0

        except Exception as e:
            logger.error("Failed to compute features",
                        error=str(e),
                        resolution=self.resolution)
            return False

    def _build_vix_sql_parts(self, time_filter: str) -> Tuple[str, str]:
        """Return the CTE and select fragments used for VIX features."""
        vix_series_cte = ""
        join_clause = ""
        if self.vix_enabled:
            vix_series_cte = f"""
        , vix_series AS (
            SELECT
                toStartOfInterval(ts, INTERVAL {self.bar_sec} SECOND) AS ts_bucket,
                avg(vix) AS vix
            FROM refs_vix_ticks
            WHERE {time_filter}
            GROUP BY ts_bucket
        )
"""
            vix_expr = f"coalesce(vs.vix, {self.vix_fallback})"
            join_clause = "\n            LEFT JOIN vix_series vs ON vs.ts_bucket = obv_calc.ts_start"
        else:
            vix_expr = f"{self.vix_fallback}"

        vix_enriched_cte = f"""
        , vix_enriched AS (
            SELECT obv_calc.*,
                {vix_expr} AS vix_value,
                lagInFrame({vix_expr}, {self.vix_lag_1m}) OVER w_sym AS vix_prev_1m,
                lagInFrame({vix_expr}, {self.vix_lag_5m}) OVER w_sym AS vix_prev_5m,
                lagInFrame({vix_expr}, 1) OVER w_sym AS vix_prev_raw,
                if({vix_expr} > lagInFrame({vix_expr}, 1) OVER w_sym,
                   {vix_expr} - lagInFrame({vix_expr}, 1) OVER w_sym,
                   0.0) AS vix_gain,
                if({vix_expr} < lagInFrame({vix_expr}, 1) OVER w_sym,
                   lagInFrame({vix_expr}, 1) OVER w_sym - {vix_expr},
                   0.0) AS vix_loss
            FROM obv_calc{join_clause}
            WINDOW w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        )
"""

        vix_select = """
            vix_value AS vix,
            avg(vix_value) OVER w_20 AS vix_sma_20,
            if(stddevSamp(vix_value) OVER w_20 > 0,
               (vix_value - avg(vix_value) OVER w_20) / stddevSamp(vix_value) OVER w_20,
               NULL) AS vix_z_20,
            if(vix_prev_1m > 0,
               vix_value / vix_prev_1m - 1.0,
               NULL) AS vix_chg_1m,
            if(vix_prev_5m > 0,
               vix_value / vix_prev_5m - 1.0,
               NULL) AS vix_chg_5m,
            if(avg(vix_loss) OVER w_14 > 0,
               100.0 - (100.0 / (1.0 + (avg(vix_gain) OVER w_14 / avg(vix_loss) OVER w_14))),
               100.0) AS vix_rsi_14,
"""

        return vix_series_cte + vix_enriched_cte, vix_select

    def _build_feature_sql(self, symbols_batch: Optional[List[str]] = None) -> str:
        """
        Build comprehensive feature computation SQL with window functions

        Args:
            symbols_batch: Optional list of symbols to compute features for.
                          If None, computes for all symbols (backward compatibility)

        Key principles:
        - No look-ahead: uses lagInFrame for previous close
        - Watermark: only processes bars older than waterline_delay_sec
        - Horizon: only recomputes last horizon_minutes
        - Comprehensive indicators: BB, RSI, Stochastic, ADX, RVOL, Money Flow, etc.
        - Batched processing: processes subset of symbols to prevent memory overflow
        """
        columns = ",\n            ".join(_FEATURE_INSERT_COLUMNS)
        vix_cte, vix_columns = self._build_vix_sql_parts(
            time_filter=f"ts < now() - INTERVAL {self.waterline_delay_sec} SECOND\n              AND ts > now() - INTERVAL {self.horizon_minutes} MINUTE"
        )

        rth_filter = ""
        if self.bar_sec < 86400:
            rth_filter = """
              AND toHour(toTimeZone(ts_start, 'America/New_York')) * 60
                  + toMinute(toTimeZone(ts_start, 'America/New_York')) BETWEEN 570 AND 959"""

        # Add symbol filter if batch is provided
        symbol_filter = ""
        if symbols_batch:
            symbols_str = "', '".join(symbols_batch)
            symbol_filter = f"\n              AND symbol IN ('{symbols_str}')"

        sql = f"""
        INSERT INTO {self.feature_table} (
            {columns}
        )
        WITH
        -- Base bars
        filtered_bars AS (
            SELECT
                symbol, ts_start, ts_end,
                open, high, low, close, vwap,
                volume, trades,
                bid_last, ask_last, mid_last, ver
            FROM {self.bar_table} FINAL
            WHERE ts_end < now() - INTERVAL {self.waterline_delay_sec} SECOND
              AND ts_end > now() - INTERVAL {self.horizon_minutes} MINUTE
              {rth_filter}{symbol_filter}
        ),

        -- Price changes & previous close (for ATR, RSI, etc.)
        price_lags AS (
            SELECT *,
                lagInFrame(close, 1) OVER w_sym AS prev_close,
                lagInFrame(close, 5) OVER w_sym AS prev_close_5
            FROM filtered_bars
            WINDOW w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        ),

        -- RSI components (up/down gains)
        rsi_components AS (
            SELECT *,
                if(close > prev_close, close - prev_close, 0.0) AS gain,
                if(close < prev_close, prev_close - close, 0.0) AS loss
            FROM price_lags
        ),

        -- Stochastic components
        stoch_components AS (
            SELECT *,
                min(low) OVER w_14 AS ll_14,
                max(high) OVER w_14 AS hh_14
            FROM rsi_components
            WINDOW w_14 AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 13 PRECEDING AND CURRENT ROW)
        ),

        -- Money Flow components
        mfi_components AS (
            SELECT *,
                (high + low + close) / 3.0 AS typical_price,
                lagInFrame((high + low + close) / 3.0, 1) OVER w_sym AS prev_typ_px
            FROM stoch_components
            WINDOW w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        ),
        mfi_flows AS (
            SELECT *,
                typical_price * volume AS raw_money_flow,
                if(typical_price > prev_typ_px, typical_price * volume, 0.0) AS positive_flow,
                if(typical_price < prev_typ_px, typical_price * volume, 0.0) AS negative_flow
            FROM mfi_components
        ),

        -- ADX/DMI components
        adx_components AS (
            SELECT *,
                lagInFrame(high, 1) OVER w_sym AS prev_high,
                lagInFrame(low, 1) OVER w_sym AS prev_low
            FROM mfi_flows
            WINDOW w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        ),
        dm_base AS (
            SELECT *,
                if(high - prev_high > prev_low - low AND high - prev_high > 0, high - prev_high, 0.0) AS plus_dm,
                if(prev_low - low > high - prev_high AND prev_low - low > 0, prev_low - low, 0.0) AS minus_dm,
                greatest(high - low, abs(high - prev_close), abs(low - prev_close)) AS tr
            FROM adx_components
        ),
        dm_calc AS (
            SELECT *,
                avg(plus_dm) OVER w_14 AS avg_plus_dm_14,
                avg(minus_dm) OVER w_14 AS avg_minus_dm_14,
                avg(tr) OVER w_14 AS avg_tr_14
            FROM dm_base
            WINDOW w_14 AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 13 PRECEDING AND CURRENT ROW)
        ),

        -- VWAP (intraday cumulative)
        vwap_calc AS (
            SELECT *,
                sum(typical_price * volume) OVER w_day / sum(volume) OVER w_day AS vwap_intraday,
                row_number() OVER w_sym AS row_idx
            FROM dm_calc
            WINDOW
                w_day AS (PARTITION BY symbol, toDate(ts_start) ORDER BY ts_start),
                w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        ),

        -- OBV (On-Balance Volume) and log return calculation
        obv_calc AS (
            SELECT *,
                if(close > prev_close, volume,
                   if(close < prev_close, -volume, 0.0)) AS obv_delta,
                if(prev_close > 0, ln(close / prev_close), NULL) AS log_ret
            FROM vwap_calc
        ){vix_cte}

        -- Standardized values for skewness/kurtosis and lags for autocorr (to avoid nested window functions)
        , standardized AS (
            SELECT vix_enriched.*,
                -- Pre-compute means and stddevs for statistical features
                avg(log_ret) OVER w_20 AS mean_ret_20,
                stddevSamp(log_ret) OVER w_20 AS std_ret_20,
                avg(log_ret) OVER w_60 AS mean_ret_60,
                stddevSamp(log_ret) OVER w_60 AS std_ret_60,
                -- Pre-compute lags for autocorrelation
                lagInFrame(log_ret, 1) OVER w_sym AS lag_ret_1,
                lagInFrame(log_ret, 5) OVER w_sym AS lag_ret_5
            FROM vix_enriched
            WINDOW
                w_sym AS (PARTITION BY symbol ORDER BY ts_start),
                w_20 AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
                w_60 AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
        )

        SELECT
            -- Position 1-4: Core identifiers and prices
            symbol,                                                                    -- 1
            ts_start AS ts,                                                           -- 2
            close,                                                                     -- 3
            vwap,                                                                      -- 4

            -- Position 5-17: Legacy/placeholder columns
            avg_tr_14 AS atr_14,                                                      -- 5
            NULL AS atr_5,                                                             -- 6 (legacy)
            NULL AS ema_10,                                                            -- 7 (legacy)
            NULL AS ema_20,                                                            -- 8 (legacy)
            NULL AS ema_50,                                                            -- 9 (legacy)
            volume,                                                                    -- 10
            NULL AS vol_ema_20,                                                        -- 11 (legacy)
            NULL AS vol_spike,                                                         -- 12 (legacy)
            NULL AS z_price_20,                                                        -- 13 (legacy)
            NULL AS z_vol_20,                                                          -- 14 (legacy)
            if(bid_last > 0 AND ask_last > 0 AND (bid_last + ask_last) / 2.0 > 0,
               ((ask_last - bid_last) / ((bid_last + ask_last) / 2.0)) * 10000.0,
               NULL) AS spread_bps,                                                   -- 15
            NULL AS slope_20,                                                          -- 16 (legacy)
            NULL AS r2_20,                                                             -- 17 (legacy)

            -- Position 18-19: Metadata
            ver,                                                                       -- 18
            now() AS created_at,                                                      -- 19

            -- Position 20-24: Returns and momentum
            close - prev_close AS ret_1,                                              -- 20
            close - prev_close_5 AS ret_5,                                            -- 21
            log_ret,                                                                  -- 22 (pre-computed in obv_calc CTE)
            if(prev_close_5 > 0, (close / prev_close_5 - 1.0) * 100.0, NULL) AS roc_5, -- 23
            if(lagInFrame(close, 10) OVER w_sym > 0,
               (close / lagInFrame(close, 10) OVER w_sym - 1.0) * 100.0, NULL) AS roc_10, -- 24

            -- Position 25-30: Moving averages and volatility
            avg(close) OVER w_5 AS sma_5,                                             -- 25
            avg(close) OVER w_20 AS sma_20,                                           -- 26
            avg(close) OVER w_60 AS sma_60,                                           -- 27
            stddevSamp(close) OVER w_20 AS std_20,                                    -- 28
            stddevSamp(close) OVER w_60 AS std_60,                                    -- 29
            if(stddevSamp(close) OVER w_60 > 0,
               (close - avg(close) OVER w_60) / stddevSamp(close) OVER w_60, NULL) AS z_price_60, -- 30

            -- Position 31-35: Bollinger Bands
            avg(close) OVER w_20 AS bb_mid_20,                                        -- 31
            avg(close) OVER w_20 + 2.0 * stddevSamp(close) OVER w_20 AS bb_up_20,   -- 32
            avg(close) OVER w_20 - 2.0 * stddevSamp(close) OVER w_20 AS bb_dn_20,   -- 33
            if(avg(close) OVER w_20 > 0,
               (4.0 * stddevSamp(close) OVER w_20) / avg(close) OVER w_20, NULL) AS bb_bw_20, -- 34
            if(stddevSamp(close) OVER w_20 > 0,
               (close - (avg(close) OVER w_20 - 2.0 * stddevSamp(close) OVER w_20))
               / (4.0 * stddevSamp(close) OVER w_20), NULL) AS bb_pct_20,           -- 35

            -- Position 36-38: Stochastic and RSI
            if(hh_14 > ll_14, ((close - ll_14) / (hh_14 - ll_14)) * 100.0, NULL) AS stoch_k_14, -- 36
            avg(if(hh_14 > ll_14, ((close - ll_14) / (hh_14 - ll_14)) * 100.0, NULL)) OVER w_3 AS stoch_d_3, -- 37
            if(avg(loss) OVER w_14 > 0,
               100.0 - (100.0 / (1.0 + (avg(gain) OVER w_14 / avg(loss) OVER w_14))),
               100.0) AS rsi_14,                                                      -- 38

            -- Position 39-41: Volatility estimators
            stddevSamp(log_ret) OVER w_20 AS rv_20,                                   -- 39
            sqrt((1.0 / (4.0 * ln(2.0))) * avg(pow(ln(high / low), 2)) OVER w_20) AS parkinson_20, -- 40
            sqrt(0.5 * avg(pow(ln(high / low), 2)) OVER w_20
                 - (2.0 * ln(2.0) - 1.0) * avg(pow(ln(close / open), 2)) OVER w_20) AS garman_klass_20, -- 41

            -- Position 42-43: Donchian Channel
            max(high) OVER w_20 AS donchian_hi_20,                                    -- 42
            min(low) OVER w_20 AS donchian_lo_20,                                     -- 43

            -- Position 44-47: ADX/DMI
            if(avg_tr_14 > 0,
               (avg_plus_dm_14 / avg_tr_14) * 100.0, NULL) AS di_plus_14,           -- 44
            if(avg_tr_14 > 0,
               (avg_minus_dm_14 / avg_tr_14) * 100.0, NULL) AS di_minus_14,         -- 45
            if(avg_plus_dm_14 + avg_minus_dm_14 > 0,
               abs(avg_plus_dm_14 - avg_minus_dm_14)
               / (avg_plus_dm_14 + avg_minus_dm_14) * 100.0, NULL) AS dx_14,        -- 46
            avg(if(avg_plus_dm_14 + avg_minus_dm_14 > 0,
                   abs(avg_plus_dm_14 - avg_minus_dm_14)
                   / (avg_plus_dm_14 + avg_minus_dm_14) * 100.0, NULL)) OVER w_14 AS adx_14, -- 47

            -- Position 48-50: Volume indicators
            avg(volume) OVER w_20 AS vol_avg_20,                                      -- 48
            if(avg(volume) OVER w_20 > 0, volume / avg(volume) OVER w_20, NULL) AS rvol_roll_20, -- 49
            if(avgIf(volume,
                    toHour(ts_start) * 60 + toMinute(ts_start)
                    = toHour(CAST(ts_start AS DateTime)) * 60 + toMinute(CAST(ts_start AS DateTime)))
               OVER w_tod > 0,
               volume / avgIf(volume,
                             toHour(ts_start) * 60 + toMinute(ts_start)
                             = toHour(CAST(ts_start AS DateTime)) * 60 + toMinute(CAST(ts_start AS DateTime)))
                        OVER w_tod, NULL) AS rvol_tod_20d,                            -- 50

            -- Position 51-53: Money flow
            sum((close - open) / (high - low + 0.0001) * volume) OVER w_20
            / sum(volume) OVER w_20 AS cmf_20,                                        -- 51
            if(sum(positive_flow) OVER w_14 + sum(negative_flow) OVER w_14 > 0,
               100.0 - (100.0 / (1.0 + (sum(positive_flow) OVER w_14 / sum(negative_flow) OVER w_14))),
               50.0) AS mfi_14,                                                       -- 52
            sum(obv_delta) OVER w_60 AS obv_60,                                       -- 53

            -- Position 54-56: VWAP
            vwap_intraday AS vwap_day,                                                -- 54
            if(vwap_intraday > 0, ((close - vwap_intraday) / vwap_intraday) * 10000.0, NULL) AS vwap_dev_bps, -- 55
            if(stddevSamp(row_idx) OVER w_20 > 0,
               covarSamp(toFloat64(row_idx), vwap_intraday) OVER w_20
               / pow(stddevSamp(row_idx) OVER w_20, 2), NULL) AS vwap_slope_20,     -- 56

            -- Position 57-58: Microstructure
            if(bid_last > 0 AND ask_last > 0,
               (bid_last + ask_last) / 2.0, NULL) AS micro_mid,                      -- 57
            if(bid_last > 0 AND ask_last > 0,
               ((close - (bid_last + ask_last) / 2.0) / ((bid_last + ask_last) / 2.0)) * 10000.0,
               NULL) AS close_to_mid_bps,                                             -- 58

            -- Position 59-63: Candlestick
            close - open AS body,                                                      -- 59
            high - greatest(open, close) AS upper_wick,                               -- 60
            least(open, close) - low AS lower_wick,                                   -- 61
            high - low AS range,                                                       -- 62
            if(close > 0, ((high - low) / close) * 100.0, NULL) AS range_pct,        -- 63

            -- Position 64-65: Time features
            {self.bar_sec} AS bar_sec,                                                -- 64
            toHour(ts_start) * 60 + toMinute(ts_start) AS minute_of_day,             -- 65

            -- Position 66-70: OHLC and trades
            open,                                                                      -- 66
            high,                                                                      -- 67
            low,                                                                       -- 68
            vwap AS vwap_bar,                                                         -- 69
            trades,                                                                    -- 70

            -- Position 71-76: VIX features
            vix_value AS vix,                                                         -- 71
            avg(vix_value) OVER w_20 AS vix_sma_20,                                  -- 72
            if(stddevSamp(vix_value) OVER w_20 > 0,
               (vix_value - avg(vix_value) OVER w_20) / stddevSamp(vix_value) OVER w_20,
               NULL) AS vix_z_20,                                                     -- 73
            if(vix_prev_1m > 0,
               vix_value / vix_prev_1m - 1.0,
               NULL) AS vix_chg_1m,                                                   -- 74
            if(vix_prev_5m > 0,
               vix_value / vix_prev_5m - 1.0,
               NULL) AS vix_chg_5m,                                                   -- 75
            if(avg(vix_loss) OVER w_14 > 0,
               100.0 - (100.0 / (1.0 + (avg(vix_gain) OVER w_14 / avg(vix_loss) OVER w_14))),
               100.0) AS vix_rsi_14,                                                  -- 76

            -- Position 77-80: NEW Tier-1 EMA features
            -- Using SMA as approximation (Click House exponentialMovingAverage not available in this version)
            avg(close) OVER (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) AS ema_9,    -- 77
            avg(close) OVER (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS ema_21,  -- 78
            avg(close) OVER (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) -
                avg(close) OVER (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS ema_cross, -- 79
            vix_value - avg(vix_value) OVER w_20 AS vix_diff,                        -- 80

            -- Position 81: NEW Tier-1 ATR
            avg(tr) OVER w_20 AS atr_20,                                              -- 81

            -- Position 82-90: NEW Tier-1 Market context (SPY/QQQ) - NULL for Phase 1
            NULL AS spy_ret_1,                                                         -- 82
            NULL AS spy_ret_5,                                                         -- 83
            NULL AS qqq_ret_1,                                                         -- 84
            NULL AS qqq_ret_5,                                                         -- 85
            NULL AS beta_spy_20,                                                       -- 86
            NULL AS beta_spy_60,                                                       -- 87
            NULL AS corr_spy_20,                                                       -- 88
            NULL AS corr_spy_60,                                                       -- 89
            NULL AS corr_qqq_20,                                                       -- 90

            -- Position 91-94: NEW Tier-1 Microstructure
            NULL AS tick_imbalance,                                                    -- 91 (requires tick data)
            if(avg(toFloat64(trades)) OVER w_20 > 0,
               toFloat64(trades) / avg(toFloat64(trades)) OVER w_20,
               NULL) AS trade_count_ratio,                                            -- 92
            if(trades > 0, volume / toFloat64(trades), NULL) AS avg_trade_size,      -- 93
            NULL AS price_impact_5m,                                                   -- 94 (requires orderbook)

            -- Position 95-105: NEW Tier-1 Statistical features
            if(stddevSamp(close) OVER w_20 > 0,
               (close - avg(close) OVER w_20) / stddevSamp(close) OVER w_20,
               NULL) AS zscore_close_20,                                              -- 95
            if(stddevSamp(close) OVER w_60 > 0,
               (close - avg(close) OVER w_60) / stddevSamp(close) OVER w_60,
               NULL) AS zscore_close_60,                                              -- 96
            if(stddevSamp(volume) OVER w_20 > 0,
               (volume - avg(volume) OVER w_20) / stddevSamp(volume) OVER w_20,
               NULL) AS zscore_volume_20,                                             -- 97
            if(stddevSamp(volume) OVER w_60 > 0,
               (volume - avg(volume) OVER w_60) / stddevSamp(volume) OVER w_60,
               NULL) AS zscore_volume_60,                                             -- 98
            -- Skew/kurt using pre-computed standardized values
            if(std_ret_20 > 0,
               avg(pow((log_ret - mean_ret_20) / std_ret_20, 3)) OVER w_20,
               NULL) AS skew_20,                                                      -- 99
            if(std_ret_60 > 0,
               avg(pow((log_ret - mean_ret_60) / std_ret_60, 3)) OVER w_60,
               NULL) AS skew_60,                                                      -- 100
            if(std_ret_20 > 0,
               avg(pow((log_ret - mean_ret_20) / std_ret_20, 4)) OVER w_20 - 3.0,
               NULL) AS kurt_20,                                                      -- 101
            if(std_ret_60 > 0,
               avg(pow((log_ret - mean_ret_60) / std_ret_60, 4)) OVER w_60 - 3.0,
               NULL) AS kurt_60,                                                      -- 102
            corr(log_ret, lag_ret_1) OVER w_20 AS autocorr_ret_1, -- 103
            corr(log_ret, lag_ret_5) OVER w_20 AS autocorr_ret_5, -- 104
            NULL AS hurst_20,                                                          -- 105 (requires custom UDF)

            -- Position 106-110: NEW Tier-1 Temporal/meta features
            if((toHour(ts_start) * 60 + toMinute(ts_start)) >= 570,
               (toHour(ts_start) * 60 + toMinute(ts_start)) - 570, 0) AS time_since_open, -- 106
            toDayOfWeek(toTimeZone(ts_start, 'America/New_York')) - 1 AS day_of_week, -- 107
            toHour(toTimeZone(ts_start, 'America/New_York')) AS hour_of_day,         -- 108
            if((toHour(ts_start) * 60 + toMinute(ts_start)) >= 570 AND
               (toHour(ts_start) * 60 + toMinute(ts_start)) < 600, 1, 0) AS is_first_30min, -- 109
            if((toHour(ts_start) * 60 + toMinute(ts_start)) >= 930 AND
               (toHour(ts_start) * 60 + toMinute(ts_start)) < 960, 1, 0) AS is_last_30min, -- 110

            -- Position 111-112: NEW Tier-1 Advanced features
            NULL AS kama_10,                                                           -- 111 (requires custom UDF)
            NULL AS entropy_20                                                         -- 112 (requires custom UDF)

        FROM standardized

        -- Window definitions
        WINDOW
            w_sym  AS (PARTITION BY symbol ORDER BY ts_start),
            w_3    AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
            w_5    AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
            w_14   AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 13 PRECEDING AND CURRENT ROW),
            w_20   AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
            w_60   AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
            w_day  AS (PARTITION BY symbol, toDate(ts_start) ORDER BY ts_start),
            w_tod  AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN {self.rvol_tod_days * 288} PRECEDING AND CURRENT ROW)

        SETTINGS max_threads={self.max_threads},
                 max_memory_usage={self.max_memory_per_query},
                 max_bytes_before_external_group_by={self.max_bytes_before_external},
                 max_bytes_before_external_sort={self.max_bytes_before_external},
                 max_temporary_data_on_disk_size_for_query={self.max_temporary_data_on_disk},
                 max_execution_time=60
        """

        return sql

    async def compute_features_for_range(
        self,
        start_time: datetime,
        end_time: datetime,
        symbols: Optional[List[str]] = None
    ):
        """
        Compute features for a specific time range (for backfill)

        Args:
            start_time: Start of range
            end_time: End of range
            symbols: Optional list of symbols (all if None)
        """
        try:
            # Build modified SQL with custom time range (already formatted with parameters)
            sql = self._build_feature_sql_with_range(start_time, end_time, symbols)

            # Execute asynchronously with the same retry envelope used by live loop
            await self._execute_ch_command_with_retry(
                sql,
                context={
                    "resolution": self.resolution,
                    "batch": "range_backfill",
                    "symbols": symbols or [],
                },
            )

            logger.info("Backfill features computed",
                       resolution=self.resolution,
                       start=start_time,
                       end=end_time,
                       symbols=symbols)

        except Exception as e:
            logger.error("Failed to compute backfill features",
                        error=str(e),
                        start=start_time,
                        end=end_time)

    def _build_feature_sql_with_range(
        self,
        start_time: datetime,
        end_time: datetime,
        symbols: Optional[List[str]]
    ) -> str:
        """
        Build comprehensive feature SQL with custom time range for backfill

        Args:
            start_time: Start of time range
            end_time: End of time range
            symbols: Optional list of symbols to filter. If provided, processes only these symbols
        """

        symbol_filter = ""
        if symbols:
            symbol_list = "','".join(symbols)
            symbol_filter = f"\n              AND symbol IN ('{symbol_list}')"

        # Format datetime values for SQL
        start_str = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_str = end_time.strftime('%Y-%m-%d %H:%M:%S')

        columns = ",\n            ".join(_FEATURE_INSERT_COLUMNS)
        vix_cte, vix_columns = self._build_vix_sql_parts(
            time_filter=f"ts >= '{start_str}'\n              AND ts < '{end_str}'"
        )

        rth_filter = ""
        if self.bar_sec < 86400:
            rth_filter = """
              AND toHour(toTimeZone(ts_start, 'America/New_York')) * 60
                  + toMinute(toTimeZone(ts_start, 'America/New_York')) BETWEEN 570 AND 959"""

        sql = f"""
        INSERT INTO {self.feature_table} (
            {columns}
        )
        WITH
        filtered_bars AS (
            SELECT
                symbol, ts_start, ts_end,
                open, high, low, close, vwap,
                volume, trades,
                bid_last, ask_last, mid_last, ver
            FROM {self.bar_table} FINAL
            WHERE ts_end >= '{start_str}'
              AND ts_end < '{end_str}'
              {rth_filter}{symbol_filter}
        ),
        price_lags AS (
            SELECT *,
                lagInFrame(close, 1) OVER w_sym AS prev_close,
                lagInFrame(close, 5) OVER w_sym AS prev_close_5
            FROM filtered_bars
            WINDOW w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        ),
        rsi_components AS (
            SELECT *,
                if(close > prev_close, close - prev_close, 0.0) AS gain,
                if(close < prev_close, prev_close - close, 0.0) AS loss
            FROM price_lags
        ),
        stoch_components AS (
            SELECT *,
                min(low) OVER w_14 AS ll_14,
                max(high) OVER w_14 AS hh_14
            FROM rsi_components
            WINDOW w_14 AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 13 PRECEDING AND CURRENT ROW)
        ),
        mfi_components AS (
            SELECT *,
                (high + low + close) / 3.0 AS typical_price,
                lagInFrame((high + low + close) / 3.0, 1) OVER w_sym AS prev_typ_px
            FROM stoch_components
            WINDOW w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        ),
        mfi_flows AS (
            SELECT *,
                typical_price * volume AS raw_money_flow,
                if(typical_price > prev_typ_px, typical_price * volume, 0.0) AS positive_flow,
                if(typical_price < prev_typ_px, typical_price * volume, 0.0) AS negative_flow
            FROM mfi_components
        ),
        adx_components AS (
            SELECT *,
                lagInFrame(high, 1) OVER w_sym AS prev_high,
                lagInFrame(low, 1) OVER w_sym AS prev_low
            FROM mfi_flows
            WINDOW w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        ),
        dm_base AS (
            SELECT *,
                if(high - prev_high > prev_low - low AND high - prev_high > 0, high - prev_high, 0.0) AS plus_dm,
                if(prev_low - low > high - prev_high AND prev_low - low > 0, prev_low - low, 0.0) AS minus_dm,
                greatest(high - low, abs(high - prev_close), abs(low - prev_close)) AS tr
            FROM adx_components
        ),
        dm_calc AS (
            SELECT *,
                avg(plus_dm) OVER w_14 AS avg_plus_dm_14,
                avg(minus_dm) OVER w_14 AS avg_minus_dm_14,
                avg(tr) OVER w_14 AS avg_tr_14
            FROM dm_base
            WINDOW w_14 AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 13 PRECEDING AND CURRENT ROW)
        ),
        vwap_calc AS (
            SELECT *,
                sum(typical_price * volume) OVER w_day / sum(volume) OVER w_day AS vwap_intraday,
                row_number() OVER w_sym AS row_idx
            FROM dm_calc
            WINDOW
                w_day AS (PARTITION BY symbol, toDate(ts_start) ORDER BY ts_start),
                w_sym AS (PARTITION BY symbol ORDER BY ts_start)
        ),
        -- OBV (On-Balance Volume) and log return calculation
        obv_calc AS (
            SELECT *,
                if(close > prev_close, volume,
                   if(close < prev_close, -volume, 0.0)) AS obv_delta,
                if(prev_close > 0, ln(close / prev_close), NULL) AS log_ret
            FROM vwap_calc
        ){vix_cte}

        -- Standardized values for skewness/kurtosis and lags for autocorr (to avoid nested window functions)
        , standardized AS (
            SELECT vix_enriched.*,
                -- Pre-compute means and stddevs for statistical features
                avg(log_ret) OVER w_20 AS mean_ret_20,
                stddevSamp(log_ret) OVER w_20 AS std_ret_20,
                avg(log_ret) OVER w_60 AS mean_ret_60,
                stddevSamp(log_ret) OVER w_60 AS std_ret_60,
                -- Pre-compute lags for autocorrelation
                lagInFrame(log_ret, 1) OVER w_sym AS lag_ret_1,
                lagInFrame(log_ret, 5) OVER w_sym AS lag_ret_5
            FROM vix_enriched
            WINDOW
                w_sym AS (PARTITION BY symbol ORDER BY ts_start),
                w_20 AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
                w_60 AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
        )

        SELECT
            -- Position 1-4: Core identifiers and prices
            symbol,                                                                    -- 1
            ts_start AS ts,                                                           -- 2
            close,                                                                     -- 3
            vwap,                                                                      -- 4

            -- Position 5-17: Legacy/placeholder columns
            avg_tr_14 AS atr_14,                                                      -- 5
            NULL AS atr_5,                                                             -- 6 (legacy)
            NULL AS ema_10,                                                            -- 7 (legacy)
            NULL AS ema_20,                                                            -- 8 (legacy)
            NULL AS ema_50,                                                            -- 9 (legacy)
            volume,                                                                    -- 10
            NULL AS vol_ema_20,                                                        -- 11 (legacy)
            NULL AS vol_spike,                                                         -- 12 (legacy)
            NULL AS z_price_20,                                                        -- 13 (legacy)
            NULL AS z_vol_20,                                                          -- 14 (legacy)
            if(bid_last > 0 AND ask_last > 0 AND (bid_last + ask_last) / 2.0 > 0,
               ((ask_last - bid_last) / ((bid_last + ask_last) / 2.0)) * 10000.0,
               NULL) AS spread_bps,                                                   -- 15
            NULL AS slope_20,                                                          -- 16 (legacy)
            NULL AS r2_20,                                                             -- 17 (legacy)

            -- Position 18-19: Metadata
            ver,                                                                       -- 18
            now() AS created_at,                                                      -- 19

            -- Position 20-24: Returns and momentum
            close - prev_close AS ret_1,                                              -- 20
            close - prev_close_5 AS ret_5,                                            -- 21
            log_ret,                                                                  -- 22 (pre-computed in obv_calc CTE)
            if(prev_close_5 > 0, (close / prev_close_5 - 1.0) * 100.0, NULL) AS roc_5, -- 23
            if(lagInFrame(close, 10) OVER w_sym > 0,
               (close / lagInFrame(close, 10) OVER w_sym - 1.0) * 100.0, NULL) AS roc_10, -- 24

            -- Position 25-30: Moving averages and volatility
            avg(close) OVER w_5 AS sma_5,                                             -- 25
            avg(close) OVER w_20 AS sma_20,                                           -- 26
            avg(close) OVER w_60 AS sma_60,                                           -- 27
            stddevSamp(close) OVER w_20 AS std_20,                                    -- 28
            stddevSamp(close) OVER w_60 AS std_60,                                    -- 29
            if(stddevSamp(close) OVER w_60 > 0,
               (close - avg(close) OVER w_60) / stddevSamp(close) OVER w_60, NULL) AS z_price_60, -- 30

            -- Position 31-35: Bollinger Bands
            avg(close) OVER w_20 AS bb_mid_20,                                        -- 31
            avg(close) OVER w_20 + 2.0 * stddevSamp(close) OVER w_20 AS bb_up_20,   -- 32
            avg(close) OVER w_20 - 2.0 * stddevSamp(close) OVER w_20 AS bb_dn_20,   -- 33
            if(avg(close) OVER w_20 > 0,
               (4.0 * stddevSamp(close) OVER w_20) / avg(close) OVER w_20, NULL) AS bb_bw_20, -- 34
            if(stddevSamp(close) OVER w_20 > 0,
               (close - (avg(close) OVER w_20 - 2.0 * stddevSamp(close) OVER w_20))
               / (4.0 * stddevSamp(close) OVER w_20), NULL) AS bb_pct_20,           -- 35

            -- Position 36-38: Stochastic and RSI
            if(hh_14 > ll_14, ((close - ll_14) / (hh_14 - ll_14)) * 100.0, NULL) AS stoch_k_14, -- 36
            avg(if(hh_14 > ll_14, ((close - ll_14) / (hh_14 - ll_14)) * 100.0, NULL)) OVER w_3 AS stoch_d_3, -- 37
            if(avg(loss) OVER w_14 > 0,
               100.0 - (100.0 / (1.0 + (avg(gain) OVER w_14 / avg(loss) OVER w_14))),
               100.0) AS rsi_14,                                                      -- 38

            -- Position 39-41: Volatility estimators
            stddevSamp(log_ret) OVER w_20 AS rv_20,                                   -- 39
            sqrt((1.0 / (4.0 * ln(2.0))) * avg(pow(ln(high / low), 2)) OVER w_20) AS parkinson_20, -- 40
            sqrt(0.5 * avg(pow(ln(high / low), 2)) OVER w_20
                 - (2.0 * ln(2.0) - 1.0) * avg(pow(ln(close / open), 2)) OVER w_20) AS garman_klass_20, -- 41

            -- Position 42-43: Donchian Channel
            max(high) OVER w_20 AS donchian_hi_20,                                    -- 42
            min(low) OVER w_20 AS donchian_lo_20,                                     -- 43

            -- Position 44-47: ADX/DMI
            if(avg_tr_14 > 0,
               (avg_plus_dm_14 / avg_tr_14) * 100.0, NULL) AS di_plus_14,           -- 44
            if(avg_tr_14 > 0,
               (avg_minus_dm_14 / avg_tr_14) * 100.0, NULL) AS di_minus_14,         -- 45
            if(avg_plus_dm_14 + avg_minus_dm_14 > 0,
               abs(avg_plus_dm_14 - avg_minus_dm_14)
               / (avg_plus_dm_14 + avg_minus_dm_14) * 100.0, NULL) AS dx_14,        -- 46
            avg(if(avg_plus_dm_14 + avg_minus_dm_14 > 0,
                   abs(avg_plus_dm_14 - avg_minus_dm_14)
                   / (avg_plus_dm_14 + avg_minus_dm_14) * 100.0, NULL)) OVER w_14 AS adx_14, -- 47

            -- Position 48-50: Volume indicators
            avg(volume) OVER w_20 AS vol_avg_20,                                      -- 48
            if(avg(volume) OVER w_20 > 0, volume / avg(volume) OVER w_20, NULL) AS rvol_roll_20, -- 49
            if(avgIf(volume,
                    toHour(ts_start) * 60 + toMinute(ts_start)
                    = toHour(CAST(ts_start AS DateTime)) * 60 + toMinute(CAST(ts_start AS DateTime)))
               OVER w_tod > 0,
               volume / avgIf(volume,
                             toHour(ts_start) * 60 + toMinute(ts_start)
                             = toHour(CAST(ts_start AS DateTime)) * 60 + toMinute(CAST(ts_start AS DateTime)))
                        OVER w_tod, NULL) AS rvol_tod_20d,                            -- 50

            -- Position 51-53: Money flow
            sum((close - open) / (high - low + 0.0001) * volume) OVER w_20
            / sum(volume) OVER w_20 AS cmf_20,                                        -- 51
            if(sum(positive_flow) OVER w_14 + sum(negative_flow) OVER w_14 > 0,
               100.0 - (100.0 / (1.0 + (sum(positive_flow) OVER w_14 / sum(negative_flow) OVER w_14))),
               50.0) AS mfi_14,                                                       -- 52
            sum(obv_delta) OVER w_60 AS obv_60,                                       -- 53

            -- Position 54-56: VWAP
            vwap_intraday AS vwap_day,                                                -- 54
            if(vwap_intraday > 0, ((close - vwap_intraday) / vwap_intraday) * 10000.0, NULL) AS vwap_dev_bps, -- 55
            if(stddevSamp(row_idx) OVER w_20 > 0,
               covarSamp(toFloat64(row_idx), vwap_intraday) OVER w_20
               / pow(stddevSamp(row_idx) OVER w_20, 2), NULL) AS vwap_slope_20,     -- 56

            -- Position 57-58: Microstructure
            if(bid_last > 0 AND ask_last > 0,
               (bid_last + ask_last) / 2.0, NULL) AS micro_mid,                      -- 57
            if(bid_last > 0 AND ask_last > 0,
               ((close - (bid_last + ask_last) / 2.0) / ((bid_last + ask_last) / 2.0)) * 10000.0,
               NULL) AS close_to_mid_bps,                                             -- 58

            -- Position 59-63: Candlestick
            close - open AS body,                                                      -- 59
            high - greatest(open, close) AS upper_wick,                               -- 60
            least(open, close) - low AS lower_wick,                                   -- 61
            high - low AS range,                                                       -- 62
            if(close > 0, ((high - low) / close) * 100.0, NULL) AS range_pct,        -- 63

            -- Position 64-65: Time features
            {self.bar_sec} AS bar_sec,                                                -- 64
            toHour(ts_start) * 60 + toMinute(ts_start) AS minute_of_day,             -- 65

            -- Position 66-70: OHLC and trades
            open,                                                                      -- 66
            high,                                                                      -- 67
            low,                                                                       -- 68
            vwap AS vwap_bar,                                                         -- 69
            trades,                                                                    -- 70

            -- Position 71-76: VIX features
            vix_value AS vix,                                                         -- 71
            avg(vix_value) OVER w_20 AS vix_sma_20,                                  -- 72
            if(stddevSamp(vix_value) OVER w_20 > 0,
               (vix_value - avg(vix_value) OVER w_20) / stddevSamp(vix_value) OVER w_20,
               NULL) AS vix_z_20,                                                     -- 73
            if(vix_prev_1m > 0,
               vix_value / vix_prev_1m - 1.0,
               NULL) AS vix_chg_1m,                                                   -- 74
            if(vix_prev_5m > 0,
               vix_value / vix_prev_5m - 1.0,
               NULL) AS vix_chg_5m,                                                   -- 75
            if(avg(vix_loss) OVER w_14 > 0,
               100.0 - (100.0 / (1.0 + (avg(vix_gain) OVER w_14 / avg(vix_loss) OVER w_14))),
               100.0) AS vix_rsi_14,                                                  -- 76

            -- Position 77-80: NEW Tier-1 EMA features
            -- Using SMA as approximation (Click House exponentialMovingAverage not available in this version)
            avg(close) OVER (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) AS ema_9,    -- 77
            avg(close) OVER (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS ema_21,  -- 78
            avg(close) OVER (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) -
                avg(close) OVER (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS ema_cross, -- 79
            vix_value - avg(vix_value) OVER w_20 AS vix_diff,                        -- 80

            -- Position 81: NEW Tier-1 ATR
            avg(tr) OVER w_20 AS atr_20,                                              -- 81

            -- Position 82-90: NEW Tier-1 Market context (SPY/QQQ) - NULL for Phase 1
            NULL AS spy_ret_1,                                                         -- 82
            NULL AS spy_ret_5,                                                         -- 83
            NULL AS qqq_ret_1,                                                         -- 84
            NULL AS qqq_ret_5,                                                         -- 85
            NULL AS beta_spy_20,                                                       -- 86
            NULL AS beta_spy_60,                                                       -- 87
            NULL AS corr_spy_20,                                                       -- 88
            NULL AS corr_spy_60,                                                       -- 89
            NULL AS corr_qqq_20,                                                       -- 90

            -- Position 91-94: NEW Tier-1 Microstructure
            NULL AS tick_imbalance,                                                    -- 91 (requires tick data)
            if(avg(toFloat64(trades)) OVER w_20 > 0,
               toFloat64(trades) / avg(toFloat64(trades)) OVER w_20,
               NULL) AS trade_count_ratio,                                            -- 92
            if(trades > 0, volume / toFloat64(trades), NULL) AS avg_trade_size,      -- 93
            NULL AS price_impact_5m,                                                   -- 94 (requires orderbook)

            -- Position 95-105: NEW Tier-1 Statistical features
            if(stddevSamp(close) OVER w_20 > 0,
               (close - avg(close) OVER w_20) / stddevSamp(close) OVER w_20,
               NULL) AS zscore_close_20,                                              -- 95
            if(stddevSamp(close) OVER w_60 > 0,
               (close - avg(close) OVER w_60) / stddevSamp(close) OVER w_60,
               NULL) AS zscore_close_60,                                              -- 96
            if(stddevSamp(volume) OVER w_20 > 0,
               (volume - avg(volume) OVER w_20) / stddevSamp(volume) OVER w_20,
               NULL) AS zscore_volume_20,                                             -- 97
            if(stddevSamp(volume) OVER w_60 > 0,
               (volume - avg(volume) OVER w_60) / stddevSamp(volume) OVER w_60,
               NULL) AS zscore_volume_60,                                             -- 98
            -- Skew/kurt using pre-computed standardized values
            if(std_ret_20 > 0,
               avg(pow((log_ret - mean_ret_20) / std_ret_20, 3)) OVER w_20,
               NULL) AS skew_20,                                                      -- 99
            if(std_ret_60 > 0,
               avg(pow((log_ret - mean_ret_60) / std_ret_60, 3)) OVER w_60,
               NULL) AS skew_60,                                                      -- 100
            if(std_ret_20 > 0,
               avg(pow((log_ret - mean_ret_20) / std_ret_20, 4)) OVER w_20 - 3.0,
               NULL) AS kurt_20,                                                      -- 101
            if(std_ret_60 > 0,
               avg(pow((log_ret - mean_ret_60) / std_ret_60, 4)) OVER w_60 - 3.0,
               NULL) AS kurt_60,                                                      -- 102
            corr(log_ret, lag_ret_1) OVER w_20 AS autocorr_ret_1, -- 103
            corr(log_ret, lag_ret_5) OVER w_20 AS autocorr_ret_5, -- 104
            NULL AS hurst_20,                                                          -- 105 (requires custom UDF)

            -- Position 106-110: NEW Tier-1 Temporal/meta features
            if((toHour(ts_start) * 60 + toMinute(ts_start)) >= 570,
               (toHour(ts_start) * 60 + toMinute(ts_start)) - 570, 0) AS time_since_open, -- 106
            toDayOfWeek(toTimeZone(ts_start, 'America/New_York')) - 1 AS day_of_week, -- 107
            toHour(toTimeZone(ts_start, 'America/New_York')) AS hour_of_day,         -- 108
            if((toHour(ts_start) * 60 + toMinute(ts_start)) >= 570 AND
               (toHour(ts_start) * 60 + toMinute(ts_start)) < 600, 1, 0) AS is_first_30min, -- 109
            if((toHour(ts_start) * 60 + toMinute(ts_start)) >= 930 AND
               (toHour(ts_start) * 60 + toMinute(ts_start)) < 960, 1, 0) AS is_last_30min, -- 110

            -- Position 111-112: NEW Tier-1 Advanced features
            NULL AS kama_10,                                                           -- 111 (requires custom UDF)
            NULL AS entropy_20                                                         -- 112 (requires custom UDF)

        FROM standardized
        WINDOW
            w_sym  AS (PARTITION BY symbol ORDER BY ts_start),
            w_3    AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
            w_5    AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
            w_14   AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 13 PRECEDING AND CURRENT ROW),
            w_20   AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
            w_60   AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
            w_day  AS (PARTITION BY symbol, toDate(ts_start) ORDER BY ts_start),
            w_tod  AS (PARTITION BY symbol ORDER BY ts_start ROWS BETWEEN {self.rvol_tod_days * 288} PRECEDING AND CURRENT ROW)
        SETTINGS max_threads={self.max_threads},
                 max_memory_usage={self.max_memory_per_query},
                 max_bytes_before_external_group_by={self.max_bytes_before_external},
                 max_bytes_before_external_sort={self.max_bytes_before_external},
                 max_temporary_data_on_disk_size_for_query={self.max_temporary_data_on_disk},
                 max_execution_time=60
        """

        # SQL already has datetime values formatted directly - no parameter substitution needed
        return sql

    def health_check(self) -> bool:
        """Check if feature calculator is healthy"""
        if not self.ch:
            return False
        try:
            result = self.ch.query("SELECT 1 AS health")
            return len(result) > 0 and result[0].get('health') == 1
        except Exception:
            return False
