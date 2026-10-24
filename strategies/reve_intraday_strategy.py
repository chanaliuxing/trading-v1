# strategies/reve_intraday_strategy.py
# --- RevE Intraday Strategy — Tick-true replay, tick-level ORB break detection, guardrails, protective FLAT LMT ---

import asyncio
import json
import math
import time
import uuid
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dt_time, date
from typing import Dict, Any, List, Optional, Tuple, Deque
import pytz
import numpy as np


# ---------- Framework contracts ----------
from services.strategy.strategy_base import (
    StrategyAlgorithm,
    StrategyContext,
    StrategyParams,
    DecisionEnvelope,
    OrderStyle,
    Freshness,
    make_decision_id,
)

from persistence.state_store import StateStore
from libs.logger import get_logger

# ---------- DB / config ----------
try:
    from libs.db.ch_pool import get_client as get_ch_client
    CLICKHOUSE_AVAILABLE = True
except ImportError as exc:
    logging.getLogger(__name__).error(
        "CRITICAL: Failed to import ClickHouse library (libs.db.ch_pool): %s. CH features disabled.",
        exc
    )
    CLICKHOUSE_AVAILABLE = False

CHClient = Any

if not CLICKHOUSE_AVAILABLE:
    def get_ch_client() -> Optional[CHClient]:
        return None

try:
    from libs.config import config as system_config
except ImportError:
    logging.getLogger(__name__).warning("Global config (libs.config) not found.")
    system_config = None

# ---------- Types ----------
try:
    from libs.cache.price_cache import PriceSnapshot
except ImportError:

    class PriceSnapshot:  # type: ignore
        bid: float
        ask: float
        last: float

# SFS 1s reader (Redis ring)
try:
    from libs.dal.sfs_facade import SFS1sReader
    SFS_AVAILABLE = True
except Exception:
    SFS1sReader = None  # type: ignore
    SFS_AVAILABLE = False

# Cost model (spread/slip/fee estimates for observability)
try:
    from libs.exec.cost import CostParams, entry_fill_est, exit_fill_est
    COST_AVAILABLE = True
except Exception:
    COST_AVAILABLE = False

MarketIndexState = Dict[str, Any]

# ---------- Constants ----------
PACIFIC_TZ = pytz.timezone("America/Vancouver")
UTC_TZ = pytz.utc
TICK_SIZE_DEFAULT = 0.01
ATR_PERIOD = 14
DEFAULT_EQUITY = 100000.0

# ---------- Helpers ----------

@dataclass
class BarInProgress:
    symbol: str
    timeframe: str  # '1m', '5m', '15m'
    start_time_utc: Optional[datetime] = None
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    volume: float = 0.0
    tick_count: int = 0
    last_tick_ts_utc: Optional[datetime] = None

    def update(self, price: float, size: float, ts_utc: datetime):
        if self.start_time_utc is None:
            self.start_time_utc = self._floor_time(ts_utc, self.timeframe)
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price
        self.volume += size
        self.tick_count += 1
        self.last_tick_ts_utc = ts_utc

    def crossed_boundary(self, current_ts_utc: datetime) -> bool:
        if self.start_time_utc is None:
            return False
        return current_ts_utc >= self.start_time_utc + self._get_timedelta(self.timeframe)

    def pop_completed_bar(self) -> Optional[Dict[str, Any]]:
        if self.start_time_utc is None or self.tick_count == 0:
            return None
        end_time_utc = self.start_time_utc + self._get_timedelta(self.timeframe)
        bar_end_time = (
            self.last_tick_ts_utc
            if self.last_tick_ts_utc and self.last_tick_ts_utc < end_time_utc
            else end_time_utc
        )
        bar_close_ts_for_decision = bar_end_time.replace(microsecond=0)
        bar = {
            "ts": self.start_time_utc,
            "ts_utc": self.start_time_utc,
            "end_ts_utc": end_time_utc,
            "bar_close_ts_utc": bar_close_ts_for_decision,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
        }
        # reset self to next bar shell — caller will re-init/update
        return bar

    @staticmethod
    def _floor_time(dt: datetime, timeframe: str) -> datetime:
        if not dt.tzinfo:
            dt = UTC_TZ.localize(dt)
        dt_utc = dt.astimezone(UTC_TZ)
        if timeframe == "1m":
            return dt_utc.replace(second=0, microsecond=0)
        if timeframe == "5m":
            return dt_utc.replace(minute=(dt_utc.minute // 5) * 5, second=0, microsecond=0)
        if timeframe == "15m":
            return dt_utc.replace(minute=(dt_utc.minute // 15) * 15, second=0, microsecond=0)
        if "s" in timeframe:
            total_seconds = int(timeframe.replace("s", ""))
            ts = dt_utc.timestamp()
            floor_ts = math.floor(ts / total_seconds) * total_seconds
            return datetime.fromtimestamp(floor_ts, tz=UTC_TZ)
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    @staticmethod
    def _get_timedelta(timeframe: str) -> timedelta:
        if timeframe == "1m":
            return timedelta(minutes=1)
        if timeframe == "5m":
            return timedelta(minutes=5)
        if timeframe == "15m":
            return timedelta(minutes=15)
        if "s" in timeframe:
            return timedelta(seconds=int(timeframe.replace("s", "")))
        raise ValueError(f"Unsupported timeframe: {timeframe}")


@dataclass
class ATRState:
    last_close: float = 0.0
    prev_atr: float = 0.0
    current_atr: float = 0.0
    period: int = ATR_PERIOD


@dataclass
class ORBTracker:
    # Opening range bounds
    orb5_high: Optional[float] = None
    orb5_low: Optional[float] = None
    orb15_high: Optional[float] = None
    orb15_low: Optional[float] = None
    start_time_pt: dt_time = dt_time(6, 30)
    orb5_end_pt: dt_time = dt_time(6, 35)
    orb15_end_pt: dt_time = dt_time(6, 45)

    # Break detection & recovery (tick-level aware)
    lowest_since_orb_break: Optional[float] = None
    orb_low_broken: bool = False
    orb_low_break_bar_start_utc: Optional[datetime] = None

    # pending (tick-level) during current 1m bar
    pending_break_bar_start_utc: Optional[datetime] = None
    pending_lowest_since_break: Optional[float] = None


@dataclass
class PositionState:
    entry_price: float
    qty: float
    entry_ts_utc: datetime
    sl_price: float
    tp1_price: Optional[float] = None
    tp1_hit: bool = False
    trail_atr_mult: float = 1.0
    max_close_since_entry: float = 0.0
    current_trail_price: float = 0.0
    strategy_tag: str = ""
    decision_id: str = ""


class RevEIntradayStrategy(StrategyAlgorithm):
    """
    RevE Intraday (Long-only):
    - Precise VWAP (Σ price*size / Σ size) on trade ticks.
    - Tick-true recovery: per-tick ORB break detection; replay uses the same indicator pipeline with replay gating.
    - Protective LMT for forced flatten; dynamic TP1; guardrails passed to RiskService via extras.
    """

    def __init__(self, ctx: StrategyContext, params: StrategyParams):
        super().__init__(ctx, params)
        # Some engine integrations expect params to hang off the context.
        # Ensure compatibility without touching shared framework code.
        if not hasattr(self.ctx, "params"):
            setattr(self.ctx, "params", params)
        self.strategy_id = self.ctx.strategy_id

        state_base_dir_cfg = system_config.get("persistence.state_store.base_dir") if system_config else None
        state_base_dir = state_base_dir_cfg or "/tmp/trading_state"
        self.state_store = StateStore(run_id=self.strategy_id, base_dir=state_base_dir)

        # Use structured logger wired to system sinks
        self.log = get_logger(f"strategy.{self.strategy_id}")
        try:
            self.log.info("REVE_LOGGER_READY", strategy_id=self.strategy_id)
        except Exception:
            pass

        # ---- defaults (overridden in _load_parameters) ----
        self.universe: List[str] = ["TSLA", "NVDA", "AAPL", "AMD", "META", "QQQ", "SPY"]
        self.risk_per_trade_pct: float = 0.0025
        self.risk_per_trade_pct_low: float = 0.0020
        self.daily_stop_pct: float = 0.012
        self.max_consecutive_losses: int = 3
        self.liquidity_max_spread: float = 0.05
        self.liquidity_min_vol_mult_1m: float = 0.7
        self.gating_p_up_uptrend: float = 0.70
        self.gating_p_up_downtrend: float = 0.30
        self.gating_trend_push_threshold: float = 0.60
        self.strat_A_absdev_win: int = 90
        self.strat_A_q_level: float = 0.80
        self.strat_A_ewma_alpha: float = 0.70
        self.strat_A_band_clip_low_atr: float = 0.60
        self.strat_A_band_clip_high_atr: float = 1.40
        self.strat_A_near_vwap_atr: float = 0.20
        self.strat_A_sl_atr: float = 0.50
        self.strat_A_tp1_atr: float = 1.00
        self.strat_A_trail_atr: float = 1.00
        self.strat_A_vol_collapse_mult: float = 0.80
        self.strat_B_k_quantile: float = 0.70
        self.strat_B_k_ewma_beta: float = 0.40
        self.strat_B_k_clip_low: float = 0.30
        self.strat_B_k_clip_high: float = 0.60
        self.strat_B_sl_atr5m: float = 0.25
        self.strat_B_trail_atr5m: float = 1.00
        self.exec_entry_buffer_atr: float = 0.02
        self.exec_cooldown_bars: int = 3
        self.exec_fee_per_share: float = 0.0035
        self.exec_slippage_bps_per_atr: float = 0.0
        self.adaptive_target_trades_low: int = 2
        self.adaptive_target_trades_high: int = 12
        self.adaptive_adjust_step_pct: float = 0.05
        self.schedule_open_pt: dt_time = dt_time(6, 30)
        self.schedule_stop_new_pt: dt_time = dt_time(12, 45)
        self.schedule_force_flat_pt: dt_time = dt_time(12, 59)
        self.save_state_interval_sec: int = 300

        # Guard
        self.guard_banddistdown_atr_min: float = 0.55
        self.guard_banddistdown_atr_max: float = 1.50
        self.guard_k_min: float = 0.25
        self.guard_k_max: float = 0.70

        # Internal
        self.symbol_states: Dict[str, Dict[str, Any]] = {}
        self.open_positions: Dict[str, List[PositionState]] = {}
        self.avg_trades_5d: float = (self.adaptive_target_trades_low + self.adaptive_target_trades_high) / 2
        self.trade_counts_last_5d: Deque[int] = deque(maxlen=5)
        self.trading_days_history: Deque[Dict[str, Any]] = deque(maxlen=10)
        self.last_closed_day_pt: Optional[str] = None
        self.consecutive_losses_today: int = 0
        self.daily_trade_count: int = 0
        self.realized_pnl_today: float = 0.0
        self.last_state_save_time: float = 0.0
        self.current_bar_index: int = 0  # used only for logging; logic uses bar start times for B'
        self.today_date_pt: Optional[date] = None

        # In-memory L1 fallback cache (from features)
        self._l1_last: Dict[str, Dict[str, Any]] = {}

        # ClickHouse client is resolved lazily at start of day
        self.ch: Optional["CHClient"] = None  # type: ignore

        self.rng_seed: int = int(self.ctx.param("rng_seed", 0))
        self._replay_mode: bool = False

        # SFS 1s reader for intra-bar microstructure (optional)
        self._sfs_reader: Optional[SFS1sReader] = SFS1sReader() if SFS_AVAILABLE else None
        # Use engine-driven 1m features instead of internal Kafka
        self.use_engine_features: bool = bool(self.ctx.param("rev.use_engine_features", True))
        # Debug gating logs (verbose). Set rev.debug_gating: true in config to enable.
        try:
            self.debug_gating: bool = bool(self.ctx.param("rev.debug_gating", True))
        except Exception:
            self.debug_gating = True
        try:
            self.debug_every_bar: bool = bool(self.ctx.param("rev.debug_every_bar", True))
        except Exception:
            self.debug_every_bar = True

    def _gate_log(self, stage: str, **kwargs) -> None:
        if not getattr(self, "debug_gating", False):
            return
        try:
            self.log.info("GATE_BLOCK", stage=stage, **kwargs)
        except Exception:
            pass

    def _ensure_clickhouse_client(self) -> None:
        if self.ch or not CLICKHOUSE_AVAILABLE:
            return
        try:
            client = get_ch_client()
            if client:
                self.ch = client  # type: ignore
            else:
                self.log.warning("get_ch_client() returned None.")
        except Exception as exc:
            self.log.error("Failed to obtain ClickHouse client", error=str(exc), exc_info=True)
            self.ch = None

    # ----------------- Init & daily reset -----------------

    def _load_parameters(self):
        self.universe = self.ctx.param("universe", []) or self.universe
        gp = self.ctx.param
        self.risk_per_trade_pct = gp("risk.per_trade_pct", self.risk_per_trade_pct)
        self.risk_per_trade_pct_low = gp("risk.per_trade_pct_low", self.risk_per_trade_pct_low)
        self.daily_stop_pct = gp("risk.daily_stop_pct", self.daily_stop_pct)
        self.max_consecutive_losses = gp("risk.max_consecutive_losses", self.max_consecutive_losses)
        self.liquidity_max_spread = gp("liquidity_filters.max_spread", self.liquidity_max_spread)
        self.liquidity_min_vol_mult_1m = gp("liquidity_filters.min_vol_mult_1m", self.liquidity_min_vol_mult_1m)
        self.gating_p_up_uptrend = gp("gating.p_up_uptrend", self.gating_p_up_uptrend)
        self.gating_p_up_downtrend = gp("gating.p_up_downtrend", self.gating_p_up_downtrend)
        self.gating_trend_push_threshold = gp("gating.trend_push_threshold", self.gating_trend_push_threshold)
        self.strat_A_absdev_win = gp("strategy_A.absdev_win", self.strat_A_absdev_win)
        self.strat_A_q_level = gp("strategy_A.q_level", self.strat_A_q_level)
        self.strat_A_ewma_alpha = gp("strategy_A.ewma_alpha", self.strat_A_ewma_alpha)
        self.strat_A_band_clip_low_atr = gp("strategy_A.band_clip_low_atr", self.strat_A_band_clip_low_atr)
        self.strat_A_band_clip_high_atr = gp("strategy_A.band_clip_high_atr", self.strat_A_band_clip_high_atr)
        self.strat_A_near_vwap_atr = gp("strategy_A.near_vwap_atr", self.strat_A_near_vwap_atr)
        self.strat_A_sl_atr = gp("strategy_A.sl_atr", self.strat_A_sl_atr)
        self.strat_A_tp1_atr = gp("strategy_A.tp1_atr", self.strat_A_tp1_atr)
        self.strat_A_trail_atr = gp("strategy_A.trail_atr", self.strat_A_trail_atr)
        self.strat_A_vol_collapse_mult = gp("strategy_A.vol_collapse_mult", self.strat_A_vol_collapse_mult)
        self.strat_B_k_quantile = gp("strategy_B.k_quantile", self.strat_B_k_quantile)
        self.strat_B_k_ewma_beta = gp("strategy_B.k_ewma_beta", self.strat_B_k_ewma_beta)
        self.strat_B_k_clip_low = gp("strategy_B.k_clip_low", self.strat_B_k_clip_low)
        self.strat_B_k_clip_high = gp("strategy_B.k_clip_high", self.strat_B_k_clip_high)
        self.strat_B_sl_atr5m = gp("strategy_B.sl_atr5m", self.strat_B_sl_atr5m)
        self.strat_B_trail_atr5m = gp("strategy_B.trail_atr5m", self.strat_B_trail_atr5m)
        self.exec_entry_buffer_atr = gp("execution_model.entry_buffer_atr", self.exec_entry_buffer_atr)
        self.exec_cooldown_bars = gp("execution_model.cooldown_bars_after_entry", self.exec_cooldown_bars)
        self.exec_fee_per_share = gp("execution_model.fee_per_share", self.exec_fee_per_share)
        self.exec_slippage_bps_per_atr = gp("execution_model.slippage_bps_per_atr", self.exec_slippage_bps_per_atr)
        self.adaptive_target_trades_low = gp("adaptive.target_trades_low", self.adaptive_target_trades_low)
        self.adaptive_target_trades_high = gp("adaptive.target_trades_high", self.adaptive_target_trades_high)
        self.adaptive_adjust_step_pct = gp("adaptive.adjust_step_pct", self.adaptive_adjust_step_pct)
        self.schedule_open_pt = self._parse_time_param("schedule_pt.open", "06:30:00")
        self.schedule_stop_new_pt = self._parse_time_param("schedule_pt.stop_new_positions", "12:45:00")
        self.schedule_force_flat_pt = self._parse_time_param("schedule_pt.force_flat", "12:59:00")
        try:
            self.log.info(
                "SCHEDULE_SET",
                open=str(self.schedule_open_pt),
                stop_new=str(self.schedule_stop_new_pt),
                force_flat=str(self.schedule_force_flat_pt),
            )
        except Exception:
            pass
        self.save_state_interval_sec = gp("state_persistence.interval_sec", self.save_state_interval_sec)
        self.guard_banddistdown_atr_min = gp("guardrail.banddistdown_atr_min", self.guard_banddistdown_atr_min)
        self.guard_banddistdown_atr_max = gp("guardrail.banddistdown_atr_max", self.guard_banddistdown_atr_max)
        self.guard_k_min = gp("guardrail.k_min", self.guard_k_min)
        self.guard_k_max = gp("guardrail.k_max", self.guard_k_max)

    def _parse_time_param(self, key: str, default: str) -> dt_time:
        time_str = self.ctx.param(key, None)
        # Support nested extras like schedule_pt: { stop_new_positions: "13:00:00" }
        if time_str is None and "." in key:
            base, sub = key.split(".", 1)
            nested = self.ctx.param(base, None)
            if isinstance(nested, dict):
                time_str = nested.get(sub)
        if time_str is None:
            time_str = default
        try:
            parts = str(time_str).split(":")
            if len(parts) == 2:
                time_str = f"{time_str}:00"
            return dt_time.fromisoformat(str(time_str))
        except (ValueError, TypeError):
            parts_def = default.split(":")
            if len(parts_def) == 2:
                default = f"{default}:00"
            return dt_time.fromisoformat(default)

    async def initialize(self) -> None:
        self.log.info(f"[{self.strategy_id}] Initializing RevE Intraday Strategy...")
        self._load_parameters()
        self.open_positions = {symbol: [] for symbol in self.universe}

        # Historical bootstrap
        initial_context: Dict[str, Dict[str, Any]] = {}
        tasks = {sym: asyncio.create_task(self._get_symbol_historical_context(sym)) for sym in self.universe}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for i, symbol in enumerate(self.universe):
            res = results[i]
            if isinstance(res, Exception):
                initial_context[symbol] = {
                    "prev_close": 0.0,
                    "prev_atr_1m": 0.1,
                    "prev_atr_5m": 0.5,
                    "median_vol_60m": 10000.0,
                }
            else:
                prev_close, prev_atr_1m, prev_atr_5m, median_vol = res
                initial_context[symbol] = {
                    "prev_close": prev_close,
                    "prev_atr_1m": prev_atr_1m or 0.1,
                    "prev_atr_5m": prev_atr_5m or 0.5,
                    "median_vol_60m": median_vol or 10000.0,
                }

        self._initialize_all_symbol_states(initial_context)

        # Restore snapshot & replay
        last_processed_ts_map: Dict[str, datetime] = {}
        try:
            snapshot = self.state_store.load()
            if snapshot:
                last_processed_ts_map = self._restore_persistent_state(snapshot)
                self.log.info(f"[{self.strategy_id}] Restored snapshot at {snapshot.get('save_ts_ms')}")
                if self.ch and last_processed_ts_map:
                    await self._replay_bars_for_recovery(last_processed_ts_map)
            else:
                self.trade_counts_last_5d.extend([0] * 5)
        except Exception:
            self.log.exception("Failed to load/restore snapshot.")
            self.trade_counts_last_5d.extend([0] * 5)

        # Subscribe to features (engine-driven)
        try:
            # Use 5s feed; on_features processes only on minute boundaries
            await self.ctx.subscribe_features(self.universe, bar_sec=5)
            self.log.info("Subscribed to 5s features via StrategyEngine (minute-boundary processing)")
        except Exception:
            self.log.exception("subscribe_features failed; strategy cannot proceed without engine-driven features")

        self.last_state_save_time = time.time()
        self.today_date_pt = datetime.now(PACIFIC_TZ).date()

        # Backfill ORB data from today's market open if we started late
        await self._backfill_orb_from_today()

        self.log.info(f"[{self.strategy_id}] Ready for {self.today_date_pt}.")

    async def on_start_of_day(self, dt: datetime) -> None:
        self.log.info("Executing on_start_of_day...", trading_day=str(dt.date()))
        had_client = self.ch is not None
        self._ensure_clickhouse_client()
        if not had_client:
            if self.ch:
                self.log.info("Successfully obtained ClickHouse client via get_client().")
            else:
                self.log.warning("Failed to get ClickHouse client via get_client() at start of day. Queries may fail.")

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        """Engine-driven 1m bar callback: build bar dict and feed core pipeline."""
        if symbol not in self.universe:
            return
        try:
            from zoneinfo import ZoneInfo
            NY_TZ = ZoneInfo("America/New_York")
            ts_val = features.get("ts") or features.get("ts_start") or features.get("ts_utc")
            if isinstance(ts_val, str):
                s = ts_val.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                ts_local = dt.astimezone(NY_TZ) if dt.tzinfo else dt.replace(tzinfo=NY_TZ)
            elif isinstance(ts_val, datetime):
                ts_local = ts_val.astimezone(NY_TZ) if ts_val.tzinfo else ts_val.replace(tzinfo=NY_TZ)
            else:
                ts_local = datetime.now(NY_TZ)

            # Update simple L1 snapshot cache from incoming features
            try:
                bid = features.get("bid")
                ask = features.get("ask")
                last = features.get("last") or features.get("close")
                if any(v is not None for v in (bid, ask, last)):
                    self._l1_last[symbol] = {
                        "bid": float(bid) if isinstance(bid, (int, float)) else None,
                        "ask": float(ask) if isinstance(ask, (int, float)) else None,
                        "last": float(last) if isinstance(last, (int, float)) else None,
                    }
            except Exception:
                pass

            minute_start_local = ts_local.replace(second=0, microsecond=0)
            # Trace per-tick timestamp alignment for debugging
            try:
                if getattr(self, "debug_gating", False):
                    self.log.info(
                        "ON_FEAT",
                        symbol=symbol,
                        ts_local=str(ts_local),
                        sec=int(ts_local.second),
                        min_start=str(minute_start_local)
                    )
            except Exception:
                pass
            # Only process 5s feed on minute boundary
            if ts_local.replace(microsecond=0) != minute_start_local:
                try:
                    if getattr(self, "debug_gating", False):
                        self.log.info("MIN_SKIP", symbol=symbol, ts=str(ts_local), minute_start=str(minute_start_local))
                except Exception:
                    pass
                # Debug probe on every 5s: run gating without submitting intents
                if getattr(self, "debug_every_bar", False):
                    try:
                        ts_utc_probe = minute_start_local.astimezone(UTC_TZ)
                        end_ts_utc_probe = ts_utc_probe + timedelta(minutes=1)
                        bar_probe = {
                            "ts": ts_utc_probe,
                            "ts_utc": ts_utc_probe,
                            "end_ts_utc": end_ts_utc_probe,
                            "bar_close_ts_utc": end_ts_utc_probe,
                            "open": float(features.get("open", 0.0) or 0.0),
                            "high": float(features.get("high", 0.0) or 0.0),
                            "low": float(features.get("low", 0.0) or 0.0),
                            "close": float(features.get("close", 0.0) or 0.0),
                            "volume": float(features.get("volume", 0.0) or 0.0),
                            "vwap": float(features.get("vwap", 0.0) or 0.0),
                            "timeframe": "1m",
                        }
                        setattr(self, "_debug_dry_run", True)
                        await self._apply_bar_effects(symbol, "1m", bar_probe, replay=False)
                    except Exception:
                        pass
                    finally:
                        try:
                            setattr(self, "_debug_dry_run", False)
                        except Exception:
                            pass
                return

            # Convert to UTC for downstream
            ts_utc = minute_start_local.astimezone(UTC_TZ)
            end_ts_utc = ts_utc + timedelta(minutes=1)

            try:
                self.log.info("MIN_BOUNDARY", symbol=symbol, ts=str(minute_start_local))
            except Exception:
                pass

            bar = {
                "ts": ts_utc,
                "ts_utc": ts_utc,
                "end_ts_utc": end_ts_utc,
                "bar_close_ts_utc": end_ts_utc,
                "open": float(features.get("open", 0.0) or 0.0),
                "high": float(features.get("high", 0.0) or 0.0),
                "low": float(features.get("low", 0.0) or 0.0),
                "close": float(features.get("close", 0.0) or 0.0),
                "volume": float(features.get("volume", 0.0) or 0.0),
                "vwap": float(features.get("vwap", 0.0) or 0.0),
                "timeframe": "1m",
            }
            await self._apply_bar_effects(symbol, "1m", bar, replay=False)
        except Exception:
            self.log.exception(f"on_features error for {symbol}")

    async def _get_symbol_historical_context(self, symbol: str) -> Tuple[float, float, float, float]:
        if not CLICKHOUSE_AVAILABLE:
            return 0.0, 0.1, 0.5, 10000.0

        self._ensure_clickhouse_client()
        if not self.ch:
            return 0.0, 0.1, 0.5, 10000.0

        bars_1m_table = self.ctx.param("db_tables.bars_1m", "trading.bars_1m_ibkr")
        features_1m_hist_table = self.ctx.param("db_tables.features_1m_hist", "trading.features_1m")
        features_5m_hist_table = self.ctx.param("db_tables.features_5m_hist", "trading.features_5m")

        try:
            query = f"""
            WITH prev_day AS (
                SELECT max(toDate(ts_start)) AS day
                FROM {bars_1m_table} FINAL
                WHERE symbol = {{symbol:String}} AND toDate(ts_start) < today()
            )
            SELECT
                coalesce(
                    (
                        SELECT argMax(close, ts_start) FROM {bars_1m_table} FINAL
                        WHERE symbol = {{symbol:String}} AND toDate(ts_start) = (SELECT day FROM prev_day)
                    ),
                    0.0
                ) AS prev_close,
                coalesce(
                    (
                        SELECT argMax(atr_14, ts) FROM {features_1m_hist_table} FINAL
                        WHERE symbol = {{symbol:String}} AND toDate(ts) = (SELECT day FROM prev_day)
                    ),
                    0.1
                ) AS prev_atr_1m,
                coalesce(
                    (
                        SELECT argMax(atr_14, ts) FROM {features_5m_hist_table} FINAL
                        WHERE symbol = {{symbol:String}} AND toDate(ts) = (SELECT day FROM prev_day)
                    ),
                    0.5
                ) AS prev_atr_5m,
                coalesce(
                    (
                        SELECT quantile(0.5)(volume) FROM {bars_1m_table} FINAL
                        WHERE symbol = {{symbol:String}}
                          AND ts_start >= today() - INTERVAL 3 DAY
                          AND ts_start < today()
                          AND volume > 0
                    ),
                    10000.0
                ) AS median_vol_60m
            """
            params = {"symbol": symbol}
            rows = self.ch.query(query, parameters=params)  # type: ignore
            row = rows[0] if rows else None
            if row:
                prev_close = float(row.get("prev_close", 0.0))
                prev_atr_1m = max(0.1, float(row.get("prev_atr_1m", 0.1)))
                prev_atr_5m = max(0.5, float(row.get("prev_atr_5m", 0.5)))
                median_vol = float(row.get("median_vol_60m", 10000.0))
                return prev_close, prev_atr_1m, prev_atr_5m, median_vol
        except Exception:
            self.log.exception(f"Context query failed for {symbol}")
        return 0.0, 0.1, 0.5, 10000.0

    async def _backfill_orb_from_today(self) -> None:
        """
        Backfill ORB data from today's market open (09:30-09:45 ET / 06:30-06:45 PT).
        This allows the strategy to work even if started after the ORB window.

        Note: Skipped in backtest mode as historical data is loaded upfront.
        """
        # Skip ORB backfill in backtest mode (ctx.param returns backtest config params)
        if self.ctx.param("_backtest_mode", False):
            self.log.info("Backtest mode detected, skipping ORB backfill (data loaded upfront)")
            return

        if not CLICKHOUSE_AVAILABLE:
            self.log.warning("ClickHouse not available, skipping ORB backfill")
            return

        self._ensure_clickhouse_client()
        if not self.ch:
            self.log.warning("No ClickHouse client, skipping ORB backfill")
            return

        bars_1m_table = self.ctx.param("db_tables.bars_1m", "trading.bars_1m_ibkr")

        try:
            from zoneinfo import ZoneInfo
            ET_TZ = ZoneInfo("America/New_York")

            # Get today's date in ET timezone
            now_et = datetime.now(ET_TZ)
            today_et = now_et.date()

            # ORB window: 09:30-09:45 ET (15 minutes for ORB15, first 5 minutes for ORB5)
            orb_start_et = datetime.combine(today_et, dt_time(9, 30), tzinfo=ET_TZ)
            orb5_end_et = datetime.combine(today_et, dt_time(9, 35), tzinfo=ET_TZ)
            orb15_end_et = datetime.combine(today_et, dt_time(9, 45), tzinfo=ET_TZ)

            # Convert to UTC for ClickHouse query
            orb_start_utc = orb_start_et.astimezone(UTC_TZ)
            orb15_end_utc = orb15_end_et.astimezone(UTC_TZ)

            # Check if we're past the ORB window
            if now_et < orb_start_et:
                self.log.info("Before market open, no ORB data to backfill yet")
                return

            self.log.info(
                "Backfilling ORB data from today's open",
                orb_window=f"{orb_start_et.strftime('%H:%M')}-{orb15_end_et.strftime('%H:%M')} ET",
                current_time=now_et.strftime('%H:%M ET')
            )

            # Query for each symbol
            for symbol in self.universe:
                try:
                    # Format times as strings for ClickHouse
                    orb_start_str = orb_start_utc.strftime('%Y-%m-%d %H:%M:%S')
                    orb5_end_str = orb5_end_et.astimezone(UTC_TZ).strftime('%Y-%m-%d %H:%M:%S')
                    orb15_end_str = orb15_end_utc.strftime('%Y-%m-%d %H:%M:%S')

                    query = f"""
                    SELECT
                        max(high) as orb15_high,
                        min(low) as orb15_low,
                        maxIf(high, ts_start < '{orb5_end_str}') as orb5_high,
                        minIf(low, ts_start < '{orb5_end_str}') as orb5_low
                    FROM {bars_1m_table} FINAL
                    WHERE symbol = '{symbol}'
                      AND ts_start >= '{orb_start_str}'
                      AND ts_start < '{orb15_end_str}'
                    """

                    rows = self.ch.query(query)  # type: ignore
                    if not rows:
                        self.log.warning(f"No ORB data found for {symbol}")
                        continue

                    row = rows[0]
                    orb15_high = row.get("orb15_high")
                    orb15_low = row.get("orb15_low")
                    orb5_high = row.get("orb5_high")
                    orb5_low = row.get("orb5_low")

                    # Debug: log raw query results
                    self.log.info(
                        f"ORB query result for {symbol}",
                        raw_row=row,
                        orb15_high=orb15_high,
                        orb15_low=orb15_low,
                        orb5_high=orb5_high,
                        orb5_low=orb5_low
                    )

                    # Update ORB state if we have valid data
                    if symbol in self.symbol_states:
                        orb_state: ORBTracker = self.symbol_states[symbol]["orb_state"]

                        if orb15_high is not None and orb15_low is not None:
                            orb_state.orb15_high = float(orb15_high)
                            orb_state.orb15_low = float(orb15_low)

                        if orb5_high is not None and orb5_low is not None:
                            orb_state.orb5_high = float(orb5_high)
                            orb_state.orb5_low = float(orb5_low)

                        self.log.info(
                            f"ORB backfilled for {symbol}",
                            orb5=f"{orb_state.orb5_low:.2f}-{orb_state.orb5_high:.2f}" if orb_state.orb5_low else "N/A",
                            orb15=f"{orb_state.orb15_low:.2f}-{orb_state.orb15_high:.2f}" if orb_state.orb15_low else "N/A"
                        )

                except Exception as e:
                    self.log.error(f"Failed to backfill ORB for {symbol}: {e}", exc_info=True)

            self.log.info("ORB backfill completed for all symbols")

        except Exception as e:
            self.log.error(f"ORB backfill failed: {e}", exc_info=True)


class RevEIntradayStrategyV2(RevEIntradayStrategy):
    """Explicit V2 wrapper to ensure loader picks the instrumented class."""

    def __init__(self, ctx: StrategyContext, params: StrategyParams):
        super().__init__(ctx, params)
        try:
            self.log.info(
                "REVE_V2_CONSTRUCTED",
                strategy_id=self.strategy_id,
                module=__name__,
                universe=list(self.universe),
            )
        except Exception:
            pass

    def _initialize_all_symbol_states(self, initial_context: Dict[str, Dict[str, Any]]):
        for symbol in self.universe:
            ctx = initial_context.get(
                symbol,
                {"prev_close": 0.0, "prev_atr_1m": 0.1, "prev_atr_5m": 0.5, "median_vol_60m": 10000.0},
            )
            self.symbol_states[symbol] = {
                "atr_calculator": {
                    "1m": ATRState(last_close=ctx["prev_close"], prev_atr=ctx["prev_atr_1m"], current_atr=ctx["prev_atr_1m"]),
                    "5m": ATRState(last_close=ctx["prev_close"], prev_atr=ctx["prev_atr_5m"], current_atr=ctx["prev_atr_5m"]),
                },
                "orb_state": ORBTracker(),
                "rolling_windows": {
                    "abs_dev_vwap_90": deque(maxlen=self.strat_A_absdev_win),
                    "vol_1m_20": deque(maxlen=20),
                    "updown_vwap_15": deque(maxlen=15),
                    "close_minus_vwap_15": deque(maxlen=15),
                    "orl_break_samples_30min": [],
                },
                "ewma_state": {"q80": ctx.get("initial_q80_ewma", 0.1), "k": ctx.get("initial_k_ewma", 0.5)},
                "regime": "RANGE",
                "last_1m_bars": deque(maxlen=max(10, ATR_PERIOD)),
                "median_vol_60m": ctx["median_vol_60m"],
                "last_entry_bar_start_utc": None,
                "last_bar_ts_1m": None,
                "flattened_today": False,
            }
            self.open_positions.setdefault(symbol, [])

    # Legacy Kafka/tick path has been removed in v2.1. Strategy runs only via engine-driven 1m features.

    # ----------------- BAR effects (core pipeline; replay-safe) -----------------

    async def _apply_bar_effects(self, symbol: str, timeframe: str, bar: Dict[str, Any], replay: bool) -> None:
        """
        统一的 bar 完成处理内核。
        - replay=True: 仅更新状态/指标，不生成入场/出场意图，也不计交易数与 PnL。
        - replay=False: 正常执行策略信号与风控/出场。
        """
        state = self.symbol_states.get(symbol)
        if not state or not bar:
            return

        close = bar["close"]
        high = bar["high"]
        low = bar["low"]
        volume = bar["volume"]
        bar_start_utc = bar["ts_utc"]
        bar_start_pt = bar_start_utc.astimezone(PACIFIC_TZ)
        bar_time_pt = bar_start_pt.time()

        # ---- ATR update ----
        try:
            if timeframe in state["atr_calculator"]:
                atr_calc: ATRState = state["atr_calculator"][timeframe]
                prev_close = atr_calc.last_close
                tr = (
                    max(high - low, abs(high - prev_close), abs(low - prev_close))
                    if prev_close > 0
                    else high - low
                )
                current_atr = ((atr_calc.prev_atr * (atr_calc.period - 1)) + tr) / atr_calc.period
                tick_size = self.ctx.param(f"tick_size.{symbol}", TICK_SIZE_DEFAULT)
                atr_calc.current_atr = max(current_atr, 5 * tick_size)
                atr_calc.prev_atr = atr_calc.current_atr
                atr_calc.last_close = close
        except Exception:
            self.log.exception(f"ATR update error: {symbol} {timeframe}")
            return

        # ---- ORB boundaries maintenance on 1m ----
        if timeframe == "1m":
            try:
                orb: ORBTracker = state["orb_state"]
                # During ORB windows build highs/lows by bar
                if orb.start_time_pt <= bar_time_pt < orb.orb5_end_pt:
                    orb.orb5_high = max(orb.orb5_high or -float("inf"), high)
                    orb.orb5_low = min(orb.orb5_low or float("inf"), low)
                if orb.start_time_pt <= bar_time_pt < orb.orb15_end_pt:
                    orb.orb15_high = max(orb.orb15_high or -float("inf"), high)
                    orb.orb15_low = min(orb.orb15_low or float("inf"), low)

                # Turn pending tick-level break into official break when the "break bar" closes
                if orb.pending_break_bar_start_utc and orb.pending_break_bar_start_utc == bar_start_utc:
                    # finalize the break at this bar close
                    orb.orb_low_broken = True
                    orb.orb_low_break_bar_start_utc = bar_start_utc
                    # lowest since break captured from tick-level
                    if orb.pending_lowest_since_break is not None:
                        orb.lowest_since_orb_break = (
                            orb.pending_lowest_since_break
                            if orb.lowest_since_orb_break is None
                            else min(orb.lowest_since_orb_break, orb.pending_lowest_since_break)
                        )
                    # collect break sample for 07:00 k calibration (first 30 min)
                    atr_5m_val = state["atr_calculator"]["5m"].current_atr
                    if atr_5m_val > 0 and bar_time_pt < dt_time(7, 0) and orb.orb15_low is not None:
                        depth = (orb.orb15_low - (orb.lowest_since_orb_break or low))
                        state["rolling_windows"]["orl_break_samples_30min"].append(depth / atr_5m_val)
                    # clear pending
                    orb.pending_break_bar_start_utc = None
                    orb.pending_lowest_since_break = None

                # If too late (beyond next bar) without recovery, auto-reset the break state
                if orb.orb_low_broken and orb.orb_low_break_bar_start_utc:
                    if bar_start_utc >= (orb.orb_low_break_bar_start_utc + timedelta(minutes=2)):
                        # beyond the "next bar" window -> reset
                        orb.orb_low_broken = False
                        orb.orb_low_break_bar_start_utc = None
                        orb.lowest_since_orb_break = None
            except Exception:
                self.log.exception(f"ORB maintenance error: {symbol}")

        # ---- 1m regime & signal pipeline ----
        if timeframe == "1m":
            self.current_bar_index += 1  # for logging
            state["last_bar_ts_1m"] = bar_start_utc

            # Prefer bar/feature-provided VWAP; fallback to close
            vwap = float(bar.get("vwap", 0.0) or 0.0) or close

            try:
                atr_1m = state["atr_calculator"]["1m"].current_atr
                if atr_1m <= 1e-6:
                    return

                # rolling windows
                windows = state["rolling_windows"]
                windows["abs_dev_vwap_90"].append(abs(close - vwap))
                windows["vol_1m_20"].append(volume)

                up_flag = close >= vwap
                windows["updown_vwap_15"].append(up_flag)
                windows["close_minus_vwap_15"].append(abs(close - vwap))
                state["last_1m_bars"].append(bar)

                # regime gating
                p_up = 0.5
                trend_push = 0.0
                if len(windows["updown_vwap_15"]) >= 15:
                    p_up = float(np.mean(windows["updown_vwap_15"]))
                    if len(windows["close_minus_vwap_15"]) >= 15:
                        trend_push = float(np.median(list(windows["close_minus_vwap_15"]))) / atr_1m
                    if p_up >= self.gating_p_up_uptrend and trend_push >= self.gating_trend_push_threshold:
                        state["regime"] = "UP_TREND"
                    elif p_up <= self.gating_p_up_downtrend and trend_push >= self.gating_trend_push_threshold:
                        state["regime"] = "DOWN_TREND"
                    else:
                        state["regime"] = "RANGE"

                # EWMA q80 -> BandDistDown
                ewma = state["ewma_state"]
                if len(windows["abs_dev_vwap_90"]) >= self.strat_A_absdev_win:
                    Q80_today = float(np.quantile(list(windows["abs_dev_vwap_90"]), self.strat_A_q_level))
                    ewma["q80"] = self.strat_A_ewma_alpha * Q80_today + (1 - self.strat_A_ewma_alpha) * ewma["q80"]
                BandDistDown = float(
                    np.clip(
                        ewma["q80"],
                        self.strat_A_band_clip_low_atr * atr_1m,
                        self.strat_A_band_clip_high_atr * atr_1m,
                    )
                )

                # k EWMA at 07:00
                if bar_time_pt == dt_time(7, 0):
                    samples = windows["orl_break_samples_30min"]
                    if len(samples) >= 3:
                        k_raw_today = float(np.quantile(samples, self.strat_B_k_quantile))
                        ewma["k"] = self.strat_B_k_ewma_beta * k_raw_today + (1 - self.strat_B_k_ewma_beta) * ewma["k"]
                    windows["orl_break_samples_30min"] = []
                k_val = float(np.clip(ewma["k"], self.strat_B_k_clip_low, self.strat_B_k_clip_high))
                atr_5m_val = state["atr_calculator"]["5m"].current_atr

                if replay:
                    return  # 重放：到此为止，仅维护状态

                # ---- Live: entry/exit/flatten ----
                now_utc = datetime.now(UTC_TZ)
                now_pt_time = now_utc.astimezone(PACIFIC_TZ).time()
                current_equity = await self._get_current_equity()
                allow_new = self._can_open_new_position(symbol, now_pt_time, current_equity)

                if allow_new:
                    tick_size = self.ctx.param(f"tick_size.{symbol}", TICK_SIZE_DEFAULT)
                    index_state = await self._get_index_state(symbol)
                    # Strategy A'
                    self._check_strategy_A_entry(symbol, bar, vwap, atr_1m, p_up, BandDistDown, tick_size, index_state, k_val)
                    # Strategy B' (next 1m recovery after tick-level break)
                    if bar_time_pt < dt_time(8, 0):
                        self._check_strategy_B_entry(symbol, bar, state["orb_state"], k_val, atr_5m_val, tick_size)
                else:
                    # Log why we cannot open new positions right now
                    try:
                        self._gate_log(
                            "PRE:allow_new_false",
                            symbol=symbol,
                            regime=str(state.get("regime")),
                            now_pt=str(now_pt_time),
                            stop_new_pt=str(self.schedule_stop_new_pt),
                            equity=float(current_equity),
                            realized_pnl=float(self.realized_pnl_today),
                            daily_stop_pct=float(self.daily_stop_pct),
                            consec_losses=int(self.consecutive_losses_today),
                            max_consec=int(self.max_consecutive_losses),
                        )
                    except Exception:
                        pass

                # manage exits (intra-bar 5s path when available)
                await self._manage_open_positions(symbol, bar, atr_1m, atr_5m_val, vwap, state["orb_state"])

                # force flatten near close
                if now_pt_time >= self.schedule_force_flat_pt and not state.get("flattened_today", False):
                    asyncio.create_task(self._force_flatten_symbol(symbol))
                    state["flattened_today"] = True

                # periodic save
                now = time.time()
                save_interval = 60 if self.open_positions.get(symbol) else self.save_state_interval_sec
                if now - self.last_state_save_time >= save_interval:
                    self.save_state()
                    self.last_state_save_time = now

            except Exception:
                self.log.exception(f"Error in 1m pipeline for {symbol}")

    # ----------------- Strategy A / B -----------------

    def _get_candlestick_confirmation(self, symbol: str) -> Tuple[bool, Optional[float], Optional[float]]:
        state = self.symbol_states[symbol]
        last_bars = list(state["last_1m_bars"])
        if len(last_bars) < 3:
            return False, None, None

        bar = last_bars[-1]
        prev_bar = last_bars[-2]
        prev_prev_bar = last_bars[-3]

        # Bottom fractal + low vol
        is_bottom_fractal = prev_bar["low"] < prev_prev_bar["low"] and prev_bar["low"] < bar["low"]
        if is_bottom_fractal:
            vol_window = list(state["rolling_windows"]["vol_1m_20"])
            if len(vol_window) >= 10:
                hist = vol_window[:-2] if len(vol_window) >= 2 else vol_window
                median_vol_ref = float(np.median(hist)) if hist else 0.0
                if median_vol_ref > 0 and prev_bar["volume"] <= median_vol_ref * self.strat_A_vol_collapse_mult:
                    return True, prev_bar["high"], prev_bar["low"]

        # Bullish engulfing
        curr_is_green = bar["close"] > bar["open"]
        prev_is_red = prev_bar["open"] > prev_bar["close"]
        if curr_is_green and prev_is_red:
            if bar["open"] < prev_bar["close"] and bar["close"] > prev_bar["open"]:
                return True, bar["high"], bar["low"]

        return False, None, None

    def _guardrail_param_bounds_ok(self, BandDistDown: float, atr_1m: float, k_val: Optional[float]) -> bool:
        ratio = (BandDistDown / atr_1m) if atr_1m > 1e-9 else float("inf")
        if ratio < self.guard_banddistdown_atr_min or ratio > self.guard_banddistdown_atr_max:
            return False
        if k_val is not None and (k_val < self.guard_k_min or k_val > self.guard_k_max):
            return False
        return True

    def _can_open_new_position(self, symbol: str, now_pt_time: dt_time, current_equity: float) -> bool:
        state = self.symbol_states.get(symbol)
        if not state:
            return False
        if now_pt_time >= self.schedule_stop_new_pt:
            return False
        if state["regime"] == "DOWN_TREND":
            return False
        if current_equity <= 0:
            return False
        if self.realized_pnl_today <= -self.daily_stop_pct * current_equity:
            if not getattr(self, "_daily_stop_logged", False):
                self.log.warning("Daily stop loss hit. No new positions.")
                self._daily_stop_logged = True
            return False
        if self.consecutive_losses_today >= self.max_consecutive_losses:
            if not getattr(self, "_max_loss_logged", False):
                self.log.warning("Max consecutive losses hit. No new positions.")
                self._max_loss_logged = True
            return False
        # cooldown by bar start time memory
        last_entry_start = state.get("last_entry_bar_start_utc")
        if last_entry_start is not None and state["last_1m_bars"]:
            last_completed_start = state["last_1m_bars"][-1]["ts_utc"]
            # ensure at least N bars between
            if (last_completed_start - last_entry_start) < timedelta(minutes=self.exec_cooldown_bars):
                return False
        return True

    def _check_strategy_A_entry(
        self,
        symbol: str,
        bar: Dict[str, Any],
        vwap: float,
        atr_1m: float,
        p_up: float,
        BandDistDown: float,
        tick_size: float,
        index_state: Optional[MarketIndexState],
        k_val: float,
    ):
        state = self.symbol_states[symbol]
        orb_state: ORBTracker = state["orb_state"]

        if index_state is None or index_state.get("close") is None or index_state.get("vwap") is None:
            # As a last-resort fallback for gating diagnostics, use the current symbol's bar
            try:
                self.log.info("INDEX_PROXY_SELF", symbol=symbol, price=float(bar.get("close", 0.0) or 0.0), vwap=float(vwap))
            except Exception:
                pass
            index_state = {"symbol": symbol, "close": float(bar.get("close", 0.0) or 0.0), "vwap": float(vwap)}
        if float(index_state["close"]) < float(index_state["vwap"]):
            self._gate_log("A:market_below_vwap", symbol=symbol, idx_close=float(index_state["close"]), idx_vwap=float(index_state["vwap"]))
            return
        if p_up < 0.50:
            self._gate_log("A:p_up_below_threshold", symbol=symbol, p_up=float(p_up), threshold=0.50)
            return

        price_snapshot = self._get_sync_price_snapshot(symbol)
        # First enforce a volume floor regardless of L1
        min_vol_req = float(state["median_vol_60m"]) * float(self.liquidity_min_vol_mult_1m)
        current_vol = float(bar.get("volume", 0.0) or 0.0)
        if float(state["median_vol_60m"]) > 0 and current_vol < min_vol_req:
            self._gate_log(
                "A:liquidity_vol_low",
                symbol=symbol,
                vol=current_vol,
                min_vol=min_vol_req,
            )
            return

        # Spread check only if we have L1; otherwise soft-allow with log
        if price_snapshot is None:
            self._gate_log("A:liquidity_soft_allow_no_l1", symbol=symbol)
        else:
            liquid_ok = self._check_liquidity(symbol, current_vol, float(state["median_vol_60m"]), price_snapshot)
            if not liquid_ok:
                spread = None
                try:
                    if getattr(price_snapshot, "ask", 0.0) and getattr(price_snapshot, "bid", 0.0):
                        spread = float(price_snapshot.ask) - float(price_snapshot.bid)
                except Exception:
                    spread = None
                self._gate_log(
                    "A:liquidity_filter",
                    symbol=symbol,
                    spread=spread,
                    max_spread=self.liquidity_max_spread,
                )
                return

        price = bar["close"]
        in_zone = abs(price - vwap) <= self.strat_A_near_vwap_atr * atr_1m or (0 < (vwap - price) <= BandDistDown)
        if not in_zone:
            self._gate_log(
                "A:not_in_zone",
                symbol=symbol,
                price=float(price),
                vwap=float(vwap),
                atr_1m=float(atr_1m),
                near_band=float(self.strat_A_near_vwap_atr * atr_1m),
                vwap_minus_price=float(vwap - price),
                BandDistDown=float(BandDistDown),
            )
            return

        confirmed, signal_high, signal_low = self._get_candlestick_confirmation(symbol)
        if not confirmed or signal_high is None or signal_low is None:
            self._gate_log("A:candlestick_confirm_fail", symbol=symbol)
            return

        if not self._guardrail_param_bounds_ok(BandDistDown, atr_1m, k_val):
            self._gate_log(
                "A:guardrail_bounds",
                symbol=symbol,
                BandDistDown=float(BandDistDown),
                atr_1m=float(atr_1m),
                k_val=float(k_val),
                min_band=float(self.guard_banddistdown_atr_min),
                max_band=float(self.guard_banddistdown_atr_max),
                k_min=float(self.guard_k_min),
                k_max=float(self.guard_k_max),
            )
            return

        try:
            self.log.info(
                "DECISION_DEBUG A-entry gating passed",
                symbol=symbol,
                price=float(bar.get("close", 0.0) or 0.0),
                vwap=float(vwap),
                atr_1m=float(atr_1m),
                p_up=float(p_up),
                BandDistDown=float(BandDistDown),
                k_val=float(k_val),
            )
        except Exception:
            pass

        entry_trigger_price = signal_high + max(tick_size, self.exec_entry_buffer_atr * atr_1m)
        stop_loss_price = min(signal_low, vwap - self.strat_A_sl_atr * atr_1m)

        # TP1(先到): min(entry + ATR, OR中线, 近10根摆动前高)
        candidates: List[float] = [entry_trigger_price + self.strat_A_tp1_atr * atr_1m]
        if orb_state.orb15_high is not None and orb_state.orb15_low is not None:
            or_mid = (orb_state.orb15_high + orb_state.orb15_low) / 2.0
            candidates.append(or_mid)
        swing_window = list(state["last_1m_bars"])[-10:]
        if swing_window:
            swing_high = max(b["high"] for b in swing_window)
            candidates.append(swing_high)
        tp1 = min((x for x in candidates if x > entry_trigger_price), default=(entry_trigger_price + self.strat_A_tp1_atr * atr_1m))

        state["last_entry_bar_start_utc"] = bar["ts_utc"]
        asyncio.create_task(
            self._generate_entry_intent(
                symbol=symbol,
                side="BUY",
                entry_price=entry_trigger_price,
                sl_price=stop_loss_price,
                tp1_price=tp1,
                trail_atr_mult=self.strat_A_trail_atr,
                atr_basis=atr_1m,
                strategy_tag="A",
                bar_close_ts_utc=bar["bar_close_ts_utc"],
                current_1m_volume=bar["volume"],
                median_vol_60m=state["median_vol_60m"],
                current_atr=atr_1m,
                BandDistDown=BandDistDown,
                guard_limits={
                    "banddistdown_atr_min": self.guard_banddistdown_atr_min,
                    "banddistdown_atr_max": self.guard_banddistdown_atr_max,
                    "k_min": self.guard_k_min,
                    "k_max": self.guard_k_max,
                },
                k=k_val,
            )
        )

    def _check_strategy_B_entry(
        self,
        symbol: str,
        bar: Dict[str, Any],
        orb_state: ORBTracker,
        k: float,
        atr_5m: float,
        tick_size: float,
    ):
        # Need established OR15
        if orb_state.orb15_low is None or orb_state.orb15_high is None:
            self._gate_log("B:orb_not_ready", symbol=symbol)
            return

        # Must have tick-level break already finalized at prior bar close
        break_start = orb_state.orb_low_break_bar_start_utc
        if not orb_state.orb_low_broken or break_start is None:
            self._gate_log("B:no_finalized_break", symbol=symbol)
            return

        # This bar must be exactly next 1m after the break bar
        if bar["ts_utc"] != (break_start + timedelta(minutes=1)):
            # if we passed beyond next bar, reset handled in _apply_bar_effects
            self._gate_log("B:not_next_bar_after_break", symbol=symbol, current=str(bar["ts_utc"]), next_expected=str(break_start + timedelta(minutes=1)))
            return

        # recovery in this bar
        recovered_this_bar = bar["close"] >= orb_state.orb15_low
        if not recovered_this_bar:
            self._gate_log("B:no_recovery_above_orb15_low", symbol=symbol, close=float(bar["close"]), orb15_low=float(orb_state.orb15_low))
            return

        max_breakdown_depth = (orb_state.orb15_low - (orb_state.lowest_since_orb_break or bar["low"]))
        if max_breakdown_depth >= k * atr_5m:
            # too deep -> skip and reset handled by _apply_bar_effects on next bar
            self._gate_log("B:breakdown_too_deep", symbol=symbol, depth=float(max_breakdown_depth), k_val=float(k), atr_5m=float(atr_5m), max_allowed=float(k * atr_5m))
            return

        # local guardrail: k bound
        if not self._guardrail_param_bounds_ok(BandDistDown=atr_5m, atr_1m=atr_5m, k_val=k):
            self._gate_log("B:guardrail_bounds", symbol=symbol, atr_5m=float(atr_5m), k_val=float(k), k_min=float(self.guard_k_min), k_max=float(self.guard_k_max))
            return

        high = bar["high"]
        entry_trigger_price = high + max(tick_size, self.exec_entry_buffer_atr * atr_5m)
        stop_loss_price = (orb_state.lowest_since_orb_break or bar["low"]) - self.strat_B_sl_atr5m * atr_5m

        state = self.symbol_states[symbol]
        sum_pcv, sum_v = state["vwap_calculator"]
        vwap = (sum_pcv / sum_v) if sum_v > 0 else bar["close"]
        or_mid = (orb_state.orb15_high + orb_state.orb15_low) / 2
        tp1 = min(vwap, or_mid)

        try:
            self.log.info(
                "DECISION_DEBUG B-entry gating passed",
                symbol=symbol,
                high=float(high),
                vwap=float(vwap),
                atr_5m=float(atr_5m),
                k_val=float(k),
            )
        except Exception:
            pass

        # reset break state after signal consumption
        orb_state.orb_low_broken = False
        orb_state.orb_low_break_bar_start_utc = None
        orb_state.lowest_since_orb_break = None

        state["last_entry_bar_start_utc"] = bar["ts_utc"]
        asyncio.create_task(
            self._generate_entry_intent(
                symbol=symbol,
                side="BUY",
                entry_price=entry_trigger_price,
                sl_price=stop_loss_price,
                tp1_price=tp1,
                trail_atr_mult=self.strat_B_trail_atr5m,
                atr_basis=atr_5m,
                strategy_tag="B",
                bar_close_ts_utc=bar["bar_close_ts_utc"],
                current_1m_volume=bar["volume"],
                median_vol_60m=state["median_vol_60m"],
                current_atr=state["atr_calculator"]["1m"].current_atr,
                k=k,
                guard_limits={
                    "banddistdown_atr_min": self.guard_banddistdown_atr_min,
                    "banddistdown_atr_max": self.guard_banddistdown_atr_max,
                    "k_min": self.guard_k_min,
                    "k_max": self.guard_k_max,
                },
            )
        )

    # ----------------- Liquidity & position mgmt -----------------

    def _check_liquidity(
        self, symbol: str, current_volume: float, median_volume: float, price_snapshot: Optional[PriceSnapshot]
    ) -> bool:
        # In backtest mode, price_snapshot might be None or have None attributes
        if price_snapshot:
            ask = getattr(price_snapshot, "ask", None)
            bid = getattr(price_snapshot, "bid", None)
            if ask is not None and bid is not None and ask > 0 and bid > 0:
                spread = ask - bid
                if spread > self.liquidity_max_spread:
                    return False
            else:
                # No price data available (backtest mode) - skip spread check
                pass
        # If no price_snapshot or no spread data, fall through to volume check

        if median_volume > 0 and current_volume < median_volume * self.liquidity_min_vol_mult_1m:
            return False

        return True

    async def _manage_open_positions(
        self, symbol: str, bar: Dict[str, Any], atr_1m: float, atr_5m: float, vwap: float, orb_state: ORBTracker
    ):
        positions = self.open_positions.get(symbol, [])
        if not positions:
            return

        positions_to_remove: List[int] = []
        low = bar["low"]
        high = bar["high"]
        close = bar["close"]

        # Try to fetch 1s subbars within this minute from SFS (Redis). Newest→older returned; filter to this minute and sort ascending.
        subbars: List[Dict[str, Any]] = []
        if self._sfs_reader is not None and bar.get("ts_utc") and bar.get("end_ts_utc") and self.ctx.param("sfs.enable_1s", True):
            try:
                start_ms = int(bar["ts_utc"].timestamp() * 1000)
                end_ms = int(bar["end_ts_utc"].timestamp() * 1000)
                arr = await self._sfs_reader.last_n(symbol, 60)
                subbars = [x for x in arr if isinstance(x, dict) and start_ms <= int(x.get("ts", 0)) < end_ms]
                # order oldest → newest
                subbars.sort(key=lambda r: int(r.get("ts", 0)))
            except Exception:
                subbars = []
        # Fallback to 5s sliding from ClickHouse within the minute
        if not subbars and CLICKHOUSE_AVAILABLE and self.ctx.param("sfs.enable_5s", True) and bar.get("ts_utc") and bar.get("end_ts_utc"):
            self._ensure_clickhouse_client()
            if not self.ch:
                subbars = []
            else:
                try:
                    q = (
                        "SELECT ts, open, high, low, close FROM sfs.sfs_5s "
                        "WHERE symbol = {symbol:String} AND ts >= {start:DateTime64(3,'UTC')} AND ts < {end:DateTime64(3,'UTC')} "
                        "ORDER BY ts ASC"
                    )
                    rows = self.ch.query(
                        q,
                        parameters={
                            "symbol": symbol,
                            "start": bar["ts_utc"],
                            "end": bar["end_ts_utc"],
                        },
                    )  # type: ignore
                    subbars = [
                        {
                            "ts": int(r.get("ts").timestamp() * 1000) if r.get("ts") else 0,
                            "open": r.get("open"),
                            "high": r.get("high"),
                            "low": r.get("low"),
                            "close": r.get("close"),
                        }
                        for r in rows or []
                    ]
                except Exception:
                    subbars = []

        for i, pos in enumerate(list(positions)):
            exit_signal: Optional[Tuple[str, float]] = None
            qty_to_exit: float = 0.0
            pnl_on_exit: float = 0.0

            # Priority: SL -> TP1 -> TRAIL
            if subbars:
                # simulate within the minute by 1s bars
                for sb in subbars:
                    sb_open = float(sb.get("open", 0) or 0)
                    sb_high = float(sb.get("high", 0) or 0)
                    sb_low = float(sb.get("low", 0) or 0)
                    sb_close = float(sb.get("close", 0) or 0)
                    if sb_low <= pos.sl_price:
                        exit_signal = ("SL", pos.sl_price)
                        qty_to_exit = pos.qty
                        pnl_on_exit = (pos.sl_price - pos.entry_price) * qty_to_exit
                        self.consecutive_losses_today += 1
                        break
                    if exit_signal is None:
                        if not pos.tp1_hit:
                            tp1_target = pos.tp1_price
                            if pos.strategy_tag == "B":
                                if orb_state.orb15_high is not None and orb_state.orb15_low is not None:
                                    or_mid = (orb_state.orb15_high + orb_state.orb15_low) / 2
                                    tp1_target = min(vwap, or_mid) if vwap > 0 else or_mid
                            if tp1_target is not None and sb_high >= tp1_target:
                                exit_signal = ("TP1", tp1_target)
                                intended = math.floor(pos.qty * 0.7)
                                qty_to_exit = max(0.0, min(pos.qty, intended if intended > 0 else pos.qty))
                                if qty_to_exit > 0:
                                    remaining = pos.qty - qty_to_exit
                                    pnl_on_exit = (tp1_target - pos.entry_price) * qty_to_exit
                                    pos.tp1_hit = True
                                    pos.qty = remaining
                                    # update trail baseline on 1s close
                                    pos.max_close_since_entry = max(pos.max_close_since_entry, sb_close)
                                    atr_basis = atr_1m if pos.strategy_tag == "A" else atr_5m
                                    if atr_basis > 1e-6:
                                        pos.current_trail_price = sb_close - pos.trail_atr_mult * atr_basis
                                    self.consecutive_losses_today = 0
                                else:
                                    exit_signal = None
                        else:
                            # update trail dynamically per 1s
                            pos.max_close_since_entry = max(pos.max_close_since_entry, sb_close)
                            atr_basis = atr_1m if pos.strategy_tag == "A" else atr_5m
                            if atr_basis > 1e-6:
                                new_trail = pos.max_close_since_entry - pos.trail_atr_mult * atr_basis
                                pos.current_trail_price = max(pos.current_trail_price, new_trail, pos.sl_price)
                            if sb_low <= pos.current_trail_price:
                                exit_signal = ("TRAIL", pos.current_trail_price)
                                qty_to_exit = pos.qty
                                pnl_on_exit = (pos.current_trail_price - pos.entry_price) * qty_to_exit
                                if pos.current_trail_price > pos.entry_price:
                                    self.consecutive_losses_today = 0
                                break
            else:
                # Fallback to 1m low/high path
                if low <= pos.sl_price:
                    exit_signal = ("SL", pos.sl_price)
                    qty_to_exit = pos.qty
                    pnl_on_exit = (pos.sl_price - pos.entry_price) * qty_to_exit
                    self.consecutive_losses_today += 1

                if exit_signal is None:
                    if not pos.tp1_hit:
                        tp1_target = pos.tp1_price
                        if pos.strategy_tag == "B":
                            if orb_state.orb15_high is not None and orb_state.orb15_low is not None:
                                or_mid = (orb_state.orb15_high + orb_state.orb15_low) / 2
                                tp1_target = min(vwap, or_mid) if vwap > 0 else or_mid
                        if tp1_target is not None and high >= tp1_target:
                            exit_signal = ("TP1", tp1_target)
                            intended = math.floor(pos.qty * 0.7)
                            qty_to_exit = max(0.0, min(pos.qty, intended if intended > 0 else pos.qty))
                            if qty_to_exit > 0:
                                remaining = pos.qty - qty_to_exit
                                pnl_on_exit = (tp1_target - pos.entry_price) * qty_to_exit
                                pos.tp1_hit = True
                                pos.qty = remaining
                                pos.max_close_since_entry = close
                                atr_basis = atr_1m if pos.strategy_tag == "A" else atr_5m
                                pos.current_trail_price = close - pos.trail_atr_mult * atr_basis
                                self.consecutive_losses_today = 0
                            else:
                                exit_signal = None
                    else:
                        pos.max_close_since_entry = max(pos.max_close_since_entry, close)
                        atr_basis = atr_1m if pos.strategy_tag == "A" else atr_5m
                        if atr_basis > 1e-6:
                            new_trail = pos.max_close_since_entry - pos.trail_atr_mult * atr_basis
                            pos.current_trail_price = max(pos.current_trail_price, new_trail, pos.sl_price)
                        if low <= pos.current_trail_price:
                            exit_signal = ("TRAIL", pos.current_trail_price)
                            qty_to_exit = pos.qty
                            pnl_on_exit = (pos.current_trail_price - pos.entry_price) * qty_to_exit
                            if pos.current_trail_price > pos.entry_price:
                                self.consecutive_losses_today = 0

            if exit_signal and qty_to_exit > 0:
                reason, exit_price = exit_signal
                total_fee = self.exec_fee_per_share * qty_to_exit * (1 if reason == "TP1" else 2)
                self.realized_pnl_today += (pnl_on_exit - total_fee)

                atr_basis_for_exit = atr_1m if pos.strategy_tag == "A" else atr_5m
                asyncio.create_task(
                    self._generate_exit_intent(
                        symbol=symbol,
                        qty=qty_to_exit,
                        exit_price=exit_price,
                        reason=reason,
                        original_decision_id=pos.decision_id,
                        bar_close_ts_utc=bar["bar_close_ts_utc"],
                        atr_basis=atr_basis_for_exit,
                    )
                )

                if reason != "TP1" or pos.qty <= 1e-6:
                    positions_to_remove.append(i)

        if positions_to_remove:
            self.open_positions[symbol] = [p for idx, p in enumerate(positions) if idx not in positions_to_remove]

    async def _force_flatten_symbol(self, symbol: str):
        positions = self.open_positions.get(symbol, [])
        if not positions:
            return
        self.log.warning(f"Force Flatten {symbol} positions @ {self.schedule_force_flat_pt}")
        self.open_positions[symbol] = []

        price_snapshot = await self._get_current_price_snapshot(symbol)
        tasks: List[asyncio.Task] = []
        for pos in positions:
            last_px = (price_snapshot.last if price_snapshot and getattr(price_snapshot, "last", 0.0) > 0 else pos.entry_price)
            exit_price = last_px
            pnl_on_exit = (exit_price - pos.entry_price) * pos.qty
            total_fee = self.exec_fee_per_share * pos.qty * 2
            self.realized_pnl_today += (pnl_on_exit - total_fee)

            tasks.append(
                self._generate_exit_intent(
                    symbol=symbol,
                    qty=pos.qty,
                    exit_price=exit_price,
                    reason="FLAT",
                    original_decision_id=pos.decision_id,
                    bar_close_ts_utc=datetime.now(UTC_TZ),
                    atr_basis=atr_1m,
                )
            )
        if tasks:
            await asyncio.gather(*tasks)

    # ----------------- Entry/Exit intents -----------------

    async def _generate_entry_intent(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        sl_price: float,
        tp1_price: Optional[float],
        trail_atr_mult: float,
        atr_basis: float,
        strategy_tag: str,
        bar_close_ts_utc: datetime,
        **guardrail_data,
    ):
        if self._replay_mode:
            return

        current_equity = await self._get_current_equity()
        if current_equity <= 0:
            return

        try:
            risk_pct = self.risk_per_trade_pct_low if self.consecutive_losses_today >= 2 else self.risk_per_trade_pct
            risk_usd = current_equity * risk_pct
            stop_distance = abs(entry_price - sl_price)
            if stop_distance <= 1e-6:
                return
            shares = math.floor(risk_usd / stop_distance)
            if shares <= 0:
                return
        except Exception:
            self.log.exception(f"Sizing error {symbol}")
            return

        decision_id = make_decision_id()
        order_style = OrderStyle(kind="LMT", limit_px=round(entry_price, 2), tif="DAY")
        freshness = Freshness(ttl_ms=self.ctx.param("ttl_ms", 2000), dlb_ms=500)
        ts_end_us = int(bar_close_ts_utc.timestamp() * 1_000_000)

        # Cost estimate for entry (observability only)
        cost_est = None
        if COST_AVAILABLE:
            try:
                params = CostParams(
                    tick_size=float(self.ctx.param(f"tick_size.{symbol}", TICK_SIZE_DEFAULT)),
                    gamma_spread=float(self.ctx.param("gamma_spread", 0.10)),
                    slippage_bps_per_atr=float(self.exec_slippage_bps_per_atr or 0.0),
                    fee_per_share=float(self.exec_fee_per_share),
                )
                side_norm = "BUY" if str(side).upper() == "BUY" else "SELL"
                cost_est = entry_fill_est(side_norm, float(entry_price), float(atr_basis), params)
            except Exception:
                cost_est = None

        extras_payload = {
            "sl_price": round(sl_price, 2),
            "tp1_price": round(tp1_price, 2) if tp1_price is not None else None,
            "trail_atr_mult": trail_atr_mult,
            "atr_basis": round(atr_basis, 4),
            "strategy_tag": strategy_tag,
            "reason": "ENTRY",
            "intra_bar_priority": ["SL", "TP", "TRAIL"],
            "cost_model_hints": {
                "fee_per_share": self.exec_fee_per_share,
                "slippage_model": "atr_bps" if self.exec_slippage_bps_per_atr > 0 else "none",
                "slippage_bps_per_atr": self.exec_slippage_bps_per_atr,
            },
            "cost_estimate": cost_est,
            **guardrail_data,
        }

        envelope = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=symbol,
            ts_bar_end_us=ts_end_us,
            target_qty=float(shares),
            order_style=order_style,
            freshness=freshness,
            extras=extras_payload,
        )

        if getattr(self, "_debug_dry_run", False):
            try:
                self.log.info(
                    "ENTRY_PROBE",
                    symbol=symbol,
                    qty=float(shares),
                    px=float(entry_price),
                    sl=float(sl_price),
                    tp=float(tp1_price) if tp1_price is not None else None,
                    tag=strategy_tag,
                )
            except Exception:
                pass
            return

        try:
            await self.ctx.submit_intent(envelope)
            new_pos = PositionState(
                entry_price=entry_price,
                qty=shares,
                entry_ts_utc=datetime.now(UTC_TZ),
                sl_price=sl_price,
                tp1_price=tp1_price,
                trail_atr_mult=trail_atr_mult,
                max_close_since_entry=entry_price,
                current_trail_price=sl_price,
                strategy_tag=strategy_tag,
                decision_id=decision_id,
            )
            self.open_positions.setdefault(symbol, []).append(new_pos)
            self.daily_trade_count += 1
            self.log.info(f"ENTRY {symbol} qty={shares} @ {entry_price:.2f} SL={sl_price:.2f} id={decision_id[:8]}")
        except Exception:
            self.log.exception(f"Submit ENTRY failed {symbol}")

    async def _generate_exit_intent(
        self,
        symbol: str,
        qty: float,
        exit_price: float,
        reason: str,
        original_decision_id: str,
        bar_close_ts_utc: datetime,
        atr_basis: float = 0.0,
    ):
        if self._replay_mode:
            return
        if qty <= 1e-6:
            return

        decision_id = make_decision_id()
        protection_offset = float(self.ctx.param("execution_model.force_flat_protect_offset", 0.10))
        if reason == "FLAT":
            limit_px = round(max(0.01, exit_price - protection_offset), 2)
            order_style = OrderStyle(kind="LMT", limit_px=limit_px, tif="DAY")
        else:
            order_style = OrderStyle(kind="MKT", tif="GTC")

        freshness = Freshness(ttl_ms=5000, dlb_ms=1000)
        target_qty = -abs(float(qty))
        ts_end_us = int(bar_close_ts_utc.timestamp() * 1_000_000)

        # Cost estimate for exit (observability only)
        cost_est = None
        if COST_AVAILABLE:
            try:
                params = CostParams(
                    tick_size=float(self.ctx.param(f"tick_size.{symbol}", TICK_SIZE_DEFAULT)),
                    gamma_spread=float(self.ctx.param("gamma_spread", 0.10)),
                    slippage_bps_per_atr=float(self.exec_slippage_bps_per_atr or 0.0),
                    fee_per_share=float(self.exec_fee_per_share),
                )
                cost_est = exit_fill_est("SELL", float(exit_price), float(atr_basis), params, reason=reason)
            except Exception:
                cost_est = None

        extras_payload = {
            "exit_trigger_price": round(exit_price, 2),
            "reason": reason,
            "related_decision_id": original_decision_id,
            "protect_offset": (protection_offset if reason == "FLAT" else None),
            "intra_bar_priority": ["SL", "TP", "TRAIL"],
            "cost_model_hints": {
                "fee_per_share": self.exec_fee_per_share,
                "slippage_model": "atr_bps" if self.exec_slippage_bps_per_atr > 0 else "none",
                "slippage_bps_per_atr": self.exec_slippage_bps_per_atr,
            },
            "cost_estimate": cost_est,
        }

        envelope = DecisionEnvelope(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            symbol=symbol,
            ts_bar_end_us=ts_end_us,
            target_qty=target_qty,
            order_style=order_style,
            freshness=freshness,
            extras=extras_payload,
        )

        if getattr(self, "_debug_dry_run", False):
            try:
                self.log.info(
                    "EXIT_PROBE",
                    symbol=symbol,
                    qty=float(target_qty),
                    px=float(exit_price),
                    reason=reason,
                    rel=original_decision_id[:8],
                )
            except Exception:
                pass
            return

        try:
            await self.ctx.submit_intent(envelope)
            self.log.info(f"EXIT {symbol} qty={target_qty:.0f} ({reason}) ~{exit_price:.2f} rel={original_decision_id[:8]} new={decision_id[:8]}")
        except Exception:
            self.log.exception(f"Submit EXIT failed {symbol}")

    # ----------------- Persistence -----------------

    def _serialize_orb(self, orb: ORBTracker) -> Dict[str, Any]:
        return {
            "orb5_high": orb.orb5_high,
            "orb5_low": orb.orb5_low,
            "orb15_high": orb.orb15_high,
            "orb15_low": orb.orb15_low,
            "lowest_since_orb_break": orb.lowest_since_orb_break,
            "orb_low_broken": orb.orb_low_broken,
            "orb_low_break_bar_start_utc": orb.orb_low_break_bar_start_utc.isoformat() if orb.orb_low_break_bar_start_utc else None,
            "pending_break_bar_start_utc": orb.pending_break_bar_start_utc.isoformat() if orb.pending_break_bar_start_utc else None,
            "pending_lowest_since_break": orb.pending_lowest_since_break,
        }

    def _deserialize_orb(self, data: Dict[str, Any]) -> ORBTracker:
        orb = ORBTracker()
        orb.orb5_high = data.get("orb5_high")
        orb.orb5_low = data.get("orb5_low")
        orb.orb15_high = data.get("orb15_high")
        orb.orb15_low = data.get("orb15_low")
        orb.lowest_since_orb_break = data.get("lowest_since_orb_break")
        orb.orb_low_broken = bool(data.get("orb_low_broken", False))
        if data.get("orb_low_break_bar_start_utc"):
            orb.orb_low_break_bar_start_utc = datetime.fromisoformat(data["orb_low_break_bar_start_utc"]).replace(tzinfo=UTC_TZ)
        if data.get("pending_break_bar_start_utc"):
            orb.pending_break_bar_start_utc = datetime.fromisoformat(data["pending_break_bar_start_utc"]).replace(tzinfo=UTC_TZ)
        orb.pending_lowest_since_break = data.get("pending_lowest_since_break")
        return orb

    def _get_persistent_state(self) -> Dict[str, Any]:
        last_ts_map: Dict[str, datetime] = {}
        orb_ser: Dict[str, Dict[str, Any]] = {}
        for sym, data in self.symbol_states.items():
            ts = data.get("last_bar_ts_1m")
            if isinstance(ts, datetime):
                last_ts_map[sym] = ts
            # serialize ORB only
            orb_ser[sym] = self._serialize_orb(data["orb_state"]) if "orb_state" in data else {}

        state = {
            "ewma_state": {sym: data["ewma_state"] for sym, data in self.symbol_states.items() if "ewma_state" in data},
            "avg_trades_5d": self.avg_trades_5d,
            "trade_counts_last_5d": list(self.trade_counts_last_5d),
            "trading_days_history": list(self.trading_days_history),
            "last_closed_day_pt": self.last_closed_day_pt,
            "consecutive_losses_today": self.consecutive_losses_today,
            "daily_trade_count": self.daily_trade_count,
            "realized_pnl_today": self.realized_pnl_today,
            "open_positions": {symbol: [pos.__dict__ for pos in positions] for symbol, positions in self.open_positions.items() if positions},
            "last_processed_ts_1m": last_ts_map,
            "rng_seed": self.rng_seed,
            # current_bars removed in v2.1 (engine-driven)
            "orb_state": orb_ser,
            "save_ts_ms": int(time.time() * 1000),
        }
        return state

    def _restore_persistent_state(self, snapshot: Dict[str, Any]) -> Dict[str, datetime]:
        last_processed_ts_map: Dict[str, datetime] = {}

        if isinstance(snapshot.get("ewma_state"), dict):
            for symbol, ewma_data in snapshot["ewma_state"].items():
                if symbol in self.symbol_states and isinstance(ewma_data, dict):
                    self.symbol_states[symbol]["ewma_state"] = ewma_data

        self.avg_trades_5d = snapshot.get("avg_trades_5d", self.avg_trades_5d)
        self.trade_counts_last_5d = deque(snapshot.get("trade_counts_last_5d", []), maxlen=5)

        hist = snapshot.get("trading_days_history", [])
        if isinstance(hist, list):
            self.trading_days_history.clear()
            for item in hist[-10:]:
                if isinstance(item, dict) and "date" in item and "trades" in item:
                    self.trading_days_history.append({"date": str(item["date"]), "trades": int(item["trades"])})
        self.last_closed_day_pt = snapshot.get("last_closed_day_pt")

        self._recompute_avg_trades_5d()

        self.consecutive_losses_today = snapshot.get("consecutive_losses_today", 0)
        self.daily_trade_count = snapshot.get("daily_trade_count", 0)
        self.realized_pnl_today = snapshot.get("realized_pnl_today", 0.0)
        if "rng_seed" in snapshot:
            try:
                self.rng_seed = int(snapshot["rng_seed"])
            except Exception:
                pass

        # Positions
        self.open_positions = {symbol: [] for symbol in self.universe}
        if isinstance(snapshot.get("open_positions"), dict):
            for symbol, pos_list in snapshot["open_positions"].items():
                if symbol not in self.universe:
                    continue
                self.open_positions[symbol] = []
                if not isinstance(pos_list, list):
                    continue
                for pos_data in pos_list:
                    if not isinstance(pos_data, dict):
                        continue
                    try:
                        ts_raw = pos_data.get("entry_ts_utc")
                        if isinstance(ts_raw, str):
                            pos_data["entry_ts_utc"] = datetime.fromisoformat(ts_raw).replace(tzinfo=UTC_TZ)
                        elif isinstance(ts_raw, (int, float)):
                            pos_data["entry_ts_utc"] = datetime.fromtimestamp(ts_raw / 1000.0, tz=UTC_TZ)
                        elif not isinstance(ts_raw, datetime):
                            raise ValueError("Invalid datetime format")
                        if pos_data["entry_ts_utc"].tzinfo is None:
                            pos_data["entry_ts_utc"] = UTC_TZ.localize(pos_data["entry_ts_utc"])
                        pos_data.setdefault("tp1_price", None)
                        pos_data.setdefault("tp1_hit", False)
                        pos_data.setdefault("trail_atr_mult", 1.0)
                        pos_data.setdefault("max_close_since_entry", pos_data.get("entry_price", 0.0))
                        pos_data.setdefault("current_trail_price", pos_data.get("sl_price", 0.0))
                        pos_data.setdefault("strategy_tag", "")
                        pos_data.setdefault("decision_id", "")
                        required = ["entry_price", "qty", "entry_ts_utc", "sl_price"]
                        if all(k in pos_data for k in required):
                            self.open_positions[symbol].append(PositionState(**pos_data))
                    except Exception:
                        self.log.exception(f"Restore position error {symbol}")

        # last processed bar ts
        if isinstance(snapshot.get("last_processed_ts_1m"), dict):
            for symbol, ts_obj in snapshot["last_processed_ts_1m"].items():
                if symbol in self.universe:
                    try:
                        if isinstance(ts_obj, datetime):
                            last_processed_ts_map[symbol] = ts_obj
                        elif isinstance(ts_obj, str):
                            dt_obj = datetime.fromisoformat(ts_obj).replace(tzinfo=UTC_TZ)
                            last_processed_ts_map[symbol] = dt_obj
                        elif isinstance(ts_obj, (int, float)):
                            dt_obj = datetime.fromtimestamp(ts_obj / 1000.0, tz=UTC_TZ)
                            last_processed_ts_map[symbol] = dt_obj
                    except Exception:
                        self.log.exception(f"Parse last_processed_ts failed {symbol}")

        # current_bars removed in v2.1 (engine-driven)

        orb_map = snapshot.get("orb_state", {})
        if isinstance(orb_map, dict):
            for symbol, orb_ser in orb_map.items():
                if symbol in self.universe and isinstance(orb_ser, dict):
                    try:
                        self.symbol_states[symbol]["orb_state"] = self._deserialize_orb(orb_ser)
                    except Exception:
                        self.log.exception(f"Restore ORB failed {symbol}")

        return last_processed_ts_map

    def save_state(self):
        try:
            current_state = self._get_persistent_state()
            self.state_store.save(state_dict=current_state)
        except Exception:
            self.log.exception("Save state failed.")

    # ----------------- Recovery -----------------

    # Tick replay removed in v2.1 engine-driven mode; bar replay is sufficient

    async def _replay_bars_for_recovery(self, last_processed_ts_map: Dict[str, datetime]):
        if not CLICKHOUSE_AVAILABLE:
            return
        self._ensure_clickhouse_client()
        if not self.ch or not last_processed_ts_map:
            return
        start_utc = min(last_processed_ts_map.values()) - timedelta(minutes=1)
        end_utc = datetime.now(UTC_TZ) - timedelta(seconds=10)
        if start_utc >= end_utc:
            return
        bars_1m_table = self.ctx.param("db_tables.bars_1m", "trading.bars_1m")
        self._replay_mode = True
        try:
            symbols = list(last_processed_ts_map.keys())
            self.log.info(f"Bar replay 1m from {start_utc} to {end_utc}")
            query = f"""
                SELECT ts_start AS ts_utc, open, high, low, close, volume, symbol
                FROM {bars_1m_table} FINAL
                WHERE symbol IN %(symbols)s
                  AND ts_start > %(start_ts)s
                  AND ts_start < %(end_ts)s
                ORDER BY ts_utc ASC, symbol ASC
            """
            params = {"symbols": symbols, "start_ts": start_utc, "end_ts": end_utc}
            rows = self.ch.query(query, parameters=params)  # type: ignore
            for row in rows:
                symbol = row["symbol"]
                ts_utc = row["ts_utc"].replace(tzinfo=UTC_TZ) if row["ts_utc"].tzinfo is None else row["ts_utc"]
                bar_data = {
                    "ts": ts_utc,
                    "ts_utc": ts_utc,
                    "end_ts_utc": ts_utc + timedelta(minutes=1),
                    "bar_close_ts_utc": ts_utc + timedelta(minutes=1),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "vwap": float(row.get("vwap", 0.0) or 0.0),
                    "symbol": symbol,
                    "timeframe": "1m",
                }
                await self._apply_bar_effects(symbol, "1m", bar_data, replay=True)
        except Exception:
            self.log.exception("Bar replay error.")
        finally:
            self._replay_mode = False

    # ----------------- External state helpers -----------------

    async def _get_index_state(self, symbol: str) -> Optional[MarketIndexState]:
        index_symbol = "QQQ" if symbol in ["TSLA", "NVDA", "AAPL", "AMD", "META", "QQQ"] else "SPY"
        try:
            if hasattr(self.ctx, "get_market_state"):
                index_data = await self.ctx.get_market_state(index_symbol)
                if index_data and hasattr(index_data, "close") and hasattr(index_data, "vwap"):
                    return {"symbol": index_symbol, "close": index_data.close, "vwap": index_data.vwap}
            elif hasattr(self.ctx, "get_price") and hasattr(self.ctx, "get_minute_stats"):
                price_snapshot = self.ctx.get_price(index_symbol)
                minute_stats = self.ctx.get_minute_stats(index_symbol)
                if price_snapshot and minute_stats and hasattr(minute_stats, "vwap"):
                    close_val = getattr(price_snapshot, "last", None)
                    vwap_val = getattr(minute_stats, "vwap", None)
                    if isinstance(close_val, (int, float)) and isinstance(vwap_val, (int, float)):
                        return {"symbol": index_symbol, "close": float(close_val), "vwap": float(vwap_val)}
            # Fallback: if we at least have last price, use it as both close and vwap to allow gating to proceed
            if hasattr(self.ctx, "get_price"):
                snap = self.ctx.get_price(index_symbol)
                if snap and hasattr(snap, "last") and isinstance(getattr(snap, "last"), (int, float)):
                    try:
                        self.log.info("INDEX_FALLBACK", index=index_symbol, last=float(snap.last))
                    except Exception:
                        pass
                    return {"symbol": index_symbol, "close": float(snap.last), "vwap": float(snap.last)}
            return None
        except Exception:
            self.log.exception(f"Get index state failed {index_symbol}")
            return None

    async def _get_current_equity(self) -> float:
        try:
            if hasattr(self.ctx, "portfolio") and callable(self.ctx.portfolio):
                p = self.ctx.portfolio()
                if p and hasattr(p, "equity"):
                    eq = p.equity
                    if isinstance(eq, (int, float)) and eq > 0:
                        return float(eq)
                if p and hasattr(p, "get_equity") and callable(p.get_equity):
                    eq = p.get_equity()
                    if eq and isinstance(eq, (int, float)) and eq > 0:
                        return float(eq)
        except Exception:
            self.log.exception("_get_current_equity failed.")
        return float(self.ctx.param("equity.default", DEFAULT_EQUITY))

    def _get_sync_price_snapshot(self, symbol: str) -> Optional[PriceSnapshot]:
        try:
            snap = self.ctx.get_price(symbol) if hasattr(self.ctx, "get_price") else None
            if snap:
                return snap
            # Fallback to last known L1 from features
            cached = self._l1_last.get(symbol)
            if cached:
                class _Snap:  # lightweight proxy
                    bid: Optional[float] = None
                    ask: Optional[float] = None
                    last: Optional[float] = None
                proxy = _Snap()
                proxy.bid = cached.get("bid")
                proxy.ask = cached.get("ask")
                proxy.last = cached.get("last")
                return proxy  # type: ignore
            return None
        except Exception:
            self.log.exception(f"Price snapshot failed {symbol}")
            return None

    async def _get_current_price_snapshot(self, symbol: str) -> Optional[PriceSnapshot]:
        try:
            if hasattr(self.ctx, "get_price"):
                return self.ctx.get_price(symbol)
        except Exception:
            self.log.exception(f"Async price snapshot failed {symbol}")
        return None

    # ----------------- Daily reset & shutdown -----------------

    async def _reset_daily_state(self, new_date_pt: date):
        prev_day_str = self.today_date_pt.isoformat() if self.today_date_pt else None
        if prev_day_str and prev_day_str != self.last_closed_day_pt:
            self.trading_days_history.append({"date": prev_day_str, "trades": int(self.daily_trade_count)})
            self.trade_counts_last_5d.append(int(self.daily_trade_count))
            self.last_closed_day_pt = prev_day_str

        self.log.info(f"Reset daily state for {new_date_pt}")
        self.consecutive_losses_today = 0
        self.daily_trade_count = 0
        self.realized_pnl_today = 0.0
        self.current_bar_index = 0
        self.today_date_pt = new_date_pt
        self._daily_stop_logged = False
        self._max_loss_logged = False

        # reload context and reset per-symbol states
        initial_context: Dict[str, Dict[str, Any]] = {}
        tasks = {sym: asyncio.create_task(self._get_symbol_historical_context(sym)) for sym in self.universe}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for i, symbol in enumerate(self.universe):
            res = results[i]
            if isinstance(res, Exception):
                ctxi = {"prev_close": 0.0, "prev_atr_1m": 0.1, "prev_atr_5m": 0.5, "median_vol_60m": 10000.0}
            else:
                prev_close, prev_atr_1m, prev_atr_5m, median_vol = res
                ctxi = {
                    "prev_close": prev_close,
                    "prev_atr_1m": prev_atr_1m or 0.1,
                    "prev_atr_5m": prev_atr_5m or 0.5,
                    "median_vol_60m": median_vol or 10000.0,
                }
            initial_context[symbol] = ctxi

            state = self.symbol_states[symbol]
            state["vwap_calculator"] = (0.0, 0.0)
            state["atr_calculator"] = {
                "1m": ATRState(last_close=ctxi["prev_close"], prev_atr=ctxi["prev_atr_1m"], current_atr=ctxi["prev_atr_1m"]),
                "5m": ATRState(last_close=ctxi["prev_close"], prev_atr=ctxi["prev_atr_5m"], current_atr=ctxi["prev_atr_5m"]),
            }
            state["orb_state"] = ORBTracker()
            for key, val in list(state["rolling_windows"].items()):
                if isinstance(val, deque):
                    val.clear()
                else:
                    state["rolling_windows"][key] = []
            state["last_1m_bars"].clear()
            state["regime"] = "RANGE"
            state["last_entry_bar_start_utc"] = None
            state["last_bar_ts_1m"] = None
            state["flattened_today"] = False
            state["median_vol_60m"] = ctxi["median_vol_60m"]

        # clear positions (no overnight)
        if any(self.open_positions.values()):
            self.log.error("Positions found at new day start; clearing.")
            self.open_positions = {symbol: [] for symbol in self.universe}

        self._apply_frequency_adaptation()
        self.save_state()

    def _apply_frequency_adaptation(self):
        self._recompute_avg_trades_5d()
        avg_trades = self.avg_trades_5d
        band_mult = 1.0
        k_mult = 1.0
        adjust = False

        if avg_trades < self.adaptive_target_trades_low:
            band_mult = 1.0 - self.adaptive_adjust_step_pct
            k_mult = 1.0 + self.adaptive_adjust_step_pct
            adjust = True
            self.log.info(f"Adapt: relax thresholds (avg={avg_trades:.1f})")
        elif avg_trades > self.adaptive_target_trades_high:
            band_mult = 1.0 + self.adaptive_adjust_step_pct
            k_mult = 1.0 - self.adaptive_adjust_step_pct
            adjust = True
            self.log.info(f"Adapt: tighten thresholds (avg={avg_trades:.1f})")

        if adjust:
            for symbol in self.universe:
                if symbol in self.symbol_states:
                    ewma = self.symbol_states[symbol]["ewma_state"]
                    ewma["q80"] *= band_mult
                    ewma["k"] *= k_mult

    def _recompute_avg_trades_5d(self):
        last5 = list(self.trading_days_history)[-5:]
        if last5:
            self.avg_trades_5d = float(np.mean([d.get("trades", 0) for d in last5]))
        else:
            if len(self.trade_counts_last_5d) > 0:
                self.avg_trades_5d = float(np.mean(list(self.trade_counts_last_5d)))
            else:
                self.avg_trades_5d = (self.adaptive_target_trades_low + self.adaptive_target_trades_high) / 2

    async def on_shutdown(self) -> None:
        self.log.info("Shutting down...")
        # No Kafka/tick loops to stop in v2.1
        try:
            self.save_state()
        except Exception:
            self.log.exception("Final save failed.")
        self.log.info("Shutdown complete.")
