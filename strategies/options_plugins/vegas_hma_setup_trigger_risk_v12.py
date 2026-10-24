from __future__ import annotations

import hashlib
import json
import math
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from libs.strategy.components.indicator_lib import IndicatorLib
from libs.strategy.base import StrategyAlgorithm, StrategyContext, StrategyParams
from strategies.options_kernel.execution_profile import build_execution_profile_from_extras
from strategies.options_kernel.plugin import BaseOptionPlugin
from strategies.options_kernel.types import (
    EntryPlan,
    ExecutionProfile,
    ExitPlan,
    IBKRTrailingStopOrderSpec,
    PluginRuntimeState,
    Selection,
    Signal,
    UnderlyingTriggeredOptionExitPlan,
)
from strategies.options_plugins.ma_1dte_reject_shared import select_option_quote_for_underlying


ET = ZoneInfo("America/New_York")
STRATEGY_KEY = "vegas_hma_setup_trigger_risk_v12"


@dataclass(frozen=True)
class VegasHMAConfig:
    fast_len: int = 12
    med_len: int = 24
    atr_len: int = 14
    atr_mult: float = 1.80
    chop_body_mult: float = 0.30
    slope_mult: float = 0.05
    max_dist_from_fhma_atr: float = 0.80
    pre_run_lookback: int = 18
    max_pre_run_atr: float = 1.60
    pullback_lookback: int = 8
    pullback_touch_atr: float = 0.35
    setup_max_bars: int = 10
    max_setup_risk_atr: float = 1.20
    quality_len: int = 120
    struct_stop_buffer_atr: float = 0.10
    trail_start_atr: float = 0.80
    trail_atr_mult: float = 0.85
    breakeven_trigger_atr: float = 0.70
    breakeven_plus_atr: float = 0.05
    use_fast_hma_exit: bool = True
    use_tunnel_fail_exit: bool = False
    trailing_mode: str = "percent"
    trailing_percent: float = 1.5
    trailing_amount: Optional[float] = None
    use_ibkr_native_trailing_stop: bool = True
    entry_start: str = "09:35:00"
    entry_end: str = "15:45:00"
    underlying_symbol: str = "SPY"
    contracts: int = 1
    signal_only: bool = True
    enabled: bool = True
    daily_max_trades: Optional[int] = None
    max_loss_per_trade: Optional[float] = None
    debug: bool = False

    @classmethod
    def from_mapping(cls, data: Dict[str, Any] | None) -> "VegasHMAConfig":
        base = asdict(cls())
        for key, value in dict(data or {}).items():
            if key in base:
                base[key] = value
        return cls(**base)

    def snapshot(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["config_version"] = self.version()
        return payload

    def version(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class VegasBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class VegasState:
    long_setup_active: bool = False
    short_setup_active: bool = False
    long_setup_low: Optional[float] = None
    long_setup_high: Optional[float] = None
    long_setup_start_index: Optional[int] = None
    short_setup_low: Optional[float] = None
    short_setup_high: Optional[float] = None
    short_setup_start_index: Optional[int] = None
    position_side: Optional[str] = None
    entry_reference_price: Optional[float] = None
    initial_underlying_stop: Optional[float] = None
    long_stop: Optional[float] = None
    short_stop: Optional[float] = None
    internal_long_stop: Optional[float] = None
    internal_short_stop: Optional[float] = None
    long_peak: Optional[float] = None
    short_trough: Optional[float] = None
    long_entry_index: Optional[int] = None
    short_entry_index: Optional[int] = None
    last_signal_index: Optional[int] = None
    last_signal_ts: Optional[datetime] = None
    last_bad_break_index: Optional[int] = None
    trades_today: int = 0
    current_day: Optional[str] = None


@dataclass(frozen=True)
class VegasDecision:
    action: str
    signal: Optional[str] = None
    side: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    order_intent: Optional[Dict[str, Any]] = None
    exit_plan: Optional[UnderlyingTriggeredOptionExitPlan] = None


@dataclass
class _VegasMinuteBuilder:
    minute_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def ingest(self, bar: Dict[str, Any]) -> None:
        close = float(bar.get("close") or self.close)
        self.high = max(self.high, float(bar.get("high") or close))
        self.low = min(self.low, float(bar.get("low") or close))
        self.close = close
        self.volume += float(bar.get("volume") or 0.0)

    def finalize(self) -> Dict[str, Any]:
        return {
            "ts": self.minute_start + timedelta(minutes=1),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
        }


class VegasHMASetupTriggerRiskV12Core:
    def __init__(self, config: VegasHMAConfig | None = None) -> None:
        self.config = config or VegasHMAConfig()
        self.state = VegasState()
        self._bars: List[VegasBar] = []

    def reset(self) -> None:
        self.state = VegasState()
        self._bars = []

    @property
    def bars(self) -> List[VegasBar]:
        return list(self._bars)

    def preload_history(self, bars: List[VegasBar]) -> None:
        self._bars = list(bars or [])
        self._clear_setups()

    def on_bar(self, bar: VegasBar, *, strategy_id: str = STRATEGY_KEY) -> VegasDecision:
        if not self.config.enabled:
            return VegasDecision(action="NONE", metadata={"disabled": True})
        self._reset_day_if_needed(bar.ts)
        self._bars.append(bar)
        index = len(self._bars) - 1
        ind = self._indicators()
        meta = self._metadata(bar, ind)
        if not ind.get("warmed") or float(ind.get("atr") or 0.0) <= 0.0:
            return VegasDecision(action="NONE", metadata={**meta, "block_reason": "WARMUP_OR_ATR_UNAVAILABLE"})
        if not meta["in_session"] and not self._in_position():
            self._clear_setups()
            return VegasDecision(action="NONE", metadata={**meta, "block_reason": "OUT_OF_SESSION"})
        decision = self._evaluate_setups_and_entry(bar, index, ind, meta, strategy_id=strategy_id)
        return decision

    def _reset_day_if_needed(self, ts: datetime) -> None:
        day = ts.astimezone(ET).date().isoformat()
        if self.state.current_day != day:
            self.state.current_day = day
            self.state.trades_today = 0
            self._clear_setups()
            if not self._in_position():
                self.state.position_side = None

    def _in_position(self) -> bool:
        return self.state.position_side in {"LONG_CALL_SIGNAL", "SHORT_PUT_SIGNAL", "LONG_UNDERLYING_DIRECTION", "SHORT_UNDERLYING_DIRECTION"}

    def _clear_setups(self) -> None:
        self.state.long_setup_active = False
        self.state.short_setup_active = False
        self.state.long_setup_low = None
        self.state.long_setup_high = None
        self.state.long_setup_start_index = None
        self.state.short_setup_low = None
        self.state.short_setup_high = None
        self.state.short_setup_start_index = None

    def _indicators(self) -> Dict[str, Any]:
        closes = [b.close for b in self._bars]
        ema144 = self._ema_series(closes, 144)
        ema169 = self._ema_series(closes, 169)
        fast_hma = IndicatorLib.hma_series(closes, self.config.fast_len)
        med_hma = IndicatorLib.hma_series(closes, self.config.med_len)
        atr_series = self._atr_series(self._bars, self.config.atr_len)
        idx = len(self._bars) - 1
        body_scores = []
        for pos, b in enumerate(self._bars):
            atr = atr_series[pos]
            body_scores.append(abs(b.close - b.open) / atr if atr is not None and atr > 0 else 0.0)
        threshold = None
        if len(body_scores) >= self.config.quality_len:
            threshold = self._percentile_nearest_rank(body_scores[-self.config.quality_len :], 55.0)
        atr = atr_series[idx] if idx < len(atr_series) else None
        fast = fast_hma[idx] if idx < len(fast_hma) else None
        med = med_hma[idx] if idx < len(med_hma) else None
        e144 = ema144[idx] if idx < len(ema144) else None
        e169 = ema169[idx] if idx < len(ema169) else None
        e144_prev5 = ema144[idx - 5] if idx >= 5 else None
        warmed = all(v is not None for v in (atr, fast, med, e144, e169, e144_prev5))
        return {
            "atr": atr,
            "atr_prev": atr_series[idx - 1] if idx >= 1 else None,
            "fast_hma": fast,
            "fast_hma_prev": fast_hma[idx - 1] if idx >= 1 else None,
            "med_hma": med,
            "med_hma_prev": med_hma[idx - 1] if idx >= 1 else None,
            "ema144": e144,
            "ema144_prev": ema144[idx - 1] if idx >= 1 else None,
            "ema169": e169,
            "ema169_prev": ema169[idx - 1] if idx >= 1 else None,
            "ema144_prev5": e144_prev5,
            "tunnel_top_prev": max(ema144[idx - 1], ema169[idx - 1]) if idx >= 1 and ema144[idx - 1] is not None and ema169[idx - 1] is not None else None,
            "tunnel_bottom_prev": min(ema144[idx - 1], ema169[idx - 1]) if idx >= 1 and ema144[idx - 1] is not None and ema169[idx - 1] is not None else None,
            "tunnel_top": max(e144, e169) if e144 is not None and e169 is not None else None,
            "tunnel_bottom": min(e144, e169) if e144 is not None and e169 is not None else None,
            "body_quality_threshold": threshold,
            "warmed": bool(warmed),
        }

    @staticmethod
    def _ema_series(values: List[float], period: int) -> List[Optional[float]]:
        out: List[Optional[float]] = []
        alpha = 2.0 / (period + 1.0)
        ema: Optional[float] = None
        for value in values:
            x = float(value)
            ema = x if ema is None else alpha * x + (1.0 - alpha) * ema
            out.append(ema)
        return out

    @staticmethod
    def _atr_series(bars: List[VegasBar], period: int) -> List[Optional[float]]:
        out: List[Optional[float]] = []
        atr: Optional[float] = None
        prev_close: Optional[float] = None
        trs: List[float] = []
        for bar in bars:
            tr = bar.high - bar.low if prev_close is None else max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
            trs.append(float(tr))
            if len(trs) < period:
                out.append(None)
                prev_close = bar.close
                continue
            if len(trs) == period:
                atr = math.fsum(trs) / float(period)
            else:
                atr = (float(atr) * (period - 1) + tr) / period
            prev_close = bar.close
            out.append(atr)
        return out

    @staticmethod
    def _percentile(values: List[float], pct: float) -> float:
        ordered = sorted(float(v) for v in values)
        if not ordered:
            return 0.0
        rank = (len(ordered) - 1) * pct / 100.0
        lo = int(math.floor(rank))
        hi = int(math.ceil(rank))
        if lo == hi:
            return ordered[lo]
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)

    @staticmethod
    def _percentile_nearest_rank(values: List[float], pct: float) -> float:
        ordered = sorted(float(v) for v in values)
        if not ordered:
            return 0.0
        rank = int(math.ceil((pct / 100.0) * len(ordered)))
        rank = min(max(rank, 1), len(ordered))
        return ordered[rank - 1]

    def _metadata(self, bar: VegasBar, ind: Dict[str, Any]) -> Dict[str, Any]:
        atr = float(ind.get("atr") or 0.0)
        rng = max(0.0, bar.high - bar.low)
        body_size = abs(bar.close - bar.open)
        close_loc = (bar.close - bar.low) / rng if rng > 0 else 0.5
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        body_score = body_size / atr if atr > 0 else 0.0
        threshold = ind.get("body_quality_threshold")
        body_quality_ok = True if threshold is None else body_score >= float(threshold)
        in_session = self._in_session(bar.ts)
        tunnel_buffer = atr * 0.5
        fast = ind.get("fast_hma")
        fast_prev = ind.get("fast_hma_prev")
        med = ind.get("med_hma")
        med_prev = ind.get("med_hma_prev")
        ema144 = ind.get("ema144")
        ema144_prev5 = ind.get("ema144_prev5")
        tunnel_top = ind.get("tunnel_top")
        tunnel_bottom = ind.get("tunnel_bottom")
        closes = self._bars
        previous_bar = closes[-2] if len(closes) >= 2 else None
        tunnel_top_prev = ind.get("tunnel_top_prev")
        tunnel_bottom_prev = ind.get("tunnel_bottom_prev")
        atr_prev = float(ind.get("atr_prev") or atr)
        tunnel_buffer_prev = atr_prev * 0.5
        fast_rising = fast is not None and fast_prev is not None and fast > fast_prev
        fast_falling = fast is not None and fast_prev is not None and fast < fast_prev
        med_not_falling = med is not None and med_prev is not None and med >= med_prev
        med_not_rising = med is not None and med_prev is not None and med <= med_prev
        ema_slope5 = (ema144 - ema144_prev5) if ema144 is not None and ema144_prev5 is not None else 0.0
        long_bias = bool(
            in_session
            and atr > 0
            and tunnel_top is not None
            and bar.close > tunnel_top + tunnel_buffer
            and fast_rising
            and med_not_falling
            and ema_slope5 > atr * self.config.slope_mult
        )
        short_bias = bool(
            in_session
            and atr > 0
            and tunnel_bottom is not None
            and bar.close < tunnel_bottom - tunnel_buffer
            and fast_falling
            and med_not_rising
            and ema_slope5 < -atr * self.config.slope_mult
        )
        dist_from_fhma_long = (bar.close - float(fast)) / atr if atr > 0 and fast is not None else 0.0
        dist_from_fhma_short = (float(fast) - bar.close) / atr if atr > 0 and fast is not None else 0.0
        not_too_extended_long = dist_from_fhma_long <= self.config.max_dist_from_fhma_atr
        not_too_extended_short = dist_from_fhma_short <= self.config.max_dist_from_fhma_atr
        is_valid_up_slope = ema_slope5 > atr * self.config.slope_mult if atr > 0 else False
        is_valid_dn_slope = ema_slope5 < -atr * self.config.slope_mult if atr > 0 else False
        first_reclaim_long = bool(
            tunnel_top is not None
            and tunnel_top_prev is not None
            and previous_bar is not None
            and bar.close > float(tunnel_top) + tunnel_buffer
            and previous_bar.close <= float(tunnel_top_prev) + tunnel_buffer_prev
        )
        first_reclaim_short = bool(
            tunnel_bottom is not None
            and tunnel_bottom_prev is not None
            and previous_bar is not None
            and bar.close < float(tunnel_bottom) - tunnel_buffer
            and previous_bar.close >= float(tunnel_bottom_prev) - tunnel_buffer_prev
        )
        pre_run_bars = self._bars[-max(1, int(self.config.pre_run_lookback)) :]
        pre_run_long = (bar.close - min(b.low for b in pre_run_bars)) / atr if atr > 0 and pre_run_bars else 0.0
        pre_run_short = (max(b.high for b in pre_run_bars) - bar.close) / atr if atr > 0 and pre_run_bars else 0.0
        first_reclaim_long_ok = first_reclaim_long and not_too_extended_long and pre_run_long <= self.config.max_pre_run_atr
        first_reclaim_short_ok = first_reclaim_short and not_too_extended_short and pre_run_short <= self.config.max_pre_run_atr
        pullback_bars = self._bars[-max(1, int(self.config.pullback_lookback)) :]
        long_pullback_seen = bool(fast is not None and pullback_bars and min(b.low for b in pullback_bars) <= float(fast) + atr * self.config.pullback_touch_atr)
        short_pullback_seen = bool(fast is not None and pullback_bars and max(b.high for b in pullback_bars) >= float(fast) - atr * self.config.pullback_touch_atr)
        long_resume_candle = bar.close > bar.open and close_loc >= 0.55 and body_quality_ok
        short_resume_candle = bar.close < bar.open and close_loc <= 0.45 and body_quality_ok
        long_quality_ok = first_reclaim_long_ok or (long_pullback_seen and long_resume_candle and not_too_extended_long)
        short_quality_ok = first_reclaim_short_ok or (short_pullback_seen and short_resume_candle and not_too_extended_short)
        prev_highs = [b.high for b in self._bars[-3:-1]]
        prev_lows = [b.low for b in self._bars[-3:-1]]
        long_breakout = bool(prev_highs and bar.high > max(prev_highs))
        short_breakout = bool(prev_lows and bar.low < min(prev_lows))
        return {
            "bias_state": {"long_bias": long_bias, "short_bias": short_bias, "ema144_slope_5": ema_slope5},
            "setup_state": self._setup_state_payload(),
            "in_session": in_session,
            "atr": atr,
            "fast_hma": fast,
            "med_hma": med,
            "ema144": ema144,
            "ema169": ind.get("ema169"),
            "tunnel_top": tunnel_top,
            "tunnel_bottom": tunnel_bottom,
            "tunnel_buffer": tunnel_buffer,
            "body_size": body_size,
            "bar_range": rng,
            "close_loc": close_loc,
            "upper_wick": upper_wick,
            "lower_wick": lower_wick,
            "body_score": body_score,
            "body_quality_threshold": threshold,
            "is_valid_body": body_size > atr * self.config.chop_body_mult if atr > 0 else False,
            "body_quality_ok": body_quality_ok,
            "dist_from_fast_hma_long_atr": dist_from_fhma_long,
            "dist_from_fast_hma_short_atr": dist_from_fhma_short,
            "not_too_extended_long": not_too_extended_long,
            "not_too_extended_short": not_too_extended_short,
            "is_valid_up_slope": is_valid_up_slope,
            "is_valid_dn_slope": is_valid_dn_slope,
            "first_reclaim_long": first_reclaim_long,
            "first_reclaim_short": first_reclaim_short,
            "pre_run_long_atr": pre_run_long,
            "pre_run_short_atr": pre_run_short,
            "first_reclaim_long_ok": first_reclaim_long_ok,
            "first_reclaim_short_ok": first_reclaim_short_ok,
            "long_pullback_seen": long_pullback_seen,
            "short_pullback_seen": short_pullback_seen,
            "long_resume_candle": long_resume_candle,
            "short_resume_candle": short_resume_candle,
            "long_quality_ok": long_quality_ok,
            "short_quality_ok": short_quality_ok,
            "long_breakout": long_breakout,
            "short_breakout": short_breakout,
            "config_version": self.config.version(),
            "config_snapshot": self.config.snapshot() if self.config.debug else {"config_version": self.config.version()},
        }

    def _setup_state_payload(self) -> Dict[str, Any]:
        return {
            "long_setup_active": self.state.long_setup_active,
            "long_setup_low": self.state.long_setup_low,
            "long_setup_high": self.state.long_setup_high,
            "short_setup_active": self.state.short_setup_active,
            "short_setup_low": self.state.short_setup_low,
            "short_setup_high": self.state.short_setup_high,
        }

    def _in_session(self, ts: datetime) -> bool:
        local = ts.astimezone(ET) if ts.tzinfo is not None else ts.replace(tzinfo=ET)
        start = time.fromisoformat(self.config.entry_start)
        end = time.fromisoformat(self.config.entry_end)
        return local.weekday() < 5 and start <= local.time() <= end

    def _evaluate_setups_and_entry(self, bar: VegasBar, index: int, ind: Dict[str, Any], meta: Dict[str, Any], strategy_id: str) -> VegasDecision:
        if self.state.last_signal_index == index or self.state.last_signal_ts == bar.ts:
            return VegasDecision(action="NONE", metadata={**meta, "block_reason": "DUPLICATE_BAR"})
        atr = float(meta["atr"])
        tunnel_bottom = float(meta["tunnel_bottom"])
        tunnel_top = float(meta["tunnel_top"])
        tunnel_buffer = float(meta["tunnel_buffer"])
        fast_hma = float(meta["fast_hma"])
        long_trigger = bool(
            meta["in_session"]
            and fast_hma > float(ind.get("fast_hma_prev") or fast_hma)
            and bool(meta["long_breakout"])
            and bar.close > tunnel_top + tunnel_buffer
            and bool(meta["is_valid_body"])
            and bool(meta["is_valid_up_slope"])
            and bool(meta["long_quality_ok"])
        )
        short_trigger = bool(
            meta["in_session"]
            and fast_hma < float(ind.get("fast_hma_prev") or fast_hma)
            and bool(meta["short_breakout"])
            and bar.close < tunnel_bottom - tunnel_buffer
            and bool(meta["is_valid_body"])
            and bool(meta["is_valid_dn_slope"])
            and bool(meta["short_quality_ok"])
        )
        self._update_v11_stops(bar, atr, long_trigger=long_trigger, short_trigger=short_trigger)
        metadata = {
            **meta,
            "long_trigger": long_trigger,
            "short_trigger": short_trigger,
            "long_stop": self.state.long_stop,
            "short_stop": self.state.short_stop,
            "distance_from_fast_hma_atr": meta["dist_from_fast_hma_long_atr"] if long_trigger else meta["dist_from_fast_hma_short_atr"] if short_trigger else None,
        }
        can_long = long_trigger and self.state.position_side != "LONG_CALL_SIGNAL"
        can_short = short_trigger and self.state.position_side != "SHORT_PUT_SIGNAL"
        if can_long:
            return self._enter_v11_long(bar, index, metadata, strategy_id)
        if can_short:
            return self._enter_v11_short(bar, index, metadata, strategy_id)
        if self.state.position_side in {"LONG_CALL_SIGNAL", "LONG_UNDERLYING_DIRECTION"}:
            exit_long = self.state.long_stop is not None and bar.close < float(self.state.long_stop)
            exit_meta = {**metadata, "dynamic_stop_reference": self.state.long_stop, "exit_reason": "V11_ATR_TRAILING_STOP" if exit_long else None}
            if exit_long:
                self._flat_after_exit()
                return VegasDecision(action="EXIT", signal="EXIT_CALL", side="CALL", metadata=exit_meta)
            return VegasDecision(action="NONE", metadata=exit_meta)
        if self.state.position_side in {"SHORT_PUT_SIGNAL", "SHORT_UNDERLYING_DIRECTION"}:
            exit_short = self.state.short_stop is not None and bar.close > float(self.state.short_stop)
            exit_meta = {**metadata, "dynamic_stop_reference": self.state.short_stop, "exit_reason": "V11_ATR_TRAILING_STOP" if exit_short else None}
            if exit_short:
                self._flat_after_exit()
                return VegasDecision(action="EXIT", signal="EXIT_PUT", side="PUT", metadata=exit_meta)
            return VegasDecision(action="NONE", metadata=exit_meta)
        return VegasDecision(action="NONE", metadata=metadata)

    def _update_v11_stops(self, bar: VegasBar, atr: float, *, long_trigger: bool, short_trigger: bool) -> None:
        long_candidate = bar.close - atr * self.config.atr_mult
        short_candidate = bar.close + atr * self.config.atr_mult
        if long_trigger:
            self.state.long_stop = long_candidate
        elif self.state.long_stop is not None:
            self.state.long_stop = max(float(self.state.long_stop), long_candidate)
        if short_trigger:
            self.state.short_stop = short_candidate
        elif self.state.short_stop is not None:
            self.state.short_stop = min(float(self.state.short_stop), short_candidate)
        self.state.internal_long_stop = self.state.long_stop
        self.state.internal_short_stop = self.state.short_stop

    def _enter_v11_long(self, bar: VegasBar, index: int, metadata: Dict[str, Any], strategy_id: str) -> VegasDecision:
        stop = float(self.state.long_stop if self.state.long_stop is not None else bar.close)
        self.state.position_side = "LONG_CALL_SIGNAL"
        self.state.entry_reference_price = bar.close
        self.state.long_peak = bar.high
        self.state.long_entry_index = index
        self.state.initial_underlying_stop = stop
        self.state.internal_long_stop = stop
        self.state.short_entry_index = None
        self.state.short_trough = None
        self.state.trades_today += 1
        self.state.last_signal_index = index
        self.state.last_signal_ts = bar.ts
        self._clear_setups()
        metadata = {**metadata, "trigger_reason": "V11_LONG_TRIGGER", "initial_underlying_stop": stop, "dynamic_stop_reference": stop, "setup_state": self._setup_state_payload()}
        return VegasDecision(
            action="ENTRY",
            signal="BUY_CALL",
            side="CALL",
            metadata=metadata,
            order_intent=self._entry_intent("CALL", bar, stop, metadata, strategy_id),
            exit_plan=self._build_exit_plan("LONG", stop, quantity=self.config.contracts),
        )

    def _enter_v11_short(self, bar: VegasBar, index: int, metadata: Dict[str, Any], strategy_id: str) -> VegasDecision:
        stop = float(self.state.short_stop if self.state.short_stop is not None else bar.close)
        self.state.position_side = "SHORT_PUT_SIGNAL"
        self.state.entry_reference_price = bar.close
        self.state.short_trough = bar.low
        self.state.short_entry_index = index
        self.state.initial_underlying_stop = stop
        self.state.internal_short_stop = stop
        self.state.long_entry_index = None
        self.state.long_peak = None
        self.state.trades_today += 1
        self.state.last_signal_index = index
        self.state.last_signal_ts = bar.ts
        self._clear_setups()
        metadata = {**metadata, "trigger_reason": "V11_SHORT_TRIGGER", "initial_underlying_stop": stop, "dynamic_stop_reference": stop, "setup_state": self._setup_state_payload()}
        return VegasDecision(
            action="ENTRY",
            signal="BUY_PUT",
            side="PUT",
            metadata=metadata,
            order_intent=self._entry_intent("PUT", bar, stop, metadata, strategy_id),
            exit_plan=self._build_exit_plan("SHORT", stop, quantity=self.config.contracts),
        )

    def _entry_intent(self, direction: str, bar: VegasBar, stop: float, metadata: Dict[str, Any], strategy_id: str) -> Dict[str, Any]:
        signal_id = f"{strategy_id}:{bar.ts.isoformat()}:{direction}"
        return {
            "side": "BUY",
            "instrument_type": "OPTION",
            "direction": direction,
            "underlying_symbol": self.config.underlying_symbol,
            "reference_underlying_price": float(bar.close),
            "initial_underlying_stop": float(stop),
            "strategy_id": strategy_id,
            "signal_id": signal_id,
            "risk_metadata": {
                "risk_atr": metadata.get("risk_atr"),
                "distance_from_fast_hma_atr": metadata.get("distance_from_fast_hma_atr"),
                "max_loss_per_trade": self.config.max_loss_per_trade,
            },
        }

    def _build_exit_plan(self, side: str, stop: float, *, quantity: float, option_contract_id: Optional[str] = None) -> UnderlyingTriggeredOptionExitPlan:
        spec = None
        if self.config.use_ibkr_native_trailing_stop:
            mode = str(self.config.trailing_mode or "percent").lower()
            spec = IBKRTrailingStopOrderSpec(
                action="SELL",
                orderType="TRAIL",
                totalQuantity=float(quantity),
                trailingPercent=float(self.config.trailing_percent) if mode == "percent" else None,
                auxPrice=float(self.config.trailing_amount) if mode == "amount" and self.config.trailing_amount is not None else None,
                trailStopPrice=None,
            )
        return UnderlyingTriggeredOptionExitPlan(
            underlying_symbol=self.config.underlying_symbol,
            trigger_price=float(stop),
            trigger_direction="BELOW" if side == "LONG" else "ABOVE",
            option_contract_id=option_contract_id,
            exit_action="SELL",
            quantity=float(quantity),
            ibkr_trailing_order_spec=spec,
            underlying_stop_reference=float(stop),
            option_stop_order_spec=asdict(spec) if spec is not None else None,
        )

    def _evaluate_exit(self, bar: VegasBar, index: int, ind: Dict[str, Any], meta: Dict[str, Any]) -> VegasDecision:
        atr = float(meta["atr"])
        side = self.state.position_side
        self._update_v11_stops(bar, atr, long_trigger=False, short_trigger=False)
        if side in {"LONG_CALL_SIGNAL", "LONG_UNDERLYING_DIRECTION"}:
            stop = self.state.long_stop
            exit_long = stop is not None and bar.close < float(stop)
            metadata = {**meta, "dynamic_stop_reference": stop, "long_stop": stop, "exit_reason": "V11_ATR_TRAILING_STOP" if exit_long else None}
            if exit_long:
                self._flat_after_exit()
                return VegasDecision(action="EXIT", signal="EXIT_CALL", side="CALL", metadata=metadata)
            return VegasDecision(action="NONE", metadata=metadata)
        stop = self.state.short_stop
        exit_short = stop is not None and bar.close > float(stop)
        metadata = {**meta, "dynamic_stop_reference": stop, "short_stop": stop, "exit_reason": "V11_ATR_TRAILING_STOP" if exit_short else None}
        if exit_short:
            self._flat_after_exit()
            return VegasDecision(action="EXIT", signal="EXIT_PUT", side="PUT", metadata=metadata)
        return VegasDecision(action="NONE", metadata=metadata)

    def _flat_after_exit(self) -> None:
        self.state.position_side = None
        self.state.entry_reference_price = None
        self.state.initial_underlying_stop = None
        self.state.internal_long_stop = self.state.long_stop
        self.state.internal_short_stop = self.state.short_stop
        self.state.long_peak = None
        self.state.short_trough = None
        self.state.long_entry_index = None
        self.state.short_entry_index = None


class VegasHMASetupTriggerRiskV12Plugin(BaseOptionPlugin):
    component_name = "vegas_hma_setup_trigger_risk_v12_plugin"
    supports_pure_kernel = True
    kernel_can_submit = False

    def __init__(self, config: VegasHMAConfig | None = None) -> None:
        self.config = config or VegasHMAConfig()
        self.core = VegasHMASetupTriggerRiskV12Core(self.config)
        self._last_decision: Optional[VegasDecision] = None
        self._last_extras: Dict[str, Any] = {}
        self._pending_entry_pre_state: Optional[VegasState] = None
        self._source_bar_sec: int = 60
        self._market_data_source: str = "sfs"
        self._minute_builder: Optional[_VegasMinuteBuilder] = None
        self._last_completed_minute_ts: Optional[datetime] = None
        self._last_completed_bar: Optional[Dict[str, Any]] = None

    def apply_default_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(extras or {})
        merged.setdefault("underlying", self.config.underlying_symbol)
        merged.setdefault("risk_symbol", f"OPT:{self.config.underlying_symbol}:0DTE")
        merged.setdefault("definition_id", "VEGAS_HMA_SETUP_TRIGGER_RISK_V12")
        merged.setdefault("kernel_mode", "pure_plugin")
        merged.setdefault("strategy_key", STRATEGY_KEY)
        merged.setdefault("signal_only", True)
        merged.setdefault("enable_live_order_intents", False)
        merged.setdefault("contracts", self.config.contracts)
        merged.setdefault("history_ready_min_bars", 180)
        merged.setdefault("preload_history_bars", 220)
        merged.setdefault("market_data_source", "sfs")
        merged.setdefault("sfs_first", True)
        merged.setdefault("source_bar_sec", 60)
        merged.setdefault("realtime_bar_sec", merged.get("source_bar_sec", 60))
        merged.setdefault(
            "realtime_feature_engine",
            {
                "enabled": True,
                "emit_on": "1m",
                "timeframes": ["1s", "1m"],
                "lookbacks": {"1s": 3600, "1m": 600},
                "sink": {"enabled": False},
            },
        )
        merged.setdefault("ab_time_start", self.config.entry_start)
        merged.setdefault("ab_time_end", self.config.entry_end)
        merged.setdefault("no_new_entry_after", self.config.entry_end)
        merged.setdefault("force_flatten_time", "15:58:00")
        cfg = dict(merged.get("vegas_hma_setup_trigger_risk_v12") or {})
        cfg.setdefault("underlying_symbol", str(merged.get("underlying") or self.config.underlying_symbol))
        cfg.setdefault("contracts", int(merged.get("contracts") or self.config.contracts))
        cfg.setdefault("signal_only", bool(merged.get("signal_only", True)))
        self.config = VegasHMAConfig.from_mapping(cfg)
        self.core = VegasHMASetupTriggerRiskV12Core(self.config)
        self._last_extras = dict(merged)
        self._market_data_source = str(merged.get("market_data_source") or "sfs").strip().lower()
        if bool(merged.get("sfs_first", True)) and self._market_data_source in {"sfs", "sfs_first", "rfe"}:
            self._source_bar_sec = 60
        else:
            self._source_bar_sec = max(5, int(merged.get("realtime_bar_sec") or merged.get("source_bar_sec") or 5))
        self._minute_builder = None
        self._last_completed_minute_ts = None
        self._last_completed_bar = None
        return merged

    def build_delegate(self, ctx: StrategyContext, params: StrategyParams) -> StrategyAlgorithm:
        raise NotImplementedError("Vegas V12 is a pure plugin strategy; set kernel_mode=pure_plugin")

    async def initialize_pure(self, ctx: StrategyContext, params: StrategyParams, kernel: Any) -> None:
        extras = self.apply_default_extras(dict(params.extras or {}))
        params.extras = extras
        preload_bars = max(0, int(extras.get("preload_history_bars", 220) or 220))
        underlying = str(extras.get("underlying") or self.config.underlying_symbol).upper()
        if preload_bars > 0:
            try:
                rows = await ctx.history(underlying, "1m", preload_bars)
                kernel.preload_bar_history(underlying, list(rows or []))
            except Exception as exc:
                log = getattr(ctx, "log", None)
                if log is not None:
                    log.warning("vegas_v12_preload_failed", strategy_id=str(ctx.strategy_id), error=str(exc))
        if bool(extras.get("sfs_first", True)):
            rfe_cfg = dict(extras.get("realtime_feature_engine") or {})
            rfe_cfg.setdefault("enabled", True)
            rfe_cfg.setdefault("emit_on", "1m")
            rfe_cfg.setdefault("timeframes", ["1s", "1m"])
            rfe_cfg.setdefault("lookbacks", {"1s": 3600, "1m": 600})
            rfe_cfg.setdefault("sink", {"enabled": False})
            if bool(rfe_cfg.get("enabled", True)) and hasattr(ctx, "enable_realtime_feature_engine"):
                self._source_bar_sec = 60
                self._market_data_source = "sfs"
                await ctx.enable_realtime_feature_engine(
                    [underlying],
                    spec=rfe_cfg,
                    emit_interval_sec=max(1, int(extras.get("sfs_poll_interval_sec", 1) or 1)),
                    warmup_bars=max(1, int(extras.get("sfs_warmup_bars", 1) or 1)),
                )
                log = getattr(ctx, "log", None)
                if log is not None:
                    log.info(
                        "vegas_v12_sfs_first_enabled",
                        strategy_id=str(ctx.strategy_id),
                        underlying=underlying,
                        emit_on=str(rfe_cfg.get("emit_on", "1m")),
                    )
                return

        source_bar_sec = max(5, int(extras.get("realtime_bar_sec") or extras.get("source_bar_sec") or 5))
        self._source_bar_sec = source_bar_sec
        self._market_data_source = "clickhouse"
        await ctx.subscribe_features([underlying], bar_sec=source_bar_sec)

    def build_signal_pure(self, runtime: PluginRuntimeState) -> Optional[Signal]:
        self._hydrate_core_history(runtime)
        pending_entry_count = int((runtime.payload or {}).get("pending_entry_count") or 0)
        completed = self._complete_runtime_bar(runtime)
        if completed is None:
            return None
        minute_ts = completed.get("ts")
        if isinstance(minute_ts, datetime) and self._last_completed_minute_ts == minute_ts:
            return None
        self._last_completed_minute_ts = minute_ts if isinstance(minute_ts, datetime) else runtime.now
        self._last_completed_bar = dict(completed)
        bar = VegasBar(
            ts=minute_ts if isinstance(minute_ts, datetime) else runtime.now,
            open=float(completed.get("open") or completed.get("close") or 0.0),
            high=float(completed.get("high") or completed.get("close") or 0.0),
            low=float(completed.get("low") or completed.get("close") or 0.0),
            close=float(completed.get("close") or 0.0),
            volume=float(completed.get("volume") or 0.0),
        )
        previous_state = copy.deepcopy(self.core.state)
        self._last_decision = self.core.on_bar(bar, strategy_id=runtime.strategy_id)
        if self._last_decision.action == "ENTRY":
            self._pending_entry_pre_state = previous_state
        if self._last_decision.action == "EXIT" and runtime.active_position is None:
            if pending_entry_count > 0:
                self.core.state = previous_state
            self._last_decision = VegasDecision(
                action="NONE",
                metadata={**dict(self._last_decision.metadata or {}), "block_reason": "NO_ACTIVE_POSITION_FOR_EXIT"},
            )
            return None
        if self._last_decision.action not in {"ENTRY", "EXIT"}:
            return None
        return Signal(
            ts=runtime.now,
            side=str(self._last_decision.side or ""),
            signal_kind=str(self._last_decision.signal or STRATEGY_KEY),
            grade="A",
            regime=STRATEGY_KEY,
            payload={
                **dict(self._last_decision.metadata or {}),
                "order_intent": dict(self._last_decision.order_intent or {}),
                "exit_plan": self._last_decision.exit_plan.to_dict() if self._last_decision.exit_plan else None,
                "source_trigger_ts": runtime.now.isoformat(),
            },
        )

    @staticmethod
    def _minute_floor(ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0)

    @staticmethod
    def _runtime_bar_dict(runtime: PluginRuntimeState) -> Dict[str, Any]:
        close = float(runtime.bar.close or 0.0)
        features = dict(runtime.features or {})
        return {
            "ts": runtime.now,
            "open": float(runtime.bar.open or features.get("open") or close),
            "high": float(runtime.bar.high or features.get("high") or close),
            "low": float(runtime.bar.low or features.get("low") or close),
            "close": close,
            "volume": float(runtime.bar.volume or features.get("volume") or 0.0),
        }

    @staticmethod
    def _new_minute_builder(minute_start: datetime, bar: Dict[str, Any]) -> _VegasMinuteBuilder:
        close = float(bar.get("close") or 0.0)
        return _VegasMinuteBuilder(
            minute_start=minute_start,
            open=float(bar.get("open") or close),
            high=float(bar.get("high") or close),
            low=float(bar.get("low") or close),
            close=close,
            volume=float(bar.get("volume") or 0.0),
        )

    def _complete_runtime_bar(self, runtime: PluginRuntimeState) -> Optional[Dict[str, Any]]:
        bar = self._runtime_bar_dict(runtime)
        if self._source_bar_sec >= 60:
            return bar

        # Sub-minute bars are stamped at bar end. Subtract one microsecond so a
        # bar ending at 09:36:00 is included in the 09:35 minute candle.
        minute_start = self._minute_floor(runtime.now - timedelta(microseconds=1))
        if self._minute_builder is None:
            self._minute_builder = self._new_minute_builder(minute_start, bar)
            return None
        if minute_start == self._minute_builder.minute_start:
            self._minute_builder.ingest(bar)
            return None
        completed = self._minute_builder.finalize()
        self._minute_builder = self._new_minute_builder(minute_start, bar)
        return completed

    def _hydrate_core_history(self, runtime: PluginRuntimeState) -> None:
        if self.core.bars:
            return
        history = []
        for row in list((runtime.payload or {}).get("bar_history") or []):
            ts = row.get("ts")
            if not isinstance(ts, datetime) or ts == runtime.now:
                continue
            close = float(row.get("close") or 0.0)
            if close <= 0.0:
                continue
            history.append(
                VegasBar(
                    ts=ts,
                    open=float(row.get("open") or close),
                    high=float(row.get("high") or close),
                    low=float(row.get("low") or close),
                    close=close,
                    volume=float(row.get("volume") or 0.0),
                )
            )
        if history:
            self.core.preload_history(history)

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal) -> Optional[EntryPlan]:
        if self._last_decision is None or self._last_decision.action != "ENTRY":
            return None
        stop = float(self._last_decision.metadata.get("initial_underlying_stop") or 0.0)
        intent = dict(self._last_decision.order_intent or {})
        close = float(intent.get("reference_underlying_price") or (self._last_completed_bar or {}).get("close") or runtime.bar.close or runtime.spot or 0.0)
        side = str(self._last_decision.side or signal.side)
        risk = abs(close - stop)
        target_1r = close + risk if side == "CALL" else close - risk
        target_1p2r = close + 1.2 * risk if side == "CALL" else close - 1.2 * risk
        return EntryPlan(
            ts=runtime.now,
            side=side,
            signal_kind=STRATEGY_KEY,
            structure_stop=stop,
            target_1r=target_1r,
            target_1p2r=target_1p2r,
            r_per_share=risk,
            contracts=int(self.config.contracts),
            payload={
                "strategy_key": STRATEGY_KEY,
                "order_intent": dict(self._last_decision.order_intent or {}),
                "exit_plan": self._last_decision.exit_plan.to_dict() if self._last_decision.exit_plan else None,
                "metadata": dict(self._last_decision.metadata or {}),
            },
        )

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan) -> Optional[Selection]:
        if self.config.signal_only or not bool((self._last_extras or {}).get("enable_live_order_intents", False)):
            return None
        quote, reason = select_option_quote_for_underlying(
            underlying=self.config.underlying_symbol,
            extras=self._last_extras,
            now=runtime.now,
            side=str(entry_plan.side),
            spot=float(runtime.spot or 0.0),
            spread_max_pct=float((self._last_extras or {}).get("spread_max_pct", 0.18) or 0.18),
            now_for_quote_age=runtime.now,
        )
        if quote is None:
            return None
        return Selection(
            opt_symbol=str(quote.get("opt_symbol") or ""),
            expiry=str(quote.get("expiry") or ""),
            strike=float(quote.get("strike") or 0.0),
            side=str(entry_plan.side),
            bid=float(quote.get("bid") or 0.0),
            ask=float(quote.get("ask") or 0.0),
            mid=float(quote.get("mid") or 0.0),
            last=float(quote.get("last") or 0.0),
            quote_age_sec=float(quote.get("quote_age_sec") or 0.0),
            payload={"reject_reason": str(reason or "")},
        )

    def build_execution_profile_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan, selection: Optional[Selection]) -> ExecutionProfile:
        return build_execution_profile_from_extras(
            profile_id=str((self._last_extras or {}).get("live_parity_profile_id") or STRATEGY_KEY),
            market_data_profile_id=str((self._last_extras or {}).get("market_data_profile_id") or "standard_close_extended_sma"),
            extras=self._last_extras,
        )

    def build_exit_plans_pure(self, runtime: PluginRuntimeState) -> List[ExitPlan]:
        if self._last_decision is None or self._last_decision.action != "EXIT":
            return []
        pos = runtime.active_position
        if pos is None:
            return []
        lot_id = str(getattr(pos, "position_lot_id", "") or "vegas_v12_internal")
        qty = float(getattr(pos, "remaining_qty", 0.0) or self.config.contracts)
        side = str(getattr(pos, "side", "") or self._last_decision.side or "").upper()
        option_contract_id = str(getattr(pos, "opt_symbol", "") or getattr(pos, "symbol", "") or "") or None
        stop = float(self._last_decision.metadata.get("dynamic_stop_reference") or 0.0)
        underlying_plan = self.core._build_exit_plan(
            "SHORT" if side == "PUT" else "LONG",
            stop,
            quantity=qty,
            option_contract_id=option_contract_id,
        )
        exit_model = underlying_plan.to_exit_plan(
            ts=runtime.now,
            position_lot_id=lot_id,
            reason=str(self._last_decision.metadata.get("exit_reason") or self._last_decision.signal or "VEGAS_V11_EXIT"),
        )
        return [
            ExitPlan(
                ts=runtime.now,
                position_lot_id=lot_id,
                qty=qty,
                reason=str(self._last_decision.metadata.get("exit_reason") or self._last_decision.signal or "VEGAS_V12_EXIT"),
                payload={**dict(exit_model.payload or {}), **dict(self._last_decision.metadata or {})},
            )
        ]

    def on_entry_submitted_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        position_lot_id: str,
    ) -> None:
        self._pending_entry_pre_state = None
        return None

    def on_entry_submit_blocked_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
        reason: str,
        payload: Dict[str, Any],
    ) -> None:
        # Entry signal state is optimistic until the kernel accepts an intent.
        # Roll back on selection/risk/submit rejection so the next setup is not blocked by a phantom position
        # and rejected attempts do not consume daily trade budget.
        pre_state = self._pending_entry_pre_state
        if pre_state is not None:
            self.core.state = copy.deepcopy(pre_state)
            self.core.state.last_signal_ts = runtime.now
            self._pending_entry_pre_state = None
            return
        self.core._flat_after_exit()
